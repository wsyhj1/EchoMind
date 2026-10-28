import asyncio
from dataclasses import replace
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rag.evidence_checker import EvidenceChecker, decompose
from rag.evidence_settings import EvidenceSettings
from rag.retrieval_service import RetrievalService
from rag.types import ChildHit, RetrievalCandidate, RetrievalOptions, RetrievalScope
from observability import RunTrace


ARCH = "晨星系统使用云舟缓存集群缓存业务状态，缓存有效期为600秒。"
OWNER = "云舟缓存集群由平台运维组维护。"
BOOK = "云舟缓存集群的典型故障是连接池耗尽。遇到连接池耗尽时，处理步骤为：限流，再检查慢查询。执行前必须备份实例配置。"
QUERY = "说明晨星系统的缓存名称、维护组和排障步骤。"


def candidate(pid, text, kb="kb", user=1):
    return RetrievalCandidate(pid, user, kb, pid, pid + ".docx", text)


class EvidenceCoverageTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_chain_and_procedure_have_exact_quotes(self):
        candidates = [candidate("a", ARCH), candidate("o", OWNER), candidate("b", BOOK)]
        report = await EvidenceChecker().check(QUERY, candidates)
        self.assertEqual([row.status for row in report.coverage], ["supported"] * 3)
        by_key = {item.key: item.text for item in candidates}
        for row in report.coverage:
            for support in row.supports:
                self.assertIn(support.quote, by_key[support.key])
        self.assertEqual({support.key[2] for support in report.coverage[-1].supports}, {"a", "b"})

    async def test_relation_needs_both_hops_and_exact_instance(self):
        for rows in ([candidate("o", OWNER)], [candidate("a", ARCH), candidate("wrong", OWNER.replace("云舟", "远帆"))]):
            report = await EvidenceChecker().check("晨星系统的缓存谁维护？", rows)
            self.assertEqual(report.coverage[0].status, "missing")

    async def test_similarity_or_negated_claim_does_not_certify_support(self):
        for text in ("云舟缓存集群可能由平台运维组维护。", "云舟缓存集群计划由平台运维组维护。",
                     "我们曾讨论云舟缓存集群的维护团队，尚未登记。"):
            report = await EvidenceChecker().check("云舟缓存集群由谁维护？", [candidate("p", text)])
            self.assertNotEqual(report.coverage[0].status, "supported")

    async def test_conflicts_keep_both_quotes_instead_of_majority_vote(self):
        report = await EvidenceChecker().check("云舟缓存集群由谁维护？", [candidate("a", OWNER),
            candidate("b", OWNER.replace("平台运维组", "业务支持组")), candidate("c", OWNER)])
        self.assertEqual(report.coverage[0].status, "conflicting")
        self.assertEqual(len(report.coverage[0].supports), 3)

    async def test_time_version_and_generic_questions_are_explicitly_uncertain(self):
        for query in ("2024年云舟缓存集群由谁维护？", "v2版本云舟缓存集群谁负责？", "请比较两种架构优缺点"):
            report = await EvidenceChecker().check(query, [candidate("o", OWNER)])
            self.assertEqual(report.coverage[0].status, "partial")

    async def test_procedure_for_a_different_fault_is_not_support(self):
        report = await EvidenceChecker().check("晨星系统主从切换怎么处理？", [candidate("a", ARCH), candidate("b", BOOK)])
        self.assertEqual(report.coverage[0].status, "missing")
        report = await EvidenceChecker().check("晨星系统连接池耗尽怎么处理？", [candidate("a", ARCH), candidate("b", BOOK)])
        self.assertEqual(report.coverage[0].status, "supported")

    async def test_decomposition_is_bounded_and_preserves_original_constraints(self):
        query = "2024年晨星系统的缓存名称、维护组、有效期和排障步骤。"
        goals = decompose(query)
        self.assertEqual(len(goals), 3)
        self.assertEqual(goals[-1].kind, "generic")
        self.assertTrue(all(query in goal.question for goal in goals))

    async def test_multiple_faults_in_one_parent_do_not_guess_procedure_binding(self):
        text = "云舟缓存集群的典型故障是连接池耗尽。云舟缓存集群的典型故障是主从切换。遇到主从切换时，处理步骤为：恢复流量。"
        report = await EvidenceChecker().check("云舟缓存集群连接池耗尽怎么处理？", [candidate("p", text)])
        self.assertEqual(report.coverage[0].status, "missing")

    async def test_explicit_past_or_present_language_is_not_certified_without_time_review(self):
        for marker in ("以前", "曾经", "目前"):
            report = await EvidenceChecker().check(f"云舟缓存集群{marker}由谁维护？", [candidate("p", OWNER)])
            self.assertEqual(report.coverage[0].status, "partial")

    async def test_unterminated_procedure_sentence_is_not_certified_as_complete(self):
        text = "云舟缓存集群的典型故障是连接池耗尽。遇到连接池耗尽时，处理步骤为：先限流，再检查"
        report = await EvidenceChecker().check("云舟缓存集群连接池耗尽怎么处理？", [candidate("p", text)])
        self.assertEqual(report.coverage[0].status, "missing")


class TargetedRetryTests(unittest.IsolatedAsyncioTestCase):
    def service(self, first=None, *, second=None, checker=None, retry_error=None):
        first = first if first is not None else [candidate("a", ARCH), candidate("o", OWNER)]
        second = second if second is not None else [candidate("b", BOOK)]
        batches = [first, second]
        milvus = SimpleNamespace(retrieve_knowledge_hits=AsyncMock(side_effect=[
            [ChildHit(c.parent_id, c.knowledge_base_id, c.parent_id, i, .1, "rrf", "rrf", c.file_hash)
             for i, c in enumerate(batch, 1)] for batch in batches]))
        database = SimpleNamespace(get_parents_with_metadata=AsyncMock(side_effect=[
            [dict(parent_id=c.parent_id, knowledge_base_id=c.knowledge_base_id, user_id=c.user_id,
                  file_hash=c.file_hash, file_name=c.file_name, text=c.text) for c in batch] for batch in batches]))
        if retry_error:
            milvus.retrieve_knowledge_hits.side_effect = [milvus.retrieve_knowledge_hits.side_effect.__next__(), retry_error]
        async def rerank(query, documents, count):
            # Deliberately rank the runbook last: coverage preservation must keep it.
            return {"output": {"results": [{"index": i, "relevance_score": 1 - i / 10}
                                             for i in range(min(count, len(documents)))]}}
        reranker = AsyncMock(side_effect=rerank)
        return RetrievalService(milvus, database, reranker, evidence_checker=checker), milvus, database, reranker

    async def retrieve(self, service, query=QUERY, k=3, *, rerank=True):
        return await service.retrieve_context(query, RetrievalScope(1, "kb"), k,
            RetrievalOptions(graph_enabled=False, evidence_enabled=True, rerank_enabled=rerank))

    async def test_missing_goal_gets_one_scoped_retry_and_final_trace_only_once(self):
        service, milvus, database, rerank = self.service()
        trace = RunTrace(1, "evidence", QUERY, "kb")
        with trace.activate():
            result = await self.retrieve(service)
        self.assertEqual(milvus.retrieve_knowledge_hits.await_count, 2)
        retry = milvus.retrieve_knowledge_hits.call_args.kwargs
        self.assertEqual((retry["user_id"], retry["knowledge_base_id"], retry["top_k"]), (1, "kb", 3))
        self.assertIn(QUERY, retry["query"])
        self.assertIn("故障排查与处理步骤", retry["query"])
        self.assertEqual(result.trace.extra_search_count, 1)
        self.assertEqual([row["status"] for row in result.trace.evidence_coverage["coverage"]], ["supported"] * 3)
        self.assertEqual({c.parent_id for c in result.selected}, {"a", "o", "b"})
        final = [row for row in trace.contexts if row["stage"] == "final"]
        self.assertEqual(len(final), 3)
        self.assertEqual(rerank.await_count, 2)  # Initial + merged pool, no extra reranker on the retry alone.

    async def test_complete_first_pass_does_not_retry(self):
        service, milvus, _, _ = self.service(first=[candidate("a", ARCH), candidate("o", OWNER), candidate("b", BOOK)])
        result = await self.retrieve(service)
        self.assertEqual(milvus.retrieve_knowledge_hits.await_count, 1)
        self.assertEqual(result.trace.extra_search_count, 0)

    async def test_no_answer_stops_after_one_retry_and_has_missing_answer_rule(self):
        service, milvus, _, _ = self.service(first=[], second=[])
        result = await self.retrieve(service, "云舟缓存集群由谁维护？")
        self.assertEqual(milvus.retrieve_knowledge_hits.await_count, 2)
        self.assertEqual(result.trace.evidence_coverage["coverage"][0]["status"], "missing")
        self.assertIn("未找到足够依据", result.context_text())

    async def test_unknown_question_and_temporal_scope_do_not_trigger_blind_retry(self):
        for query in ("比较晨星与别的系统的优劣", "2024年晨星系统的缓存谁维护？"):
            service, milvus, _, _ = self.service()
            await self.retrieve(service, query)
            self.assertEqual(milvus.retrieve_knowledge_hits.await_count, 1)

    async def test_default_disabled_skips_checking_and_explicit_false_overrides_env(self):
        service, milvus, _, _ = self.service()
        with patch.dict(os.environ, {"EVIDENCE_CHECK_ENABLED": "true"}):
            result = await service.retrieve_context(QUERY, RetrievalScope(1, "kb"), 3,
                RetrievalOptions(graph_enabled=False, evidence_enabled=False))
        self.assertEqual(milvus.retrieve_knowledge_hits.await_count, 1)
        # Disabling checking still exposes real source names for citation; it
        # must not invent a coverage result or perform supplemental retrieval.
        import json
        payload = json.loads(result.context_text().split("\n", 1)[1])
        self.assertEqual([item["text"] for item in payload["evidence"]], [ARCH, OWNER])
        self.assertNotIn("coverage", payload)

    async def test_checker_timeout_falls_back_to_original_and_cancellation_propagates(self):
        async def slow(*args):
            await asyncio.sleep(.1)
        service, milvus, _, _ = self.service(checker=SimpleNamespace(check=AsyncMock(side_effect=slow)))
        with patch.dict(os.environ, {"EVIDENCE_CHECK_TIMEOUT_MS": "1"}):
            result = await self.retrieve(service)
        self.assertFalse(result.trace.evidence_check_enabled)
        self.assertEqual(result.trace.fallback_reasons, ["evidence_TimeoutError"])
        service, _, _, _ = self.service(checker=SimpleNamespace(check=AsyncMock(side_effect=asyncio.CancelledError)))
        with self.assertRaises(asyncio.CancelledError):
            await self.retrieve(service)

    async def test_retry_failure_preserves_original_evidence_and_attempt_count(self):
        service, milvus, _, _ = self.service(retry_error=RuntimeError("offline"))
        result = await self.retrieve(service)
        self.assertEqual(result.trace.extra_search_count, 1)
        self.assertFalse(result.trace.evidence_check_enabled)
        self.assertEqual([c.parent_id for c in result.selected], ["a", "o"])

    async def test_scope_filter_on_retry_drops_foreign_records(self):
        service, _, _, _ = self.service(second=[candidate("b", BOOK, user=2)])
        result = await self.retrieve(service)
        self.assertNotIn("b", [c.parent_id for c in result.selected])
        self.assertEqual(result.trace.evidence_coverage["coverage"][-1]["status"], "missing")

    async def test_top_k_and_context_budget_remain_hard_limits(self):
        service, _, _, _ = self.service(first=[candidate("a", ARCH + "说明" * 3000), candidate("o", OWNER)])
        with patch.dict(os.environ, {"EVIDENCE_MAX_CONTEXT_CHARS": "2000"}):
            result = await self.retrieve(service, k=2, rerank=False)
        self.assertLessEqual(len(result.selected), 2)
        self.assertLessEqual(len(result.context_text()), 2000)

    async def test_unhelpful_extra_evidence_does_not_displace_verified_first_pass(self):
        service, _, _, _ = self.service(second=[candidate("wrong", BOOK.replace("云舟", "远帆"))])
        result = await self.retrieve(service)
        self.assertEqual([c.parent_id for c in result.selected], ["a", "o"])

    async def test_context_budget_does_not_turn_cut_off_procedure_into_supported_answer(self):
        text = "云舟缓存集群的典型故障是连接池耗尽。遇到连接池耗尽时，处理步骤为：" + "检查日志，" * 800 + "最后备份配置。"
        service, _, _, _ = self.service(first=[candidate("p", text)])
        with patch.dict(os.environ, {"EVIDENCE_MAX_CONTEXT_CHARS": "2000"}):
            result = await self.retrieve(service, query="云舟缓存集群连接池耗尽怎么处理？", k=1, rerank=False)
        self.assertTrue(all(row["status"] != "supported" for row in result.trace.evidence_coverage["coverage"]))

    async def test_original_user_time_constraint_survives_agent_query_rewrite(self):
        service, _, _, _ = self.service(first=[candidate("o", OWNER)])
        trace = RunTrace(1, "rewrite", "2024年云舟缓存集群由谁维护？", "kb")
        with trace.activate():
            result = await self.retrieve(service, query="云舟缓存集群由谁维护？", k=1)
        self.assertEqual(result.trace.evidence_coverage["coverage"][0]["status"], "partial")

    async def test_original_multi_goal_question_survives_agent_query_rewrite(self):
        service, milvus, _, _ = self.service()
        trace = RunTrace(1, "rewrite", QUERY, "kb")
        with trace.activate():
            result = await self.retrieve(service, query="晨星系统缓存谁维护？")
        self.assertEqual(len(result.trace.evidence_coverage["coverage"]), 3)
        self.assertEqual(milvus.retrieve_knowledge_hits.await_count, 2)
        self.assertIn(QUERY, milvus.retrieve_knowledge_hits.call_args.kwargs["query"])


class EvidenceSettingsTests(unittest.TestCase):
    def test_retry_and_candidate_caps_cannot_be_bypassed(self):
        for values in ({"EVIDENCE_MAX_RETRIEVAL_ROUNDS": "2"}, {"EVIDENCE_EXTRA_PARENT_LIMIT": "4"},
                       {"EVIDENCE_MAX_CONTEXT_CHARS": "0"}, {"EVIDENCE_CHECK_TIMEOUT_MS": "-1"}):
            with patch.dict(os.environ, values), self.assertRaises(ValueError):
                EvidenceSettings.from_env()


if __name__ == "__main__":
    unittest.main()

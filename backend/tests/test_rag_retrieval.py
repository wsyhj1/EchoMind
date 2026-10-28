import asyncio
from dataclasses import replace
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from observability import RunTrace
from rag.retrieval_service import RetrievalService
from rag.settings import env_bool
from rag.types import ChildHit, RetrievalOptions, RetrievalScope


def hit(parent="a", kb="kb", rank=1, **extra):
    return ChildHit(parent, kb, str(rank), rank, .02, "rrf", "rrf", file_hash=parent, **extra)


def record(parent="a", kb="kb", user=1, text="same text"):
    return dict(parent_id=parent, knowledge_base_id=kb, user_id=user, file_hash=parent,
                file_name=parent + ".docx", text=text)


class RetrievalContractTests(unittest.IsolatedAsyncioTestCase):
    async def retrieve(self, hits=None, rows=None, response=None, error=None, *, kb="kb", k=2, options=None):
        milvus = SimpleNamespace(retrieve_knowledge_hits=AsyncMock(return_value=hits if hits is not None else [hit(), hit("b", rank=2)]))
        database = SimpleNamespace(get_parents_with_metadata=AsyncMock(return_value=rows if rows is not None else [record(), record("b")]))
        rerank = AsyncMock(return_value=response, side_effect=error)
        trace = RunTrace(1, "unit", "q", kb)
        with trace.activate():
            result = await RetrievalService(milvus, database, rerank).retrieve_context("q", RetrievalScope(1, kb), k, options)
        return result, milvus, database, rerank, trace

    async def test_duplicate_text_maps_by_api_index_and_keeps_child_hits(self):
        result, _, _, _, trace = await self.retrieve(hits=[hit("b"), hit(), hit("b", rank=3)],
            response={"output": {"results": [{"index": 1, "relevance_score": .8}, {"index": 0, "relevance_score": .7}]}})
        self.assertEqual([c.parent_id for c in result.candidates], ["b", "a"])
        self.assertEqual([c.parent_id for c in result.selected], ["a", "b"])
        self.assertEqual(len(result.candidates[0].child_hits), 2)
        self.assertEqual(result.selected[0].rerank_score, .8)
        self.assertEqual(result.selected[0].child_hits[0].score_type, "rrf")
        self.assertEqual([r["parent_id"] for r in trace.contexts if r["stage"] == "final"], ["a", "b"])

    async def test_default_kb_composite_identity_does_not_merge_equal_ids(self):
        result, _, database, _, _ = await self.retrieve(hits=[hit(kb="other"), hit()],
            rows=[record(), record(kb="other")], kb="默认知识库", options=RetrievalOptions(rerank_enabled=False))
        self.assertEqual([c.knowledge_base_id for c in result.selected], ["other", "kb"])
        database.get_parents_with_metadata.assert_awaited_once_with(["a", "a"], "默认知识库", 1)

    async def test_missing_stale_or_wrong_tenant_parent_is_explicitly_skipped(self):
        for rows in ([record(user=2)], [record(kb="foreign")], [{**record(), "file_hash": "stale"}], []):
            with self.subTest(rows=rows):
                result, _, _, rerank, _ = await self.retrieve(hits=[hit()], rows=rows)
                self.assertEqual(result.selected, [])
                self.assertEqual(result.trace.missing_parent_keys, [(1, "kb", "a")])
                rerank.assert_not_awaited()

    async def test_timeout_exception_none_and_bad_indices_fallback_to_ordered_candidates(self):
        responses = [None, {}, {"output": {"results": []}}, {"output": {"results": [{"index": True}]}},
                     {"output": {"results": [{"index": 7}]}}, {"output": {"results": [{"index": 0}, {"index": 0}]}},
                     {"output": {"results": [{"index": 0, "relevance_score": float("nan")}]}}]
        for response in responses:
            with self.subTest(response=response):
                result, *_ = await self.retrieve(response=response)
                self.assertEqual([c.parent_id for c in result.selected], ["a", "b"])
                self.assertTrue(result.trace.fallback_reasons)
                self.assertTrue(all(c.rerank_score is None for c in result.selected))
        result, *_ = await self.retrieve(error=TimeoutError())
        self.assertEqual(result.trace.fallback_reasons, ["rerank_TimeoutError"])

    async def test_late_invalid_rerank_item_does_not_leave_partial_scores(self):
        result, *_ = await self.retrieve(response={"output": {"results": [
            {"index": 1, "relevance_score": .9}, {"index": -1, "relevance_score": .2}]}})
        self.assertEqual([c.parent_id for c in result.selected], ["a", "b"])
        self.assertTrue(all(c.rerank_score is None for c in result.selected))

    async def test_reranker_cannot_address_documents_outside_submitted_api_cap(self):
        hits = [hit(str(i), rank=i + 1) for i in range(8)]
        rows = [record(str(i)) for i in range(8)]
        result, _, _, rerank, _ = await self.retrieve(hits=hits, rows=rows, k=1,
            response={"output": {"results": [{"index": 3}]}})
        self.assertEqual(len(rerank.call_args.args[1]), 3)
        self.assertEqual(result.selected[0].parent_id, "0")
        self.assertTrue(result.trace.fallback_reasons)

    async def test_cancel_is_propagated_and_not_converted_to_fallback(self):
        with self.assertRaises(asyncio.CancelledError):
            await self.retrieve(error=asyncio.CancelledError())

    async def test_empty_result_and_rerank_disabled_do_not_call_external_reranker(self):
        result, _, database, rerank, _ = await self.retrieve(hits=[])
        self.assertEqual(result.context_text(), "未检索到相关文档")
        database.get_parents_with_metadata.assert_not_awaited()
        rerank.assert_not_awaited()
        result, _, _, rerank, _ = await self.retrieve(options=RetrievalOptions(rerank_enabled=False))
        self.assertEqual([c.parent_id for c in result.selected], ["a", "b"])
        rerank.assert_not_awaited()

    async def test_hybrid_fallback_is_retained_without_changing_score_space(self):
        result, *_ = await self.retrieve(hits=[replace(hit(), source="dense", score_type="cosine", fallback=True)])
        self.assertEqual(result.trace.fallback_reasons[0], "hybrid_to_dense")
        self.assertEqual(result.candidates[0].child_hits[0].score_type, "cosine")

    def test_boolean_false_is_false_and_unknown_values_fail_clearly(self):
        with patch.dict("os.environ", {"RAG_TRACE_ENABLED": "false"}):
            self.assertFalse(env_bool("RAG_TRACE_ENABLED"))
        with patch.dict("os.environ", {"RAG_TRACE_ENABLED": "yes"}):
            with self.assertRaises(ValueError):
                env_bool("RAG_TRACE_ENABLED")


class EvidenceMetricTests(unittest.TestCase):
    def test_complete_evidence_and_mrr_have_different_meanings(self):
        from evaluation.rag_metrics import evidence_metrics
        metrics = evidence_metrics(["noise", "b", "b", "a"], ["a", "b", "b"], 2)
        self.assertEqual(metrics, dict(recall_at_k=.5, hit_at_k=1., complete_evidence_at_k=0., mrr_at_k=.5))
        self.assertTrue(all(v is None for v in evidence_metrics([], [], answerable=False).values()))

    def test_failed_cases_are_kept_and_missing_gold_is_not_zero(self):
        from evaluation.rag_metrics import evidence_metrics, summarize
        report = summarize([{"category": "multi_hop", "status": "failed", "metrics": evidence_metrics([], ["a"])},
                            {"category": "unanswerable", "status": "completed", "metrics": evidence_metrics([], [], answerable=False)}])
        self.assertEqual(report["overall"]["cases"], 2)
        self.assertEqual(report["overall"]["failed"], 1)
        self.assertEqual(report["overall"]["metrics"]["recall_at_k"], {"mean": 0., "n": 1})

    def test_datasets_are_fixed_unique_and_have_expected_category_distribution(self):
        from collections import Counter
        from evaluation.rag_fixtures import dataset, corpus
        rows = dataset()
        self.assertEqual(len(rows), len({r["question_id"] for r in rows}))
        self.assertEqual(Counter(r["category"] for r in rows), {"single_fact": 20, "multi_hop": 20,
            "multi_evidence": 20, "ambiguous": 15, "distractor": 10, "unanswerable": 10, "isolation": 5})
        self.assertTrue(all(row["gold_review"] == "pending_human_review" for row in rows))
        self.assertEqual(len(corpus()), 16)


class StructuredManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_structured_hits_preserve_child_order_scope_and_fallback(self):
        from knowledeg_base_manager import KnowledgeBaseManager
        rows = [[{"id": i, "distance": .1, "entity": {"parent_id": parent,
            "knowledge_base_id": "kb", "file_hash": parent, "text": "text"}}
            for i, parent in enumerate(("b", "a", "b"), 1)]]
        client = SimpleNamespace(hybrid_search=AsyncMock(side_effect=RuntimeError("offline")),
                                 search=AsyncMock(return_value=rows))
        manager = KnowledgeBaseManager(client, SimpleNamespace(aembed_query=AsyncMock(return_value=[.1])), "unit", 1)
        hits = await manager.retrieve_knowledge_hits("q", "kb", 17)
        self.assertEqual([h.parent_id for h in hits], ["b", "a", "b"])
        self.assertEqual([h.rank for h in hits], [1, 2, 3])
        self.assertTrue(all(h.fallback and h.source == "dense" and h.score_type == "cosine" for h in hits))
        self.assertIn("user_id == 17", client.search.call_args.kwargs["filter"])
        self.assertIn('knowledge_base_id == "kb"', client.search.call_args.kwargs["filter"])

    async def test_no_embedding_is_failure_in_structured_api_but_legacy_stays_empty(self):
        from knowledeg_base_manager import KnowledgeBaseManager
        manager = KnowledgeBaseManager(SimpleNamespace(), None, "unit", 1)
        manager.get_dense_vector = AsyncMock(return_value=[])
        with self.assertRaises(RuntimeError):
            await manager.retrieve_knowledge_hits("q", "kb", 17)
        self.assertEqual(await manager.hybrid_retrieval_knowledge_base("q", "kb", 17), [])

    async def test_default_scope_has_only_user_filter_and_rejects_injected_user(self):
        from knowledeg_base_manager import KnowledgeBaseManager
        client = SimpleNamespace(hybrid_search=AsyncMock(return_value=[[]]))
        manager = KnowledgeBaseManager(client, SimpleNamespace(aembed_query=AsyncMock(return_value=[.1])), "unit", 1)
        await manager.retrieve_knowledge_hits("q", "默认知识库", 17)
        requests = client.hybrid_search.call_args.kwargs["reqs"]
        self.assertTrue(all(request.expr == "user_id == 17" for request in requests))
        with self.assertRaises(ValueError):
            await manager.retrieve_knowledge_hits("q", "kb", "17 or user_id > 0")


if __name__ == "__main__":
    unittest.main()

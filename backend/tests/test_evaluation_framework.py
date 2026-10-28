import asyncio
import json
import math
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from observability import RunTrace, measure, current_trace, union_ms, safe_error
from evaluation.engine import metric_plan, RagasEvaluator, final_knowledge, supporting_contexts
from evaluation.batch import summarize


def sample_run():
    return {"run_id": str(uuid4()), "user_id": 1, "session_id": "unit", "status": "complete",
            "knowledge_base_id": "test", "query": "What is the code?", "answer": "The code is 42.",
            "config": {"top_k": 3}, "timings": {"server_total_ms": 10},
            "contexts": [{"stage": "final", "source_type": "knowledge", "source_id": "a", "text": "Code 42",
                           "included_in_prompt": True}], "model_inputs": []}


class TimingTests(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_requests_do_not_share_trace_or_parent(self):
        async def request(query):
            trace = RunTrace(1, query, query, "kb")
            with trace.activate():
                with measure("tool", "kb") as parent:
                    async def branch():
                        with measure("retrieval", "kb"):
                            await asyncio.sleep(.01)
                    await asyncio.gather(branch(), branch())
                self.assertIs(current_trace(), trace)
                trace.finish("answer")
                children = [s for s in trace.spans if s["kind"] == "retrieval"]
                self.assertTrue(all(s["parent_span_id"] == parent["span_id"] for s in children))
            return trace
        first, second = await asyncio.gather(request("a"), request("b"))
        self.assertNotEqual(first.run_id, second.run_id)
        self.assertEqual(len(first.spans), 3)
        self.assertEqual(len(second.spans), 3)
        self.assertIsNone(current_trace())
        self.assertIsNone(first.timings()["ttft_ms"])

    def test_overlap_uses_interval_union(self):
        self.assertEqual(union_ms([{"start_ms": 0, "duration_ms": 10}, {"start_ms": 2, "duration_ms": 10},
                                   {"start_ms": 20, "duration_ms": 3}]), 15)

    async def test_failure_is_not_zero_or_success_and_secrets_are_redacted(self):
        trace = RunTrace(1, "a", "q", "kb")
        with trace.activate():
            with self.assertRaises(ValueError):
                with measure("rerank", "kb"):
                    raise ValueError("Bearer secret sk-private-key")
            trace.finish("", "failed")
        self.assertEqual(trace.spans[0]["status"], "failed")
        self.assertNotIn("secret", trace.spans[0]["error"])
        self.assertNotIn("sk-private", trace.spans[0]["error"])
        self.assertNotIn("embedding", trace.timings()["stages"])


class MetricTests(unittest.IsolatedAsyncioTestCase):
    def test_evidence_removed_from_final_model_input_is_not_scored_as_visible(self):
        run = sample_run()
        run["model_inputs"] = [{"messages": [{"role": "human", "content": "Question"}]}]
        self.assertEqual(final_knowledge(run), [])
        self.assertEqual(supporting_contexts(run), [])

    def test_reference_and_proxy_scores_are_not_averaged_together(self):
        report = summarize([{"evaluation": {"result": {"scores": {
            "context_precision": {"status": "completed", "value": .1, "metric_id": "context_precision_proxy"}}}}},
            {"evaluation": {"result": {"scores": {
                "context_precision": {"status": "completed", "value": .9, "metric_id": "context_precision"}}}}}])
        self.assertEqual(report["metrics"]["context_precision_proxy"]["mean"], .1)
        self.assertEqual(report["metrics"]["context_precision"]["mean"], .9)

    def test_missing_reference_is_not_zero(self):
        for metric in ("context_recall", "factual_correctness", "precision_at_k", "recall_at_k"):
            _, result = metric_plan(sample_run(), {}, metric)
            self.assertEqual(result["status"], "not_applicable")
            self.assertIsNone(result["value"])

    def test_id_metrics_have_explicit_k_and_deduplicate(self):
        run = sample_run()
        run["contexts"].append(dict(run["contexts"][0]))
        payload = {"reference_context_ids": ["a", "a", "b"]}
        self.assertAlmostEqual(metric_plan(run, payload, "precision_at_k")[1]["value"], 1 / 3)
        self.assertAlmostEqual(metric_plan(run, payload, "recall_at_k")[1]["value"], .5)

    def test_expected_but_missing_retrieval_is_not_silently_skipped(self):
        run = sample_run()
        run["contexts"] = []
        _, result = metric_plan(run, {"requires_retrieval": True, "reference": "42"}, "context_recall")
        self.assertEqual(result["value"], 0)
        self.assertTrue(result["routing_failure"])
        self.assertEqual(metric_plan(run, {}, "faithfulness")[1]["status"], "not_applicable")

    async def test_judge_failure_remains_failure_and_no_reference_remains_na(self):
        evaluator = RagasEvaluator({"timeout_seconds": 1}, "test")
        evaluator._initialize = AsyncMock()
        evaluator.metrics = {"faithfulness": SimpleNamespace(ascore=AsyncMock(side_effect=RuntimeError("API failed")))}
        result = await evaluator.evaluate(sample_run(), {"metrics": ["faithfulness", "context_recall"]})
        self.assertEqual(result["scores"]["faithfulness"]["status"], "failed")
        self.assertIsNone(result["scores"]["faithfulness"]["value"])
        self.assertEqual(result["scores"]["context_recall"]["status"], "not_applicable")

    def test_report_keeps_failed_cases_in_denominator(self):
        report = summarize([{"error": "failed"}, {"run": sample_run(), "evaluation": {"result": {
            "scores": {"faithfulness": {"status": "completed", "value": .9},
                       "context_recall": {"status": "not_applicable", "value": None}}}}}])
        self.assertEqual(report["cases"], 2)
        self.assertEqual(report["failed_cases"], 1)
        self.assertEqual(report["metrics"]["context_recall"]["not_applicable"], 1)


class RetrievalTests(unittest.IsolatedAsyncioTestCase):
    async def test_hybrid_and_fallback_keep_rank_and_metadata(self):
        from knowledeg_base_manager import KnowledgeBaseManager
        hits = [[{"id": i, "distance": .9, "entity": {"id": i, "parent_id": parent,
                  "text": parent, "file_hash": "hash", "knowledge_base_id": "kb"}}
                 for i, parent in enumerate(["b", "a", "b"])]]
        for fallback in (False, True):
            client = SimpleNamespace(hybrid_search=AsyncMock(side_effect=RuntimeError("fallback") if fallback else None,
                                                            return_value=hits), search=AsyncMock(return_value=hits))
            manager = KnowledgeBaseManager(client, SimpleNamespace(aembed_query=AsyncMock(return_value=[.1])), "test", 1)
            trace = RunTrace(1, "r", "q", "kb")
            with trace.activate(), patch("knowledeg_base_manager.get_stream_writer", return_value=lambda _: None):
                ids = await manager.hybrid_retrieval_knowledge_base("q", "kb", 1)
            self.assertEqual(ids, ["b", "a"])
            self.assertEqual(len(trace.contexts), 3)
            self.assertTrue(all(c["included_in_prompt"] is False for c in trace.contexts))

    async def test_parent_fetch_restores_input_order_and_source_fields(self):
        from postgresql_client import PostgreSQLParentClient
        fake = SimpleNamespace(pool=SimpleNamespace(fetch=AsyncMock(return_value=[
            {"parent_id": "a", "text": "A", "file_hash": "a"}, {"parent_id": "b", "text": "B", "file_hash": "b"}])))
        rows = await PostgreSQLParentClient.get_parent_records(fake, ["b", "a", "b"], "kb", 1)
        self.assertEqual([row["parent_id"] for row in rows], ["b", "a"])
        self.assertEqual(rows[0]["file_hash"], "b")

    async def test_rerank_index_maps_duplicate_text_to_correct_source(self):
        import tools
        from schemas import ContextSchema
        from rag.types import ChildHit
        rows = [{"parent_id": p, "text": "same text", "file_hash": p, "file_name": p,
                 "knowledge_base_id": "kb", "user_id": 1} for p in ("a", "b")]
        database = SimpleNamespace(get_parents_with_metadata=AsyncMock(return_value=rows))
        client = SimpleNamespace(retrieve_knowledge_hits=AsyncMock(return_value=[
            ChildHit(p, "kb", p, rank, .02, "rrf", "rrf", file_hash=p)
            for rank, p in enumerate(("a", "b"), 1)]))
        trace = RunTrace(1, "r", "q", "kb")
        with trace.activate(), patch.object(tools, "get_stream_writer", return_value=lambda _: None), \
                patch.object(tools, "get_milvus_client", AsyncMock(return_value=client)), \
                patch.object(tools, "get_postgresql_client", AsyncMock(return_value=database)), \
                patch.object(tools, "rerank_documents", AsyncMock(return_value={"output": {"results": [
                    {"index": 1, "relevance_score": .9}, {"index": 0, "relevance_score": .8}]}})):
            await tools.search_knowledge_base.coroutine("q", SimpleNamespace(context=ContextSchema(1, "kb", 2)))
        self.assertEqual([c["source_id"] for c in trace.contexts if c["stage"] == "final"], ["b", "a"])


class StreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_before_first_model_call_is_persisted(self):
        import agent
        store = SimpleNamespace(save_run=AsyncMock())
        trace = RunTrace(1, "cancel", "q", "kb")
        stream = agent.stream_agent_response("q", "kb", 1, trace=trace, store=store)
        self.assertEqual(json.loads(await anext(stream))["type"], "run")
        await stream.aclose()
        self.assertEqual(store.save_run.call_args.args[0]["status"], "cancelled")
        self.assertIsNone(current_trace())

    async def test_error_produces_metrics_and_closes_trace(self):
        import agent
        trace = RunTrace(1, "failed", "q", "kb")
        store = SimpleNamespace(save_run=AsyncMock())
        with patch.object(agent.AsyncRedisSaver, "from_conn_string", side_effect=RuntimeError("offline")):
            events = [json.loads(line) async for line in agent.stream_agent_response("q", "kb", 1, trace=trace, store=store)]
        self.assertEqual([e["type"] for e in events], ["run", "error", "metrics", "done"])
        self.assertEqual(events[-1]["status"], "failed")
        self.assertTrue(events[-2]["persisted"])


@unittest.skipUnless(os.getenv("ECHOMIND_TEST_DATABASE_URL"), "Set ECHOMIND_TEST_DATABASE_URL for PostgreSQL integration checks")
class StoreIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import asyncpg
        from evaluation_store import EvaluationStore
        self.schema = "evaluation_test_" + uuid4().hex
        self.admin = await asyncpg.connect(os.environ["ECHOMIND_TEST_DATABASE_URL"])
        await self.admin.execute(f'CREATE SCHEMA "{self.schema}"')
        self.pool = await asyncpg.create_pool(os.environ["ECHOMIND_TEST_DATABASE_URL"], min_size=1, max_size=3,
                                              server_settings={"search_path": self.schema})
        self.store = EvaluationStore(self.pool)
        await self.store.initialize()

    async def asyncTearDown(self):
        await self.pool.close()
        await self.admin.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        await self.admin.close()

    async def test_dedup_claim_lease_ownership_and_restart_recovery(self):
        run = sample_run()
        await self.store.save_run(run)
        payload = {"metrics": ["recall_at_k"], "config": {"model": "test"}}
        a = await self.store.create_job(run["run_id"], 1, payload)
        b = await self.store.create_job(run["run_id"], 1, payload)
        self.assertEqual(a["job_id"], b["job_id"])
        first, second = await asyncio.gather(self.store.claim(), self.store.claim())
        active = first or second
        self.assertTrue(bool(first) != bool(second))
        await self.pool.execute("UPDATE evaluation_jobs SET locked_until=now()-interval '1 second'")
        recovered = await self.store.claim()
        self.assertEqual(recovered["attempts"], 2)
        self.assertNotEqual(recovered["lease_id"], active["lease_id"])
        self.assertEqual(await self.store.complete(active, {"scores": {}}), "UPDATE 0")
        self.assertEqual(await self.store.complete(recovered, {"scores": {}}), "UPDATE 1")
        self.assertIsNone(await self.store.get_job(a["job_id"], 2))
        await self.pool.execute("UPDATE evaluation_jobs SET result=$1::jsonb WHERE job_id=$2",
                                json.dumps({"scores": {"faithfulness": {"status": "failed", "value": None}}}), recovered["job_id"])
        retried = await self.store.retry_job(recovered["job_id"], 1)
        self.assertEqual(retried["status"], "queued")
        self.assertIsNone(retried["result"])
        self.assertEqual((await self.store.claim())["attempts"], 3)

    async def test_api_scope_validation_and_session_restore(self):
        import httpx
        from fastapi import FastAPI
        import api
        app = FastAPI()
        app.include_router(api.router)
        app.state.evaluation_store = self.store
        run = sample_run()
        await self.store.save_run(run)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            self.assertEqual((await client.get(f"/runs/{run['run_id']}?user_id=2")).status_code, 404)
            self.assertEqual((await client.get("/runs/not-a-uuid?user_id=1")).status_code, 422)
            result = await client.post(f"/runs/{run['run_id']}/evaluations?user_id=1", json={"metrics": ["recall_at_k"]})
            self.assertEqual(result.status_code, 202)
            history = (await client.get("/runs?user_id=1&session_id=unit")).json()
            self.assertEqual(history[0]["evaluation_job_id"], result.json()["job_id"])
            bad = await client.post(f"/runs/{run['run_id']}/evaluations?user_id=1", json={"metrics": ["arbitrary_metric"]})
            self.assertEqual(bad.status_code, 422)


if __name__ == "__main__":
    unittest.main()

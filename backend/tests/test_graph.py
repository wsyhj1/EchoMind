import asyncio
from dataclasses import replace
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from graph.schemas import Extraction, GraphPath, GraphRecall, validated
from graph.settings import GraphSettings
from graph.fusion import retain_paths
from rag.retrieval_service import RetrievalService
from rag.types import ChildHit, RetrievalCandidate, RetrievalOptions, RetrievalScope


def extraction(subject="订单系统", target="赤霄缓存集群", predicate="USES", quote=None,
               subject_type="System", target_type="CacheCluster", factuality="asserted"):
    return Extraction.model_validate({"entities": [
        {"local_id": "a", "name": subject, "type": subject_type},
        {"local_id": "b", "name": target, "type": target_type}],
        "relations": [{"subject_local_id": "a", "object_local_id": "b", "predicate": predicate,
                       "evidence_quote": quote or f"{subject}使用{target}。", "factuality": factuality}]})


class GraphValidationTests(unittest.TestCase):
    def test_quotes_endpoints_and_current_affirmation_are_required(self):
        text = "订单系统使用赤霄缓存集群。"
        good, dropped = validated(extraction(), text)
        self.assertEqual((len(good.relations), dropped), (1, 0))
        for raw in (extraction(quote="模型虚构的引用"), extraction(factuality="planned"),
                    extraction(quote="订单系统不使用赤霄缓存集群。")):
            got, dropped = validated(raw, text + "订单系统不使用赤霄缓存集群。")
            self.assertEqual((len(got.relations), dropped), (0, 1))
        raw = extraction()
        raw.entities[1].name = "另一缓存"
        self.assertEqual(len(validated(raw, text)[0].relations), 0)

    def test_schema_rejects_injected_predicates_duplicate_ids_and_generic_instances(self):
        data = extraction().model_dump()
        data["relations"][0]["predicate"] = "DROP TABLE"
        with self.assertRaises(ValueError):
            validated(data, "订单系统使用赤霄缓存集群。")
        data = extraction().model_dump()
        data["entities"][1]["local_id"] = "a"
        with self.assertRaises(ValueError):
            validated(data, "订单系统使用赤霄缓存集群。")
        self.assertFalse(validated(extraction(target="Redis"), "订单系统使用Redis。")[0].relations)

    def test_settings_enforce_hard_bounds_and_false_defaults(self):
        with patch.dict(os.environ, {"GRAPH_MAX_HOPS": "3"}):
            with self.assertRaises(ValueError):
                GraphSettings.from_env()

    def test_generic_qualifiers_do_not_split_instances_but_regions_remain_distinct(self):
        for qualifier in ("独立实例", "实例", "赤霄实例"):
            raw = extraction()
            raw.entities[1].qualifier = qualifier
            got, _ = validated(raw, f"订单系统使用赤霄缓存集群。{qualifier}。")
            self.assertEqual(got.entities[1].qualifier, "")
        raw.entities[1].qualifier = "华东"
        got, _ = validated(raw, "订单系统使用赤霄缓存集群。华东区域。")
        self.assertEqual(got.entities[1].qualifier, "华东")

    def test_temporal_queries_skip_current_graph_and_wrong_domains_are_rejected(self):
        from graph.retriever import relation_templates
        self.assertFalse(relation_templates("2024年赤霄缓存谁维护？"))
        self.assertFalse(relation_templates("支付系统明年谁维护？"))
        raw = extraction()
        raw.entities[1].type = "Team"
        self.assertFalse(validated(raw, "订单系统使用赤霄缓存集群。")[0].relations)


class GraphServiceTests(unittest.IsolatedAsyncioTestCase):
    async def setup_result(self, *, enabled=True, error=None, timeout=False, top_k=2):
        row = lambda pid: dict(parent_id=pid, knowledge_base_id="kb", user_id=1, file_hash=pid,
                               file_name=pid, text=pid)
        milvus = SimpleNamespace(retrieve_knowledge_hits=AsyncMock(return_value=[
            ChildHit(pid, "kb", pid, i, .1, "rrf", "rrf", file_hash=pid) for i, pid in enumerate(("a", "b"), 1)]))
        database = SimpleNamespace(get_parents_with_metadata=AsyncMock(return_value=[row("a"), row("b")]))
        recall = GraphRecall([GraphPath("kb", ["r1", "r2"], ["a", "z"], ["USES", "MAINTAINED_BY"])], [row("a"), row("z")], 1, "query")
        async def recall_fn(*args):
            if timeout:
                await asyncio.sleep(.1)
            if error:
                raise error
            return recall
        retriever = SimpleNamespace(recall=AsyncMock(side_effect=recall_fn))
        async def rank(query, documents, top_n):
            return {"output": {"results": [{"index": documents.index("b"), "relevance_score": .9}]}}
        rerank = AsyncMock(side_effect=rank)
        with patch.dict(os.environ, {"GRAPH_QUERY_TIMEOUT_MS": "10"}):
            result = await RetrievalService(milvus, database, rerank, retriever).retrieve_context(
                "订单系统谁维护？", RetrievalScope(1, "kb"), top_k, RetrievalOptions(graph_enabled=enabled))
        return result, retriever

    async def test_complete_graph_path_survives_reranker_and_preserves_sources(self):
        result, _ = await self.setup_result()
        self.assertEqual({c.parent_id for c in result.selected}, {"a", "z"})
        self.assertTrue(result.trace.graph_used)
        self.assertEqual(result.candidates[0].sources, ["rrf", "graph"])
        self.assertEqual(len(result.trace.graph_paths[0]["relation_ids"]), 2)

    async def test_graph_off_never_queries_graph(self):
        result, retriever = await self.setup_result(enabled=False)
        retriever.recall.assert_not_awaited()
        self.assertEqual([c.parent_id for c in result.selected], ["b"])

    async def test_timeout_and_missing_graph_fall_back_to_vector_results(self):
        for kwargs in ({"timeout": True}, {"error": RuntimeError("missing graph")}):
            result, _ = await self.setup_result(**kwargs)
            self.assertEqual([c.parent_id for c in result.selected], ["b"])
            self.assertFalse(result.trace.graph_used)
            self.assertTrue(result.trace.fallback_reasons[0].startswith("graph_"))

    async def test_two_parent_path_is_not_forced_into_top_one(self):
        result, _ = await self.setup_result(top_k=1)
        self.assertEqual(len(result.selected), 1)
        self.assertEqual(result.selected[0].parent_id, "b")


if __name__ == "__main__":
    unittest.main()

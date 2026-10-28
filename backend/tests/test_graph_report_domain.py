"""Report ontology tests use grammar examples, never holdout answers or clouds."""
import asyncio
from dataclasses import replace
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from graph.extractor import GraphExtractor
from graph.schemas import DOMAINS, PREDICATES, validated
from graph.semantic_guard import quote_supports_relation
from graph.settings import GraphSettings
from graph.retriever import relation_templates


CASES = [
    ("Product", "产品甲", "HAS_FEATURE", "Feature", "功能乙", "产品甲具有功能乙。"),
    ("Product", "产品甲", "HAS_MODULE", "Module", "模块丙", "产品甲包含模块丙。"),
    ("Feature", "功能乙", "IMPLEMENTED_BY", "Module", "模块丙", "功能乙由模块丙实现。"),
    ("Module", "模块丙", "EVALUATED_ON", "Dataset", "数据集丁", "模块丙在数据集丁上评估。"),
    ("Experiment", "实验戊", "REPORTS_METRIC", "Metric", "宏平均指标己", "实验戊报告宏平均指标己。"),
    ("Product", "产品甲", "HAS_EXPERIMENT", "Experiment", "实验戊", "产品甲开展实验戊。"),
    ("Module", "模块丙", "USES", "Model", "模型庚", "模块丙使用模型庚。"),
]


def extraction(case):
    st, subject, predicate, tt, target, quote = case
    return {"entities": [{"local_id": "s", "name": subject, "type": st},
                         {"local_id": "t", "name": target, "type": tt}],
            "relations": [{"subject_local_id": "s", "predicate": predicate, "object_local_id": "t",
                           "evidence_quote": quote, "factuality": "asserted"}]}


class ReportOntologyTests(unittest.TestCase):
    def test_explicit_module_composition_requires_complete_named_list_items(self):
        quote = '平台甲由页面导航模块、合成案例、审计与导出模块组成。'
        for name in ('页面导航模块', '审计与导出模块'):
            self.assertTrue(quote_supports_relation('平台甲', 'HAS_MODULE', name, quote,
                subject_type='System', target_type='Module'))
        for name in ('导航模块', '导出模块', '合成案例'):
            self.assertFalse(quote_supports_relation('平台甲', 'HAS_MODULE', name, quote,
                subject_type='System', target_type='Module'))
        for bad in ('平台甲计划由页面导航模块、审计模块组成。',
                    '例如，平台甲由页面导航模块、审计模块组成。',
                    '平台甲不由页面导航模块、审计模块组成。',
                    '平台甲由页面导航模块、审计模块组成的方案尚未实现。',
                    '平台甲由页面导航模块与其他系统共同组成。'):
            self.assertFalse(quote_supports_relation('平台甲', 'HAS_MODULE', '页面导航模块', bad,
                subject_type='System', target_type='Module'), bad)
        self.assertFalse(quote_supports_relation('平台甲', 'HAS_MODULE', '样本集乙',
            '平台甲包含样本集乙。', subject_type='System', target_type='Module'))
        self.assertFalse(quote_supports_relation('平台甲', 'HAS_MODULE', '模块丙',
            '平台甲由模块乙组成，平台丁由模块丙组成。', subject_type='System', target_type='Module'))
        self.assertTrue(quote_supports_relation('平台甲', 'HAS_MODULE', '案例管理模块',
            '平台甲由案例管理模块、审计模块组成。', subject_type='System', target_type='Module'))

    def test_named_report_relations_are_directional_and_typed(self):
        for case in CASES:
            with self.subTest(predicate=case[2]):
                st, subject, predicate, tt, target, quote = case
                self.assertTrue(quote_supports_relation(subject, predicate, target, quote, subject_type=st, target_type=tt))
                self.assertFalse(quote_supports_relation(target, predicate, subject, quote, subject_type=tt, target_type=st))
                self.assertEqual(len(validated(extraction(case), quote)[0].relations), 1)
        self.assertEqual(set(PREDICATES), set(DOMAINS))

    def test_co_mentions_tables_negated_planned_and_historical_claims_are_rejected(self):
        cases = [
            ("功能乙", "IMPLEMENTED_BY", "模块丙", "功能乙和模块丙出现在作品概览。"),
            ("模块丙", "EVALUATED_ON", "数据集丁", "模块丙 | 数据集丁 | 0.91"),
            ("产品甲", "HAS_FEATURE", "功能乙", "产品甲尚未实现功能乙。"),
            ("产品甲", "HAS_FEATURE", "功能乙", "产品甲计划支持功能乙。"),
            ("实验戊", "REPORTS_METRIC", "指标己", "实验戊未报告指标己。"),
            ("模块丙", "EVALUATED_ON", "数据集丁", "例如模块丙在数据集丁上评估。"),
            ("模块丙", "EVALUATED_ON", "数据集丁", "模块丙可能在数据集丁上评估。"),
            ("模块丙", "EVALUATED_ON", "数据集丁", "2024年模块丙在数据集丁上评估。"),
            ("实验戊", "REPORTS_METRIC", "指标己", "实验戊的参考文献介绍指标己。"),
        ]
        for subject, predicate, target, quote in cases:
            with self.subTest(quote=quote):
                self.assertFalse(quote_supports_relation(subject, predicate, target, quote))

    def test_dataset_is_not_service_and_metric_is_not_a_numeric_value(self):
        self.assertFalse(quote_supports_relation("产品甲", "USES", "数据集丁", "产品甲使用数据集丁。", subject_type="Product", target_type="Service"))
        bad = extraction(CASES[3])
        bad["entities"][1]["type"] = "Service"
        self.assertEqual(validated(bad, CASES[3][-1])[0].relations, [])
        generic = extraction(CASES[3])
        generic["entities"][1]["name"] = "数据集"
        self.assertEqual(validated(generic, CASES[3][-1])[0].relations, [])
        self.assertEqual(DOMAINS["REPORTS_METRIC"][1], {"Metric"})
        self.assertFalse(quote_supports_relation("实验戊", "REPORTS_METRIC", "0.91", "实验戊报告0.91。", subject_type="Experiment", target_type="Metric"))

    def test_unknown_numeric_version_and_conflict_questions_stay_text_only(self):
        for query in ("模块丙的F1具体数值多少？", "版本v1的功能由谁实现？", "比较两个实验结果", "这些结果冲突吗？", "它呢？"):
            self.assertEqual(relation_templates(query), [])
        self.assertIn(("HAS_FEATURE", "IMPLEMENTED_BY"), relation_templates("产品甲的功能如何实现？"))
        self.assertIn(("EVALUATED_ON",), relation_templates("模块丙在哪个数据集上评估？"))
        self.assertIn(("REPORTS_METRIC",), relation_templates("实验戊报告哪些指标？"))
        self.assertIn(("HAS_FEATURE", "IMPLEMENTED_BY"), relation_templates("实验对比功能由什么实现？"))

    def test_v3_reads_v2_but_explicit_v2_does_not_read_v3(self):
        self.assertEqual(GraphSettings().readable_versions, ("v3", "v2"))
        self.assertEqual(replace(GraphSettings(), version="v2").readable_versions, ("v2",))
        self.assertEqual(replace(GraphSettings(), version="custom").readable_versions, ("custom",))

    def test_migration_keeps_old_allowlist_and_adds_domain_predicates(self):
        sql = (Path(__file__).resolve().parents[1] / "migrations/006_graph_report_domain.sql").read_text(encoding="utf-8")
        for predicate in PREDICATES:
            self.assertIn("'" + predicate + "'", sql)


class ExtractionBudgetHookTests(unittest.IsolatedAsyncioTestCase):
    async def test_reservation_failure_is_not_retried_as_a_model_failure(self):
        create = AsyncMock()
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        reserve = AsyncMock(side_effect=RuntimeError("budget exhausted"))
        with self.assertRaisesRegex(RuntimeError, "budget exhausted"):
            await GraphExtractor(GraphSettings(), client=client, before_request=reserve).extract({"text": "test"})
        reserve.assert_awaited_once()
        create.assert_not_awaited()

    async def test_each_schema_retry_reserves_one_physical_request(self):
        bad = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"properties":{}}'))])
        good = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"entities":[],"relations":[]}'))])
        create = AsyncMock(side_effect=[bad, good])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        reserve = AsyncMock()
        result, rejected = await GraphExtractor(GraphSettings(), client=client, before_request=reserve).extract({"text": "No supported relation"})
        self.assertEqual(len(result.relations), 0)
        self.assertEqual(reserve.await_count, 2)
        self.assertEqual(create.await_count, 2)


if __name__ == "__main__":
    unittest.main()

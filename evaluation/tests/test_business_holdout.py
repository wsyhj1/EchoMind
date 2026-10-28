"""Holdout integrity checks, never fit graph grammar to held-out answers."""
from hashlib import sha256
import json
from pathlib import Path
import unittest

from evaluation.business_holdout_fixtures import AS_OF, VERSION, dataset, documents


class BusinessHoldoutIntegrityTests(unittest.TestCase):
    def test_all_sources_are_contiguous_and_authorized(self):
        docs = {row["doc_id"]: row for row in documents()}
        rows = dataset()
        self.assertEqual(len(rows), 32)
        self.assertEqual(len({row["question_id"] for row in rows}), 32)
        for case in rows:
            with self.subTest(question=case["question_id"]):
                self.assertEqual(case["corpus_version"], VERSION)
                self.assertEqual(case["as_of"], AS_OF)
                self.assertFalse(case["human_reviewed"])
                self.assertIn(case["gold_review"], ("agent_draft", "agent_reviewed"))
                self.assertEqual(bool(case["gold_sources"]), case["answerable"])
                for ref in case["gold_sources"]:
                    doc = docs[ref["doc_id"]]
                    self.assertIn(ref["quote"], doc["text"])
                    self.assertEqual(doc["user_id"], case["user_id"])
                    if case["knowledge_base_id"] != "默认知识库":
                        self.assertEqual(doc["knowledge_base_id"], case["knowledge_base_id"])

    def test_holdout_covers_missing_baseline_boundaries_and_keeps_unknowns(self):
        rows = dataset()
        categories = {case["category"] for case in rows}
        self.assertTrue({"long_report", "cross_section", "table_row", "same_time_conflict", "ambiguous_alias",
                         "historical", "multi_turn", "unanswerable", "tenant_isolation", "kb_isolation"} <= categories)
        self.assertGreater(max(len(doc["text"]) for doc in documents()), 1500)
        self.assertGreaterEqual(sum(not row["answerable"] for row in rows), 6)
        self.assertTrue(all(case["history"] for case in rows if case["category"].startswith("multi_turn")))
        self.assertTrue(any(len(case["gold_sources"]) >= 2 for case in rows if case["answer_mode"] == "conflict"))
        text = json.dumps(rows + documents(), ensure_ascii=False)
        self.assertTrue(all(old not in text for old in ("赤霄", "青岚", "白泽", "玄鹤", "连接池耗尽", "主从切换")))

    def test_checked_in_candidate_fingerprints_and_no_live_accuracy_claim(self):
        folder = Path(__file__).resolve().parents[1] / "datasets"
        manifest = json.loads((folder / "business_holdout_v1.manifest.json").read_text(encoding="utf-8"))
        for key, filename in (("sources_sha256", "business_holdout_v1.sources.json"), ("dataset_sha256", "business_holdout_v1.jsonl")):
            self.assertEqual(sha256((folder / filename).read_bytes()).hexdigest(), manifest[key])
        self.assertTrue(manifest["not_a_live_accuracy_result"])
        self.assertEqual(manifest["new_model_calls"], 0)
        self.assertEqual(manifest["new_retrieval_calls"], 0)


if __name__ == "__main__":
    unittest.main()

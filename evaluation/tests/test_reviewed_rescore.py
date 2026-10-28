from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from evaluation.rag_metrics import METRICS, evidence_metrics
from evaluation.rescore_reviewed import ARMS, audit, write_report


class ReviewedRescoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.original_path = self.folder / "v1.jsonl"
        self.reviewed_path = self.folder / "v2.jsonl"
        self.state_path = self.folder / "corpus.json"
        self.runs = self.folder / "runs"
        self.runs.mkdir()
        self.sources = {key: {"parent_id": key, "user_id": 1, "knowledge_base_id": "kb", "file_hash": key,
            "text_sha256": sha256(text.encode()).hexdigest()} for key, text in (("a", "fact A"), ("b", "fact B"))}
        self.quotes = {"a": "fact A", "b": "fact B"}
        self.original = []
        for qid, refs, answerable in (("Q079", ["a", "b"], True), ("Q083", ["a", "b"], True),
                                      ("Q082", ["a"], True), ("Q093", [], False), ("Q096", [], False)):
            self.original.append({"question_id": qid, "query": qid+" original query", "user_id": 1,
                "knowledge_base_id": "kb", "category": "fact" if answerable else "unanswerable",
                "corpus_version": "fixture", "answerable": answerable,
                "gold_sources": [{"doc_id": ref, "quote": self.quotes[ref]} for ref in refs]})
        self.reviewed = deepcopy(self.original)
        for row in self.reviewed:
            row.update(gold_review="agent_reviewed", human_reviewed=False, dataset_version="v2", review_version="review1", as_of="2026-10-23")
            if row["question_id"] in ("Q082", "Q093"):
                row["query"] += " edited"
            elif row["question_id"] in ("Q079", "Q083"):
                target = "a" if row["question_id"] == "Q079" else "b"
                row["gold_sources"] = [ref for ref in row["gold_sources"] if ref["doc_id"] == target]
        self.write_jsonl(self.original_path, self.original)
        self.write_jsonl(self.reviewed_path, self.reviewed)
        state = {"status": "ready", "schema": "fixture", "collection": "fixture", "corpus_version": "fixture", "source_map": self.sources}
        self.state_path.write_text(json.dumps(state), encoding="utf-8")
        self.manifest = {"dataset_sha256": sha256(self.original_path.read_bytes()).hexdigest(),
            "corpus_schema": "fixture", "collection": "fixture", "corpus_version": "fixture",
            "corpus_digest": sha256(json.dumps(self.sources, sort_keys=True).encode()).hexdigest(),
            "top_k": 5, "models": {"test": "no-model"}, "runtime_source_sha256": {"file": "same"},
            "benchmark_source_sha256": {"eval": "same"}, "rerank_enabled": True, "retrieval_mode": "hybrid"}
        for arm, mode in ARMS.items():
            rows = []
            for case in self.original:
                refs = [ref["doc_id"] for ref in case["gold_sources"]]
                selected = (["b"] if case["question_id"] == "Q083" else ["a"]) if case["answerable"] else []
                contexts = [{"stage": "final", "parent_id": ref, "query": case["query"], "user_id": 1,
                    "knowledge_base_id": "kb", "file_hash": ref, "text": self.quotes[ref]} for ref in selected]
                rows.append({"question_id": case["question_id"], "mode": mode, "category": case["category"],
                    "status": "completed", "final_parent_ids": selected, "gold_parent_ids": refs, "contexts": contexts,
                    "metrics": evidence_metrics(selected, refs, 5, answerable=case["answerable"])})
            self.write_jsonl(self.runs / f"{arm}.jsonl", rows)
            (self.runs / f"{arm}.manifest.json").write_text(json.dumps({**self.manifest, "mode": mode}), encoding="utf-8")

    @staticmethod
    def write_jsonl(path, rows):
        path.write_text("".join(json.dumps(row)+"\n" for row in rows), encoding="utf-8")

    def run_audit(self):
        return audit(self.original_path, self.reviewed_path, self.state_path, self.runs)

    def change_raw(self, arm, action):
        path = self.runs / f"{arm}.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        action(rows)
        self.write_jsonl(path, rows)

    def test_scores_only_identical_queries_without_network_and_preserves_original_values(self):
        before = {path: path.read_bytes() for path in self.runs.iterdir()}
        with patch("socket.socket.connect", side_effect=AssertionError("network forbidden")):
            result = self.run_audit()
        self.assertEqual(result["same_query_case_count"], 3)
        self.assertEqual(result["same_query_answerable_count"], 2)
        self.assertEqual(result["requires_rerun_ids"], ["Q082", "Q093"])
        self.assertEqual(result["new_model_calls"], 0)
        for data in result["arms"].values():
            for entry in data["annotation_change_spotchecks"]:
                self.assertEqual(entry["original_metrics"]["recall_at_k"], .5)
                self.assertEqual(entry["reviewed_metrics"]["recall_at_k"], 1)
            for entry in data["questions"]:
                if entry["question_id"] in ("Q082", "Q093"):
                    self.assertEqual(entry["status"], "requires_rerun")
                    self.assertIsNone(entry["reviewed_metrics"])
                if entry["question_id"] == "Q096":
                    self.assertEqual(entry["reviewed_metrics"], dict.fromkeys(METRICS))
        self.assertTrue(all(path.read_bytes() == value for path, value in before.items()))

    def test_changed_user_kb_answerability_or_extra_query_is_rejected(self):
        for field, value in (("user_id", 2), ("knowledge_base_id", "other"), ("answerable", False), ("query", "different")):
            with self.subTest(field=field):
                rows = deepcopy(self.reviewed)
                rows[0][field] = value
                self.write_jsonl(self.reviewed_path, rows)
                with self.assertRaisesRegex(ValueError, "binding changed|changed query set"):
                    self.run_audit()
        self.write_jsonl(self.reviewed_path, self.reviewed)

    def test_original_manifest_cannot_bind_a_different_dataset(self):
        path = self.runs / "A.manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["dataset_sha256"] = "different"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "dataset_sha256"):
            self.run_audit()

    def test_original_score_must_reproduce_before_any_new_score(self):
        self.change_raw("B", lambda rows: rows[0]["metrics"].update(recall_at_k=.999))
        with self.assertRaisesRegex(ValueError, "Original Gold/score cannot be reproduced"):
            self.run_audit()

    def test_selected_context_must_match_original_query_and_scope(self):
        self.change_raw("C", lambda rows: rows[0]["contexts"][0].update(query="other query"))
        with self.assertRaisesRegex(ValueError, "query/user binding mismatch"):
            self.run_audit()

    def test_out_of_scope_source_or_modified_quote_cannot_be_reused(self):
        for change in ({"knowledge_base_id": "other"}, {"quote": "invented quote"}):
            with self.subTest(change=change):
                rows = deepcopy(self.reviewed)
                if "quote" in change:
                    rows[0]["gold_sources"][0].update(change)
                else:
                    rows[0]["knowledge_base_id"] = change["knowledge_base_id"]
                self.write_jsonl(self.reviewed_path, rows)
                with self.assertRaises(ValueError):
                    self.run_audit()

    def test_failed_original_is_retained_but_not_assigned_a_new_fabricated_score(self):
        def fail(rows):
            rows[0].update(status="failed", metrics=dict.fromkeys(METRICS))
            rows[0].pop("final_parent_ids")
        self.change_raw("D", fail)
        result = self.run_audit()
        data = result["arms"]["D"]
        self.assertEqual(data["reviewed_gold_same_query_summary"]["overall"]["cases"], 3)
        self.assertEqual(data["reviewed_gold_same_query_summary"]["overall"]["failed"], 1)
        entry = data["questions"][0]
        self.assertEqual(entry["status"], "original_run_failed_unscorable")
        self.assertEqual(entry["reviewed_metrics"], dict.fromkeys(METRICS))

    def test_report_cannot_overwrite_protected_original_manifest(self):
        path = self.runs / "A.manifest.json"
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "overwrite original inputs"):
            write_report(self.run_audit(), path)
        self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()

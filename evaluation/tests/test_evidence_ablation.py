import json
from pathlib import Path
import tempfile
import unittest

from evaluation.ablation import ARMS, report


class AblationReportTests(unittest.TestCase):
    def setup_runs(self, folder):
        base = {"dataset_sha256": "data", "corpus_schema": "schema", "collection": "collection", "corpus_version": "v1",
                "corpus_digest": "corpus", "models": {"model": "m"}, "top_k": 5, "rerank_enabled": True,
                "retrieval_mode": "hybrid", "runtime_source_sha256": {"backend.py": "same"}}
        (folder / "execution.json").write_text(json.dumps({"concurrency": 1}), encoding="utf-8")
        row = {"question_id": "Q1", "category": "unanswerable", "status": "failed", "gold_parent_ids": [],
               "final_parent_ids": [], "metrics": {}, "steps_ms": {"total": 10}}
        for arm, mode in ARMS.items():
            path = folder / f"{arm}.jsonl"
            path.write_text(json.dumps(row) + "\n", encoding="utf-8")
            path.with_suffix(".manifest.json").write_text(json.dumps({**base, "mode": mode}), encoding="utf-8")

    def test_unanswerable_or_failed_cases_stay_unavailable_and_are_not_dropped(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            self.setup_runs(folder)
            result = report(folder)
            self.assertIsNone(result["answer_accuracy"])
            for group in result["results"].values():
                self.assertEqual(group["overall"]["failed"], 1)
                self.assertEqual(group["overall"]["cases"], 1)
                self.assertIsNone(group["overall"]["metrics"]["recall_at_k"]["mean"])
            self.assertIn("unavailable", (folder / "phase3_ablation_report.md").read_text(encoding="utf-8"))

    def test_source_changes_in_one_arm_invalidate_the_comparison(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            self.setup_runs(folder)
            path = folder / "D.manifest.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data["runtime_source_sha256"]["backend.py"] = "changed"
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Source changed"):
                report(folder)


if __name__ == "__main__":
    unittest.main()

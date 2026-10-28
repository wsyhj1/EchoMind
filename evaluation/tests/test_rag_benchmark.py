import json
from pathlib import Path
import tempfile
import unittest

from evaluation.compare_retrieval import compare
from evaluation.rag_corpus import validate_state
from evaluation.rag_fixtures import build_files
from evaluation.retrieval import frozen_module, load_cases


def temporary_directory():
    folder = Path(__file__).resolve().parents[2] / ".runtime/rag_accuracy/test-temp"
    folder.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=folder)


class BenchmarkTests(unittest.TestCase):
    def test_pdf_docx_regeneration_is_byte_stable_and_text_is_intact(self):
        import docx2txt
        import fitz
        from evaluation.rag_corpus import normalize
        with temporary_directory() as folder:
            first = build_files(Path(folder))
            second = build_files(Path(folder))
            self.assertEqual([d["file_hash"] for d in first], [d["file_hash"] for d in second])
            for document in second:
                if document["format"] == "docx":
                    extracted = docx2txt.process(document["path"])
                else:
                    with fitz.open(document["path"]) as pdf:
                        extracted = "".join(page.get_text() for page in pdf)
                self.assertIn(normalize(document["text"]), normalize(extracted))

    def test_cleanup_namespace_guard_rejects_public_and_sql_injection(self):
        for name in ("public", "knowledge_base_collection", "rag_accuracy_bad", 'rag_accuracy_123456789abc";DROP SCHEMA public CASCADE;--'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_state({"schema": name, "collection": name})
        validate_state({"schema": "rag_accuracy_123456789abc", "collection": "rag_accuracy_abcdef012345"})

    def test_frozen_source_cannot_silently_be_replaced_by_new_implementation(self):
        with temporary_directory() as folder:
            path = Path(folder) / "tools.py"
            path.write_text("changed = True", encoding="utf-8")
            with self.assertRaises(ValueError):
                frozen_module("tools", Path(folder), {"source_sha256": {"backend/tools.py": "wrong-hash"}})

    def test_dataset_rejects_duplicate_ids_and_answerable_without_gold(self):
        row = {"question_id": "q", "query": "q", "user_id": 1, "knowledge_base_id": "kb",
               "gold_sources": [], "answerable": True}
        with temporary_directory() as folder:
            path = Path(folder) / "cases.jsonl"
            path.write_text(json.dumps(row), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_cases(path)
            row["answerable"] = False
            path.write_text(json.dumps(row) + "\n" + json.dumps(row), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_cases(path)

    def test_paired_comparison_rejects_different_budgets_partial_runs_and_changed_gold(self):
        manifest = {"dataset_sha256": "dataset", "corpus_schema": "corpus", "collection": "corpus",
                    "corpus_version": "v1", "corpus_digest": "digest", "models": {"model": "m"},
                    "top_k": 5, "rerank_enabled": True, "retrieval_mode": "hybrid"}
        row = {"question_id": "q", "status": "completed", "category": "fact", "gold_parent_ids": ["a"],
               "final_parent_ids": ["a"], "metrics": {"complete_evidence_at_k": 1.0}}
        with temporary_directory() as folder:
            a, b = Path(folder) / "a.jsonl", Path(folder) / "b.jsonl"
            for path in (a, b):
                path.write_text(json.dumps(row), encoding="utf-8")
                path.with_suffix(".manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            report = compare(a, b)
            self.assertIn("待人工审核", report)
            b.with_suffix(".manifest.json").write_text(json.dumps({**manifest, "top_k": 6}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "top_k"):
                compare(a, b)
            b.with_suffix(".manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            b.write_text("", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "identical question IDs"):
                compare(a, b)
            b.write_text(json.dumps({**row, "gold_parent_ids": ["b"]}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Gold differs"):
                compare(a, b)


if __name__ == "__main__":
    unittest.main()

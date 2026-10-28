"""Executable holdout preflight, exact span unions and no Gold prompt leakage."""
from contextlib import redirect_stderr
from copy import deepcopy
from hashlib import sha256
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from evaluation.business_holdout_runner import (
    DEFAULT_DATASET, DEFAULT_MANIFEST, DEFAULT_SOURCES, canonical_messages,
    dry_run_plan, load_bundle, main, require_capabilities, runtime_request,
    score_selected_spans, split_sources, validate_span,
)


class BusinessHoldoutRunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = load_bundle()
        cls.cases = {case["question_id"]: case for case in cls.bundle["cases"]}
        cls.parents = split_sources(cls.bundle["sources"])

    def test_reviewed_manifest_source_spans_and_history_preflight_without_network(self):
        with patch("socket.socket.connect", side_effect=AssertionError("network forbidden")):
            plan = dry_run_plan(self.bundle)
        self.assertEqual(plan["question_count"], 32)
        self.assertEqual(plan["source_document_count"], 12)
        self.assertEqual(plan["history_case_count"], 2)
        self.assertEqual(plan["new_model_calls"], 0)
        self.assertEqual(plan["new_retrieval_calls"], 0)
        self.assertIsNone(plan["answer_accuracy"])
        self.assertIsNone(plan["observed_retrieval_span_recall"])
        self.assertTrue(all(row["offline_mapping_validation"]["all_gold_offsets_reachable"] for row in plan["case_plans"]))

    def test_unsupported_single_query_and_document_only_modes_are_refused(self):
        with self.assertRaisesRegex(ValueError, "BH029:actual_message_history.*BH030:actual_message_history"):
            dry_run_plan(self.bundle, mode="span_single_query")
        with self.assertRaisesRegex(ValueError, "source_span_mapping"):
            dry_run_plan(self.bundle, mode="document_only")
        changed = deepcopy(self.cases["BH002"])
        changed["required_runner_capabilities"].append("future_unknown_capability")
        with self.assertRaisesRegex(ValueError, "future_unknown_capability"):
            require_capabilities([changed], "span_history")

    def test_actual_multi_turn_history_is_preserved_before_current_query(self):
        for qid in ("BH029", "BH030"):
            case = self.cases[qid]
            messages = canonical_messages(case)
            self.assertEqual(messages[:-1], case["history"])
            self.assertEqual(messages[-1], {"role": "user", "content": case["query"]})
            self.assertEqual(len(messages), 3)
        changed = deepcopy(self.cases["BH029"])
        changed["history"] = []
        with self.assertRaisesRegex(ValueError, "History requirement mismatch"):
            canonical_messages(changed)

    def test_runtime_allowlist_never_copies_gold_or_review_evidence(self):
        case = deepcopy(self.cases["BH029"])
        case.update(gold_answer="GOLD_SECRET_SENTINEL", expected_claims=["EXPECTED_SECRET_SENTINEL"],
                    review_evidence=[{"quote": "REVIEW_SECRET_SENTINEL"}], review_note="PRIVATE_REVIEW_NOTE")
        runtime = runtime_request(case)
        self.assertEqual(set(runtime), {"question_id", "user_id", "knowledge_base_id", "as_of", "messages"})
        encoded = json.dumps(runtime)
        for secret in ("GOLD_SECRET_SENTINEL", "EXPECTED_SECRET_SENTINEL", "REVIEW_SECRET_SENTINEL", "PRIVATE_REVIEW_NOTE"):
            self.assertNotIn(secret, encoded)
        self.assertNotIn("gold_sources", runtime)
        self.assertNotIn("answerable", runtime)

    def test_history_cannot_become_system_instruction_or_omit_actual_turns(self):
        case = deepcopy(self.cases["BH029"])
        case["history"][0]["role"] = "system"
        with self.assertRaisesRegex(ValueError, "actual user/assistant"):
            canonical_messages(case)
        case = deepcopy(self.cases["BH029"])
        case["history"].pop()
        with self.assertRaisesRegex(ValueError, "finish with assistant"):
            canonical_messages(case)

    def test_same_document_first_chunk_cannot_count_as_both_long_report_spans(self):
        case = self.cases["BH001"]
        first = next(parent for parent in self.parents.values() if parent.doc_id == "report_overview" and parent.start_char == 0)
        score = score_selected_spans(case, [first.parent_id], self.parents)
        self.assertEqual(score["gold_span_count"], 2)
        self.assertEqual(score["source_span_recall"], .5)
        self.assertEqual(score["complete_evidence"], 0)
        all_doc = [parent.parent_id for parent in self.parents.values() if parent.doc_id == "report_overview"]
        self.assertEqual(score_selected_spans(case, all_doc, self.parents)["complete_evidence"], 1)

    def test_union_spans_across_chunks_without_double_counting_overlap(self):
        text = "0123456789ABCDEF"
        body_hash = sha256(text.encode()).hexdigest()
        sources = {"toy": {"text": text, "user_id": 1, "knowledge_base_id": "kb", "text_sha256": body_hash}}
        parents = split_sources(sources, max_chars=6, overlap=2)
        selected = sorted(parents.values(), key=lambda parent: parent.start_char)
        case = {"user_id": 1, "knowledge_base_id": "kb", "gold_sources": [{"doc_id": "toy", "start_char": 3, "end_char": 12,
            "source_text_sha256": body_hash, "source_span_id": "toy-span"}]}
        partial = score_selected_spans(case, [selected[0].parent_id, selected[1].parent_id, selected[0].parent_id], parents)
        self.assertEqual(partial["source_span_recall"], 0)
        self.assertAlmostEqual(partial["source_character_recall"], 7/9)
        self.assertEqual(partial["complete_evidence"], 0)
        complete = score_selected_spans(case, [parent.parent_id for parent in selected[:3]], parents)
        self.assertEqual(complete["complete_evidence"], 1)
        self.assertEqual(complete["source_character_recall"], 1)

    def test_selected_parents_cannot_cross_tenant_or_selected_library(self):
        case = self.cases["BH002"]
        private = next(parent.parent_id for parent in self.parents.values() if parent.doc_id == "private")
        budget = next(parent.parent_id for parent in self.parents.values() if parent.doc_id == "other_space")
        for parent_id in (private, budget):
            with self.subTest(parent=parent_id), self.assertRaisesRegex(ValueError, "escaped"):
                score_selected_spans(case, [parent_id], self.parents)
        self.assertEqual(score_selected_spans(self.cases["BH027"], [budget], self.parents)["complete_evidence"], 1)
        with self.assertRaisesRegex(ValueError, "escaped"):
            score_selected_spans(self.cases["BH027"], [private], self.parents)

    def test_unknown_selected_parent_is_not_silently_ignored(self):
        with self.assertRaisesRegex(ValueError, "frozen offset catalog"):
            score_selected_spans(self.cases["BH002"], ["not-a-real-parent"], self.parents)

    def test_review_only_clarification_sources_do_not_create_recall_gold(self):
        case = self.cases["BH015"]
        self.assertTrue(case["review_evidence"])
        selected = [parent.parent_id for parent in self.parents.values() if parent.doc_id == "implementation"]
        score = score_selected_spans(case, selected, self.parents)
        self.assertEqual(score["gold_span_count"], 0)
        self.assertIsNone(score["source_span_recall"])
        self.assertIsNone(score["complete_evidence"])

    def test_span_offsets_hashes_and_scope_are_exactly_validated(self):
        case = self.cases["BH002"]
        good = case["gold_sources"][0]
        for field, bad in (("start_char", good["start_char"]+1), ("quote", "invented text"),
                           ("source_text_sha256", "incorrect"), ("source_span_id", "incorrect")):
            ref = {**good, field: bad}
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_span(ref, case, self.bundle["sources"])
        changed_case = {**case, "user_id": 999}
        with self.assertRaisesRegex(ValueError, "out-of-scope"):
            validate_span(good, changed_case, self.bundle["sources"])

    def test_segmentation_is_deterministic_and_preserves_exact_unicode_offsets(self):
        text = "甲🙂乙\r\n丙丁戊"
        sources = {"unicode": {"user_id": 1, "knowledge_base_id": "kb", "text": text, "text_sha256": sha256(text.encode()).hexdigest()}}
        first = split_sources(sources, 4, 1)
        self.assertEqual(first, split_sources(sources, 4, 1))
        for parent in first.values():
            self.assertEqual(parent.text, text[parent.start_char:parent.end_char])
        for params in ((0, 0), (4, 4), (4, -1)):
            with self.assertRaises(ValueError):
                split_sources(sources, *params)

    def test_manifest_fingerprint_tamper_and_draft_provenance_are_rejected(self):
        manifest = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
        for field, value in (("dataset_sha256", "bad"), ("sources_sha256", "bad"),
                             ("review_sha256", "bad"), ("gold_review", "agent_draft")):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "manifest.json"
                path.write_text(json.dumps({**manifest, field: value}), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_bundle(DEFAULT_DATASET, path, DEFAULT_SOURCES)

    def test_live_execution_flag_is_refused_before_any_calls(self):
        with patch("sys.argv", ["business_holdout_runner", "--execute"]), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                main()
        self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()

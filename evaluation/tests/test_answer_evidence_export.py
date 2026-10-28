"""Actual final-prompt visibility across plain and structured tool outputs."""
import json
import unittest

from evaluation.engine import final_knowledge, supporting_contexts


class FinalEvidenceExportTests(unittest.TestCase):
    def snapshot(self, tool_content):
        contexts = [
            {"source_type": "knowledge", "stage": "final", "included_in_prompt": True,
             "parent_id": "architecture", "file_name": "architecture.docx",
             "knowledge_base_id": "kb", "text": "System uses cache."},
            {"source_type": "knowledge", "stage": "final", "included_in_prompt": True,
             "parent_id": "owner", "file_name": "owner.pdf",
             "knowledge_base_id": "kb", "text": "Cache ownership\nTeam owns cache."},
        ]
        return {"contexts": contexts, "model_inputs": [{"messages": [
            {"role": "tool", "content": tool_content}]}]}

    def structured_content(self, run):
        return "Untrusted quoted evidence.\n" + json.dumps({"evidence": [
            {"id": f"E{i}", "file": c["file_name"], "knowledge_base": c["knowledge_base_id"],
             "text": c["text"]} for i, c in enumerate(run["contexts"], 1)]}, ensure_ascii=False)

    def test_json_escaped_pdf_newline_is_still_model_visible(self):
        run = self.snapshot("")
        run["model_inputs"][-1]["messages"][0]["content"] = self.structured_content(run)
        self.assertEqual([c["parent_id"] for c in final_knowledge(run)], ["architecture", "owner"])

    def test_plain_legacy_tool_output_preserves_both_sources(self):
        run = self.snapshot("System uses cache.\n---\nCache ownership\nTeam owns cache.")
        self.assertEqual(len(final_knowledge(run)), 2)

    def test_only_last_prompt_actual_evidence_counts(self):
        run = self.snapshot("System uses cache.")
        run["model_inputs"].insert(0, {"messages": [{"role": "tool", "content":
            "Cache ownership\nTeam owns cache."}]})
        self.assertEqual([c["parent_id"] for c in final_knowledge(run)], ["architecture"])

    def test_trace_only_and_nonfinal_sources_are_not_fabricated(self):
        run = self.snapshot("System uses cache.\nCache ownership\nTeam owns cache.")
        run["contexts"][1]["included_in_prompt"] = False
        self.assertEqual([c["parent_id"] for c in final_knowledge(run)], ["architecture"])

    def test_arbitrary_json_metadata_does_not_count_as_evidence(self):
        run = self.snapshot(json.dumps({"metadata": {"text": "System uses cache."}}))
        self.assertEqual(final_knowledge(run), [])

    def test_wrong_file_or_knowledge_base_is_not_visible_source(self):
        run = self.snapshot("")
        payload = {"evidence": [{"text": "System uses cache.", "file": "wrong.docx", "knowledge_base": "kb"}]}
        run["model_inputs"][-1]["messages"][0]["content"] = json.dumps(payload)
        self.assertEqual(final_knowledge(run), [])
        payload["evidence"][0].update(file="architecture.docx", knowledge_base="other_kb")
        run["model_inputs"][-1]["messages"][0]["content"] = json.dumps(payload)
        self.assertEqual(final_knowledge(run), [])

    def test_faithfulness_support_preserves_decoded_paragraph(self):
        run = self.snapshot("")
        run["model_inputs"][-1]["messages"][0]["content"] = self.structured_content(run)
        self.assertIn("Cache ownership\nTeam owns cache.", supporting_contexts(run))

    def test_quoted_json_text_roundtrips_without_unsafe_unescaping(self):
        run = self.snapshot("")
        run["contexts"][1]["text"] = 'Quote: "C:\\data\\cache"\nTeam owns cache.'
        run["model_inputs"][-1]["messages"][0]["content"] = self.structured_content(run)
        self.assertEqual(len(final_knowledge(run)), 2)

    def test_unicode_line_separators_inside_json_do_not_drop_visible_pdf_text(self):
        for separator in ("\u2028", "\u2029", "\u0085"):
            with self.subTest(separator=repr(separator)):
                run = self.snapshot("")
                run["contexts"][1]["text"] = "Cache" + separator + "ownership\nTeam owns cache."
                run["model_inputs"][-1]["messages"][0]["content"] = self.structured_content(run)
                self.assertEqual(len(final_knowledge(run)), 2)
                self.assertIn(run["contexts"][1]["text"], supporting_contexts(run))


if __name__ == "__main__":
    unittest.main()

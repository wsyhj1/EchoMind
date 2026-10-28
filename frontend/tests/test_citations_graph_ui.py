"""Click real source controls and graph consent flows without external requests."""
from copy import deepcopy
import hashlib
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

from streamlit.testing.v1 import AppTest

FRONTEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FRONTEND))


def response(payload):
    result = MagicMock()
    result.json.return_value = payload
    return result


class CitationGraphUITests(unittest.TestCase):
    def app(self, script):
        app = AppTest.from_string(script, default_timeout=15)
        app.session_state.identity_api_base = "http://localhost:8000"
        app.session_state.identity_token = "test-session-token"
        return app

    def citation_app(self):
        return self.app('from citations_ui import render_answer_citations\n'
                        'render_answer_citations({"run_id":"run", "content":"结论（来源：notes.pdf，E1）", "persisted":True}, "http://localhost:8000", 7)')

    def graph_app(self):
        return self.app('from graph_ui import render_graph_management\n'
                        'render_graph_management("http://localhost:8000",7,"kb")')

    def graph_status(self, status="UNINDEXED"):
        return {"schema_ready": True, "service": {"model": "test-model", "host": "model.example", "target_id": "a" * 64},
                "limits": {"max_parents": 20, "max_calls": 10}, "documents": [
                    {"file_hash": "hash", "file_name": "<script>notes</script>.pdf", "status": status,
                     "parents_completed": 2, "parents_total": 4, "raw_relations": 6,
                     "usable_relations": 1, "rejected_relations": 5, "error_type": None}]}

    def test_citation_click_renders_plaintext_and_rechecks_deleted_source(self):
        snapshot = {"validation_id": "audit", "statistics": {"citation_occurrences": 1},
                    "citations": [{"citation_id": "c1", "file": "notes.pdf", "evidence_id": "E1", "status": "structure_verified"}]}
        detail = {"citation": snapshot["citations"][0], "checked_at": "now",
                  "text": '<script>alert("doc")</script>\n[evil](https://external.invalid)'}
        with patch("requests.get") as get:
            app = self.citation_app().run()
            get.assert_not_called()
            get.side_effect = lambda url, **kw: response(snapshot if url.endswith("/citations") else detail)
            app.button(key="citations_7_run_open_E1").click().run()
            self.assertFalse(app.exception)
            self.assertTrue(any('<script>alert("doc")</script>' in t.value for t in app.text))
            self.assertFalse(any('<script>alert("doc")</script>' in t.value for t in app.markdown))
            self.assertTrue(any("语义未核实" in info.value for info in app.info))
            self.assertNotIn("text", app.session_state["citations_7_run"]["snapshot"]["citations"][0])
            detail.update(citation={**detail["citation"], "status": "source_deleted"}, text=None)
            app.run()
            self.assertFalse(app.exception)
            self.assertFalse(any('<script>alert("doc")</script>' in t.value for t in app.text))
            self.assertTrue(any("来源已删除" in w.value for w in app.warning))
            lookups = get.call_count
            app.button(key="citations_7_run_close").click().run()
            app.run()
            self.assertEqual(get.call_count, lookups)
            self.assertTrue(all(c.kwargs["params"]["user_id"] == 7 for c in get.call_args_list))

    def test_graph_is_lazy_and_submission_requires_explicit_consent(self):
        data = self.graph_status()
        with patch("requests.get", return_value=response(data)) as get, patch("requests.post", return_value=response({})) as post:
            app = self.graph_app().run()
            get.assert_not_called(); post.assert_not_called()
            app.button(key="graph_7_kb_open").click().run()
            self.assertFalse(app.exception)
            self.assertTrue(app.button(key="graph_7_kb_submit_hash").disabled)
            post.assert_not_called()
            consent = next(c for c in app.checkbox if "我同意" in c.label)
            consent.set_value(True).run()
            post.assert_not_called()
            app.button(key="graph_7_kb_submit_hash").click().run()
            self.assertFalse(app.exception)
            self.assertEqual(post.call_args.kwargs["json"], {"user_id": 7, "knowledge_base_id": "kb", "file_hash": "hash", "external_model_consent": True, "consent_target": "a" * 64, "force": False})
            self.assertTrue(any("model.example" in t.value for t in app.text))
            self.assertTrue(any("拒绝关系" in f.value.columns for f in app.dataframe))
            self.assertFalse(any("<script>notes" in t.value for t in app.markdown))

    def test_ready_requires_force_and_target_change_resets_consent(self):
        data = self.graph_status("READY")
        with patch("requests.get", side_effect=lambda *a, **kw: response(data)), patch("requests.post") as post:
            app = self.graph_app().run()
            app.button(key="graph_7_kb_open").click().run()
            next(c for c in app.checkbox if "我同意" in c.label).set_value(True).run()
            self.assertTrue(app.button(key="graph_7_kb_submit_hash").disabled)
            app.checkbox(key="graph_7_kb_force_hash").set_value(True).run()
            self.assertFalse(app.button(key="graph_7_kb_submit_hash").disabled)
            data["service"]["target_id"] = "b" * 64
            app.run()
            self.assertFalse(next(c for c in app.checkbox if "我同意" in c.label).value)
            self.assertTrue(app.button(key="graph_7_kb_submit_hash").disabled)
            post.assert_not_called()

    def test_running_job_can_be_cancelled_without_new_extraction_consent(self):
        with patch("requests.get", return_value=response(self.graph_status("RUNNING"))), patch("requests.post", return_value=response({})) as post:
            app = self.graph_app().run()
            app.button(key="graph_7_kb_open").click().run()
            self.assertFalse(app.checkbox)
            app.button(key="graph_7_kb_cancel_hash").click().run()
            self.assertEqual(post.call_args.args[0], "http://localhost:8000/graph-jobs/cancel")
            self.assertEqual(post.call_args.kwargs["json"], {"user_id": 7, "knowledge_base_id": "kb", "file_hash": "hash"})
            self.assertTrue(any("无法撤回" in item.value for item in app.success))


if __name__ == "__main__":
    unittest.main()

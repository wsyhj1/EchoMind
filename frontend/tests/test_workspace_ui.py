"""Exercise workspace navigation and streaming without model calls or databases."""
import json
from copy import deepcopy
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import MagicMock, patch
import requests
import streamlit as st

from streamlit.testing.v1 import AppTest

FRONTEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FRONTEND))


class WorkspaceUITest(unittest.TestCase):
    def setUp(self):
        original = st.get_option
        listener = patch.object(st, "get_option", side_effect=lambda key: "127.0.0.1" if key == "server.address" else original(key))
        listener.start()
        self.addCleanup(listener.stop)
        directory = patch('conversations_ui._directory', return_value={'items': [], 'next_cursor': None})
        directory.start()
        self.addCleanup(directory.stop)

    def app(self, knowledge_bases=None, history=None):
        app = AppTest.from_file(str(FRONTEND / "app.py"), default_timeout=15)
        scope = next(iter(knowledge_bases or {}), None)
        conversation = {'conversation_id':'00000000-0000-4000-8000-000000000001', 'session_id':'c_fixture',
            'user_id':1, 'scope_id':scope, 'title':'测试会话', 'revision':1,'archived':False,'read_only':False}
        for key, value in {
            "identity_info": {"user_id": 1, "username": "本机用户", "mode": "local"},
            "identity_checked_at": time.monotonic(),
            "initialized": True, "history_restored": True, "data_loading": False,
            "files_loaded": True, "knowledge_bases": knowledge_bases or {},
            "chat_history": history or [],
            "workspace_view": "知识对话",
            "selected_kb": scope, "conversation_ready":True,
            "active_conversation": conversation if scope else None,
            "profile_data": {"user_id": 1, "fields": {"background": "", "goals": "", "interests": "", "preferences": ""},
                             "automatic_summary": "", "personalization_enabled": True, "auto_update_enabled": True,
                             "revision": 1, "source": "empty", "updated_at": None},
            "profile_loaded_at": time.monotonic(),
            "settings_data": self.settings_response(),
            "settings_summary_data": self.settings_response(),
        }.items():
            app.session_state[key] = value
        return app

    def test_empty_workspace_guides_setup(self):
        app = self.app().run()
        self.assertFalse(app.exception)
        self.assertFalse(app.chat_input)
        self.assertTrue(any("创建知识库" in info.value for info in app.info))

    def test_switch_scope_and_start_new_conversation(self):
        app = self.app({"默认知识库": [], "研究资料": ["notes.pdf"]}, [
            {"role": "user", "content": "已有对话", "timestamp": "09:00"},
        ]).run()
        old_session = app.session_state["chat_session_id"]
        app.selectbox(key="kb_selector").select("研究资料").run()
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state["selected_kb"], "研究资料")
        app.button(key="new_chat").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state["chat_history"], [])
        self.assertNotEqual(old_session, app.session_state["chat_session_id"])
        self.assertEqual(app.session_state["selected_kb"], "研究资料")
        self.assertNotIn('conversation', app.query_params)
        self.assertNotIn('session', app.query_params)

    def test_names_are_escaped_in_custom_markup(self):
        app = self.app({'<img src=x onerror=alert(1)>': ['<script>test</script>.pdf']}).run()
        self.assertFalse(app.exception)
        markup = "\n".join(item.value for item in app.markdown)
        self.assertNotIn('<img src=x', markup)
        self.assertIn('&lt;script&gt;', markup)

    def test_cancel_document_delete_keeps_file(self):
        app = self.app({"默认知识库": ["notes.pdf"]}).run()
        app.button(key="del_f_默认知识库_0").click().run()
        self.assertIsNotNone(app.session_state["pending_delete"])
        app.button(key="cancel_del_btn").click().run()
        self.assertFalse(app.exception)
        self.assertIsNone(app.session_state["pending_delete"])
        self.assertEqual(app.session_state["knowledge_bases"]["默认知识库"], ["notes.pdf"])

    def test_streaming_keeps_markdown_and_metadata(self):
        app = self.app({"默认知识库": []}).run()
        response = MagicMock()
        response.__enter__.return_value = response
        response.status_code = 200
        events = [
            {"type": "run", "run_id": "test-run"},
            {"type": "answer", "content": "答案：\n```python\nprint(1)\n```"},
            {"type": "metrics", "run_id": "test-run", "persisted": False,
             "timings": {"server_total_ms": 1200, "ttft_ms": 100, "stages": {}}},
        ]
        response.iter_lines.return_value = [json.dumps({**event,'conversation_id':'00000000-0000-4000-8000-000000000001'}) for event in events]
        with patch("requests.post", return_value=response) as get:
            app.chat_input[0].set_value("测试问题").run()
        self.assertFalse(app.exception)
        self.assertEqual(get.call_count, 1)
        self.assertNotIn("top_k", get.call_args.kwargs["params"])
        self.assertEqual(len(app.chat_message), 2)
        self.assertTrue(any("```python\nprint(1)\n```" in item.value for item in app.markdown))
        self.assertEqual(app.session_state["chat_history"][-1]["run_id"], "test-run")
        self.assertFalse(app.session_state["is_streaming"])

    def test_stream_http_error_leaves_input_usable(self):
        app = self.app({"默认知识库": []}).run()
        response = MagicMock()
        response.__enter__.return_value = response
        response.status_code = 503
        with patch("requests.post", return_value=response):
            app.chat_input[0].set_value("测试问题").run()
        self.assertFalse(app.exception)
        self.assertIn("503", app.session_state["chat_history"][-1]["content"])
        self.assertFalse(app.chat_input[0].disabled)

    def test_evaluation_results_and_cached_evidence_survive_rerun(self):
        chat = {"role": "assistant", "content": "一条回答", "timestamp": "09:00", "run_id": "run-1",
                "persisted": True, "evaluation_job_id": "job-1",
                "timings": {"server_total_ms": 1000, "ttft_ms": 100,
                            "stages": {"retrieval": {"duration_ms": 30, "calls": 1}}}}
        job = {"job_id": "job-1", "status": "completed", "attempts": 1,
               "payload": {"config": {"model": "test-judge"}}, "result": {"scores": {
                   "faithfulness": {"value": 0.8, "status": "completed"},
                   "context_recall": {"value": None, "status": "not_applicable", "reason": "缺少标注"},
               }}}
        snapshot = {"contexts": [{"stage": "final", "source_type": "knowledge", "source_id": "p-1",
                                   "rank": 1, "file_name": "notes.pdf", "text": "证据正文"}]}
        def fake_get(url, **kwargs):
            response = MagicMock()
            response.json.return_value = snapshot if "/runs/" in url else job
            return response
        with patch("requests.get", side_effect=fake_get) as get:
            app = self.app({"默认知识库": []}, [chat]).run()
            self.assertFalse(app.exception)
            app.button(key="evidence_run-1").click().run()
            self.assertFalse(app.exception)
            app.button(key="refresh_eval_run-1").click().run()
            self.assertFalse(app.exception)
        self.assertEqual(sum("/runs/" in call.args[0] for call in get.call_args_list), 1)
        self.assertIn("run-1", app.session_state["run_detail_cache"])
        self.assertTrue(any("父块 ID" in frame.value.columns for frame in app.dataframe))

    def test_evaluation_requires_explicit_submit_and_shows_queue(self):
        chat = {"role": "assistant", "content": "待评测回答", "timestamp": "09:00",
                "run_id": "run-2", "persisted": True}
        response = MagicMock()
        response.json.return_value = {"job_id": "job-2", "status": "queued", "attempts": 0}
        with patch("requests.post", return_value=response) as post, patch("requests.get", return_value=response):
            app = self.app({"默认知识库": []}, [chat]).run()
            self.assertFalse(app.exception)
            post.assert_not_called()
            next(button for button in app.button if button.label == "开始评测").click().run()
            self.assertFalse(app.exception)
            post.assert_called_once()
            self.assertEqual(app.session_state["evaluation_jobs"]["run-2"], "job-2")
            self.assertFalse(app.multiselect)
            app.toggle(key="configure_eval_run-2").set_value(True).run()
            self.assertFalse(app.exception)
            self.assertTrue(app.multiselect)
            post.assert_called_once()

    def test_profile_toggle_persists_server_setting(self):
        app = self.app().run()
        with patch("requests.get", return_value=self.response(deepcopy(app.session_state["profile_data"]))):
            app.button(key="open_profile").click().run()
        updated = deepcopy(app.session_state["profile_data"])
        updated.update(personalization_enabled=False, revision=2)
        response = MagicMock()
        response.json.return_value = updated
        with patch("requests.patch", return_value=response) as patch_request:
            app.toggle(key="settings_profile_use").set_value(False).run()
        self.assertFalse(app.exception)
        self.assertFalse(app.session_state["profile_data"]["personalization_enabled"])
        self.assertEqual(patch_request.call_args.kwargs["json"], {"personalization_enabled": False, "expected_revision": 1})

    def test_profile_editor_saves_manual_fields(self):
        app = self.app().run()
        profile = deepcopy(app.session_state["profile_data"])
        loaded = MagicMock()
        loaded.json.return_value = profile
        saved = MagicMock()
        saved.json.return_value = {**profile, "fields": {**profile["fields"], "preferences": "先给结论"},
                                   "revision": 2, "source": "manual"}
        with patch("requests.get", return_value=loaded), patch("requests.put", return_value=saved) as put:
            app.button(key="open_profile").click().run()
            self.assertFalse(app.exception)
            app.text_area(key="profile_field_preferences").set_value("先给结论")
            next(button for button in app.button if button.label == "保存画像").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(put.call_args.kwargs["json"]["fields"]["preferences"], "先给结论")
        self.assertEqual(app.session_state["profile_data"]["source"], "manual")

    def test_failed_profile_toggle_rolls_back(self):
        app = self.app().run()
        with patch("requests.get", return_value=self.response(deepcopy(app.session_state["profile_data"]))):
            app.button(key="open_profile").click().run()
        response = MagicMock(status_code=503)
        response.raise_for_status.side_effect = requests.HTTPError(response=response)
        with patch("requests.patch", return_value=response):
            app.toggle(key="settings_profile_use").set_value(False).run()
        self.assertFalse(app.exception)
        self.assertTrue(app.toggle(key="settings_profile_use").value)
        self.assertTrue(any("设置未保存" in warning.value for warning in app.warning))

    def test_profile_clear_requires_confirmation_and_updates_local_state(self):
        app = self.app().run()
        profile = deepcopy(app.session_state["profile_data"])
        loaded = MagicMock()
        loaded.json.return_value = profile
        cleared = MagicMock()
        cleared.json.return_value = {**profile, "auto_update_enabled": False, "revision": 2}
        with patch("requests.get", return_value=loaded), patch("requests.delete", return_value=cleared) as delete:
            app.button(key="open_profile").click().run()
            self.assertTrue(app.button(key="clear_profile").disabled)
            delete.assert_not_called()
            app.checkbox(key="confirm_clear_profile").set_value(True).run()
            self.assertFalse(app.exception)
            app.button(key="clear_profile").click().run()
            self.assertFalse(app.exception)
            delete.assert_called_once()
        self.assertFalse(app.session_state["profile_data"]["auto_update_enabled"])
        self.assertFalse(app.session_state["profile_dialog_open"])

    def test_profile_conflict_preserves_draft_and_shows_latest_content(self):
        app = self.app().run()
        profile = deepcopy(app.session_state["profile_data"])
        latest = {**profile, "fields": {**profile["fields"], "preferences": "服务器偏好"}, "source": "manual", "revision": 2}
        loaded, refreshed, conflict = MagicMock(), MagicMock(), MagicMock(status_code=409)
        loaded.json.return_value = profile
        refreshed.json.return_value = latest
        conflict.raise_for_status.side_effect = requests.HTTPError(response=conflict)
        with patch("requests.get", side_effect=[loaded, refreshed]), patch("requests.put", return_value=conflict):
            app.button(key="open_profile").click().run()
            app.text_area(key="profile_field_preferences").set_value("我的草稿")
            next(button for button in app.button if button.label == "保存画像").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.text_area(key="profile_field_preferences").value, "我的草稿")
        self.assertEqual(app.session_state["profile_editor_revision"], 2)
        self.assertTrue(any("服务器偏好" in text.value for text in app.text))

    def settings_response(self, **overrides):
        defaults = {"top_k": 5, "retrieval_mode": "hybrid", "rerank_enabled": True,
                    "graph_enabled": None, "evidence_enabled": None, "memory_enabled": True}
        return {"user_id": 1, "revision": 1, "updated_at": None, "defaults": defaults,
                "values": {**defaults, **overrides},
                "runtime": {"agent_model": "test-chat", "embedding_model": "test-embedding",
                            "rerank_model": "test-rerank", "graph_default_enabled": False,
                            "evidence_default_enabled": False,
                            "graph_extraction_enabled": False}}

    def response(self, data):
        response = MagicMock()
        response.json.return_value = data
        return response

    def test_settings_rag_save_reopen_and_close(self):
        app = self.app().run()
        initial = self.settings_response(memory_enabled=False)
        saved = deepcopy(initial)
        saved.update(revision=2)
        saved["values"].update(top_k=9, retrieval_mode="dense", rerank_enabled=False, graph_enabled=True, evidence_enabled=True)
        with patch("requests.get", side_effect=[self.response(initial), self.response(saved)]) as get, \
                patch("requests.put", return_value=self.response(saved)) as put:
            app.button(key="open_settings").click().run()
            self.assertFalse(app.exception)
            self.assertEqual(app.radio(key="settings_nav").options, ["RAG 设置", "画像设置", "对话与记忆", "技能与命令", "模型与服务"])
            put.assert_not_called()
            app.selectbox(key="settings_mode").select("dense")
            app.number_input(key="settings_top_k").set_value(9)
            app.toggle(key="settings_rerank").set_value(False)
            app.selectbox(key="settings_graph").select("开启")
            app.selectbox(key="settings_evidence").select("开启")
            next(button for button in app.button if button.label == "保存 RAG 设置").click().run()
            self.assertFalse(app.exception)
            self.assertEqual(put.call_args.kwargs["json"], {"values": saved["values"], "expected_revision": 1})
            self.assertIn("证据检查开启", "\n".join(item.value for item in app.markdown))
            self.assertTrue(any("设置已保存" in message.value for message in app.success))
            app.button(key="close_settings").click().run()
            self.assertFalse(app.session_state["settings_open"])
            # A fresh browser session must load the persisted values as well.
            app = self.app().run()
            app.button(key="open_settings").click().run()
            self.assertEqual(app.number_input(key="settings_top_k").value, 9)
            self.assertEqual(get.call_count, 2)

    def test_reset_rag_keeps_memory_setting(self):
        app = self.app().run()
        initial = self.settings_response(top_k=12, memory_enabled=False, graph_enabled=True)
        saved = self.settings_response(memory_enabled=False)
        saved["revision"] = 2
        with patch("requests.get", return_value=self.response(initial)), \
                patch("requests.put", return_value=self.response(saved)) as put:
            app.button(key="open_settings").click().run()
            next(button for button in app.button if button.label == "恢复 RAG 默认值").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(put.call_args.kwargs["json"]["values"], saved["values"])
        self.assertEqual(app.number_input(key="settings_top_k").value, 5)

    def test_settings_save_failure_preserves_draft_and_saved_revision(self):
        for status in (503, 409):
            with self.subTest(status=status):
                app = self.app().run()
                failure = MagicMock(status_code=status)
                failure.raise_for_status.side_effect = requests.HTTPError(response=failure)
                with patch("requests.get", return_value=self.response(self.settings_response())) as get, \
                        patch("requests.put", return_value=failure):
                    app.button(key="open_settings").click().run()
                    app.number_input(key="settings_top_k").set_value(8)
                    next(button for button in app.button if button.label == "保存 RAG 设置").click().run()
                    self.assertFalse(app.exception)
                    self.assertEqual(app.number_input(key="settings_top_k").value, 8)
                    self.assertEqual(app.session_state["settings_data"]["revision"], 1)
                    self.assertEqual(get.call_count, 1)
                    self.assertTrue(app.error if status == 503 else app.warning)

    def test_settings_navigation_profile_memory_and_models(self):
        app = self.app().run()
        initial = self.settings_response(top_k=7)
        profile = deepcopy(app.session_state["profile_data"])
        saved = self.settings_response(top_k=7, memory_enabled=False)
        saved["revision"] = 2
        with patch("requests.get", side_effect=[self.response(initial), self.response(profile)]), \
                patch("requests.put", return_value=self.response(saved)) as put:
            app.button(key="open_settings").click().run()
            app.radio(key="settings_nav").set_value("画像设置").run()
            self.assertFalse(app.exception)
            self.assertTrue(app.text_area(key="profile_field_preferences"))
            self.assertTrue(app.toggle(key="settings_profile_use").value)
            app.radio(key="settings_nav").set_value("对话与记忆").run()
            app.toggle(key="settings_memory").set_value(False)
            next(button for button in app.button if button.label == "保存记忆设置").click().run()
            self.assertFalse(app.exception)
            self.assertEqual(put.call_args.kwargs["json"]["values"], saved["values"])
            app.radio(key="settings_nav").set_value("模型与服务").run()
            self.assertFalse(app.exception)
            self.assertTrue(any("test-chat" in item.value for item in app.text))
            put.assert_called_once()

    def test_settings_load_failure_can_retry(self):
        app = self.app().run()
        with patch("requests.get", side_effect=[requests.ConnectionError(), self.response(self.settings_response())]):
            app.button(key="open_settings").click().run()
            self.assertTrue(app.error)
            app.button(key="retry_settings").click().run()
            self.assertFalse(app.exception)
            self.assertEqual(app.number_input(key="settings_top_k").value, 5)

    def test_sidebar_summarizes_saved_settings_and_moves_profile_controls_to_settings(self):
        app = self.app({"默认知识库": [], "研究资料": ["notes.pdf"]})
        app.session_state["settings_data"] = self.settings_response(
            top_k=8, retrieval_mode="dense", rerank_enabled=False, graph_enabled=True, memory_enabled=False)
        app.run()
        self.assertFalse(app.exception)
        self.assertEqual(app.button(key="open_rag").label, "语义检索 · Top 8")
        self.assertEqual(app.button(key="open_profile").label, "画像 · 随记忆停用")
        self.assertEqual([item.key for item in app.toggle], ['conversation_archived_filter'])
        markup = "\n".join(item.value for item in app.markdown)
        self.assertIn("重排关闭", markup)
        self.assertIn("图谱开启", markup)
        self.assertFalse(any("profile_card" == item.key for item in app.get("container")))
        self.assertTrue(any("搜索全部知识库" in item.value for item in app.caption))
        app.selectbox(key="kb_selector").select("研究资料").run()
        self.assertTrue(any("仅搜索当前知识库" in item.value for item in app.caption))


if __name__ == "__main__":
    unittest.main()

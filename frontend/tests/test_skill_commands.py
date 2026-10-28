"""Real workspace callbacks: local commands never issue a chat/model request."""
import json
import unittest
from unittest.mock import MagicMock,patch
import test_workspace_ui as fixtures


class SkillCommandTests(unittest.TestCase):
    setUp=fixtures.WorkspaceUITest.setUp
    app=fixtures.WorkspaceUITest.app
    settings_response=fixtures.WorkspaceUITest.settings_response

    def test_help_is_local(self):
        app=self.app({'默认知识库':[]}).run()
        with patch('requests.get') as get:
            app.chat_input[0].set_value('/help').run()
        self.assertFalse(app.exception)
        self.assertTrue(app.session_state['skills_help_open'])
        self.assertTrue(app.session_state['settings_open'])
        self.assertEqual(app.radio(key='settings_nav').value,'技能与命令')
        self.assertFalse(any(button.key=='skills_help_close' for button in app.button))
        self.assertEqual(app.session_state['chat_history'],[])
        self.assertFalse(app.session_state['is_streaming'])
        self.assertFalse(any('/chat_with_agent/' in str(call) for call in get.call_args_list))

    def test_home_keeps_management_and_preferences_collapsed(self):
        app=self.app({'默认知识库':[]}).run()
        self.assertFalse(app.exception)
        # Streamlit's test parser represents icon-bearing expanders as Status.
        sections={item.label:item for item in [*app.expander,*app.status]}
        self.assertFalse(sections['资料管理'].proto.expanded)
        self.assertFalse(sections['更多'].proto.expanded)
        self.assertFalse(any(button.key and button.key.startswith('prompt_') for button in app.button))
        app.button(key='open_skill_help').click().run()
        self.assertFalse(app.exception)
        self.assertTrue(app.session_state['settings_open'])
        self.assertEqual(app.radio(key='settings_nav').value,'技能与命令')


    def test_unknown_and_missing_argument_do_not_send(self):
        for command in ('&not_a_skill','/compare'):
            app=self.app({'默认知识库':[]}).run()
            with patch('requests.get') as get:
                app.chat_input[0].set_value(command).run()
            self.assertFalse(app.exception)
            self.assertEqual(app.session_state['chat_history'],[])
            self.assertTrue(app.warning)
            self.assertFalse(any('/chat_with_agent/' in str(call) for call in get.call_args_list))


    def test_new_command_preserves_old_session_and_does_not_send(self):
        app=self.app({'默认知识库':[]},[{'role':'user','content':'旧问题','timestamp':'09:00'}]).run()
        old=app.session_state['chat_session_id']
        with patch('requests.get') as get:
            app.chat_input[0].set_value('&新对话').run()
        self.assertFalse(app.exception)
        self.assertNotEqual(app.session_state['chat_session_id'],old)
        self.assertEqual(app.session_state['chat_history'],[])
        self.assertFalse(any('/chat_with_agent/' in str(call) for call in get.call_args_list))


    def test_settings_command_opens_existing_dialog(self):
        app=self.app({'默认知识库':[]}).run()
        response=MagicMock()
        response.json.return_value=self.settings_response()
        with patch('requests.get',return_value=response) as get:
            app.chat_input[0].set_value('/设置').run()
        self.assertFalse(app.exception)
        self.assertTrue(app.session_state['settings_open'])
        self.assertFalse(any('/chat_with_agent/' in str(call) for call in get.call_args_list))


    def test_agent_command_sends_only_raw_command_once(self):
        app=self.app({'默认知识库':[]}).run()
        response=MagicMock()
        response.__enter__.return_value=response
        response.status_code=200
        response.iter_lines.return_value=iter([json.dumps({'type':'run','run_id':'skills-test',
            'conversation_id':'00000000-0000-4000-8000-000000000001',
            'skill':{'id':'summary','version':'test-v1'}}),json.dumps({'type':'answer','content':'带依据的摘要',
            'conversation_id':'00000000-0000-4000-8000-000000000001'})])
        with patch('requests.post',return_value=response) as get:
            app.chat_input[0].set_value('&摘要 检索质量').run()
        self.assertFalse(app.exception)
        sent=[call for call in get.call_args_list if '/turns/stream' in call.args[0]]
        self.assertEqual(len(sent),1)
        self.assertEqual(sent[0].kwargs['json']['query'],'&摘要 检索质量')
        self.assertNotIn('instructions',sent[0].kwargs['json'])
        self.assertEqual(app.session_state['chat_history'][-1]['skill']['id'],'summary')



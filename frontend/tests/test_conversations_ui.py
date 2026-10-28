"""Navigation/stream failures on the real Streamlit page, using explicit API fixtures."""
import json
import unittest
from unittest.mock import MagicMock,patch
import requests
import test_workspace_ui as fixtures


CID = '00000000-0000-4000-8000-000000000002'


def conversation(scope='甲', cid=CID):
    return {'conversation_id':cid,'session_id':'c_'+cid.replace('-',''),'user_id':1,
            'scope_id':scope,'title':'第二条会话','revision':3,'archived':False,'read_only':False}


def messages(scope='甲', text='只属于第二条', cid=CID):
    return {'conversation':conversation(scope,cid),'items':[{'turn_id':'turn2','sequence':1,
        'query':text,'answer':'第二条回答','status':'complete','run_id':'run-second',
        'started_at':'2026-10-26T08:00:00+00:00'}],'next_cursor':None}


def response(data=None,status=200,events=None):
    result=MagicMock(status_code=status)
    result.ok=status<400
    result.__enter__.return_value=result
    result.json.return_value=data
    if status>=400:
        result.raise_for_status.side_effect=requests.HTTPError(response=result)
    if events is not None:
        result.iter_lines.return_value=[json.dumps(item) for item in events]
    return result


class ConversationUITests(unittest.TestCase):
    setUp=fixtures.WorkspaceUITest.setUp
    app=fixtures.WorkspaceUITest.app
    settings_response=fixtures.WorkspaceUITest.settings_response

    def test_sidebar_switches_same_kb_conversations_and_deep_link(self):
        first=self.app({'甲':[]},[{'role':'user','content':'只属于第一条','timestamp':'09:00'}])
        with patch('conversations_ui._directory',return_value={'items':[conversation()],'next_cursor':None}),\
             patch('requests.get',return_value=response(messages())):
            first.run()
            first.button(key='conversation_'+CID).click().run()
        self.assertFalse(first.exception)
        self.assertEqual(first.query_params['conversation'],[CID])
        self.assertEqual(first.session_state['chat_history'][0]['content'],'只属于第二条')
        self.assertNotIn('只属于第一条',str(first.session_state['chat_history']))

    def test_switching_kb_changes_session_and_remembers_previous_conversation(self):
        app=self.app({'甲':[],'乙':[]}).run()
        old=app.session_state['active_conversation']['conversation_id']
        app.session_state['conversation_recent']={'甲':old}
        app.selectbox(key='kb_selector').select('乙').run()
        self.assertIsNone(app.session_state['active_conversation'])
        self.assertEqual(app.session_state['chat_history'],[])
        with patch('requests.get',return_value=response(messages('甲',cid=old))):
            app.selectbox(key='kb_selector').select('甲').run()
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state['active_conversation']['conversation_id'],old)

    def test_deep_link_library_missing_from_cached_directory_keeps_bound_scope(self):
        app=self.app({'默认知识库':[]})
        app.session_state['conversation_ready']=False
        app.query_params['conversation']=CID
        with patch('requests.get',return_value=response(messages('新建资料库'))):
            app.run()
        self.assertFalse(app.exception)
        self.assertEqual(app.selectbox(key='kb_selector').value,'新建资料库')
        self.assertEqual(app.session_state['selected_kb'],'新建资料库')
        self.assertEqual(app.session_state['conversation_draft_scope'],'新建资料库')
        self.assertFalse(app.chat_input[0].disabled)

    def test_first_send_creates_once_then_uses_post_bound_conversation(self):
        app=self.app({'甲':[]}).run()
        app.button(key='new_chat').click().run()
        stream=response(events=[{'type':'run','conversation_id':CID,'run_id':'offline'},
            {'type':'answer','conversation_id':CID,'content':'离线回答'}])
        with patch('requests.post',side_effect=[response(conversation()),stream]) as post:
            app.chat_input[0].set_value('第一条问题').run()
        self.assertFalse(app.exception)
        self.assertEqual(post.call_count,2)
        self.assertEqual(post.call_args_list[0].kwargs['json']['scope_id'],'甲')
        self.assertIn(CID,post.call_args_list[1].args[0])
        self.assertEqual(post.call_args_list[1].kwargs['json']['query'],'第一条问题')
        self.assertEqual(app.session_state['chat_history'][-1]['content'],'离线回答')

    def test_failed_open_clears_old_context_and_blocks_input(self):
        app=self.app({'甲':[]},[{'role':'user','content':'旧内容','timestamp':'09:00'}])
        with patch('conversations_ui._directory',return_value={'items':[conversation()],'next_cursor':None}),\
             patch('requests.get',return_value=response(status=404)):
            app.run()
            app.button(key='conversation_'+CID).click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state['chat_history'],[])
        self.assertTrue(app.chat_input[0].disabled)

    def test_uncertain_stream_retry_reuses_original_id_and_ignores_foreign_events(self):
        app=self.app({'甲':[]}).run()
        foreign=response(events=[{'type':'answer','conversation_id':CID,'content':'错误窗口的片段'}])
        with patch('requests.post',return_value=foreign) as post:
            app.chat_input[0].set_value('重试问题').run()
        first=post.call_args.kwargs['json']
        self.assertNotIn('错误窗口的片段',str(app.session_state['chat_history']))
        stream=response(events=[{'type':'answer','conversation_id':app.session_state['active_conversation']['conversation_id'],'content':'重试结果'}])
        with patch('requests.post',return_value=stream) as post:
            app.button(key='conversation_retry_send').click().run()
        self.assertFalse(app.exception)
        self.assertEqual(first,post.call_args.kwargs['json'])

    def test_archived_and_legacy_views_are_read_only(self):
        app=self.app({'甲':[]})
        current=conversation()
        current['archived']=True
        app.session_state['active_conversation']=current
        app.run()
        self.assertTrue(app.chat_input[0].disabled)
        self.assertTrue(any(button.label=='恢复会话' for button in app.button))
        app.session_state['legacy_read_only']=True
        app.run()
        self.assertTrue(app.chat_input[0].disabled)
        self.assertTrue(any('旧版历史' in item.value for item in app.info))

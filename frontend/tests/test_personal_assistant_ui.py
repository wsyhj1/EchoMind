"""Workbench interactions, network uncertainty, project chat and review; no providers."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4

import requests

sys.path.insert(0,str(Path(__file__).resolve().parent))
import test_workspace_ui as fixtures

PID = '10000000-0000-4000-8000-000000000001'
NOW = '2026-10-27T15:00:00+00:00'


class AssistantUITests(unittest.TestCase):
    setUp = fixtures.WorkspaceUITest.setUp
    app = fixtures.WorkspaceUITest.app
    settings_response = fixtures.WorkspaceUITest.settings_response

    def workspace(self,title='EchoMind 演示项目'):
        return {'project':{'project_id':PID,'user_id':1,'title':title,'goal':'隔天接上进度','scope_id':'默认知识库',
            'revision':1,'archived':False,'created_at':NOW,'updated_at':NOW},'tasks':[],'notes':[],
            'resources':[],'events':[],'events_has_more':False,'conversations':[],'proposals':[]}

    def run_app(self,workspace=None,empty=False):
        self.data = workspace or self.workspace()
        self.empty = empty
        app = self.app({'默认知识库':[]})
        app.session_state['workspace_view'] = '工作台'
        getter = patch('assistant_ui._get',side_effect=self.get)
        getter.start()
        self.addCleanup(getter.stop)
        return app

    def get(self,api,user_id,path,**params):
        if path=='/assistant/reminders/status':
            return {'healthy':True,'last_tick_at':NOW}
        if path=='/assistant/notifications':
            return {'items':[],'has_more':False,'unread_count':0}
        if path.endswith('/reminders'):
            return {'items':[],'has_more':False}
        if path=='/assistant/projects':
            return {'items':[] if self.empty else [{**self.data['project'],'open_tasks':len(self.data['tasks']),'done_tasks':0}]}
        if path.endswith('/resume'):
            return {'next_steps':[],'blocked_tasks':[],'unallocated_minutes':params['minutes']}
        if path.endswith('/review'):
            return {'project_id':PID,'project_revision':self.data['project']['revision'],'markdown':'# 复盘\n\n依据：用户保存的变更。'}
        return deepcopy(self.data)

    def post(self,api,user_id,path,body):
        if path=='/assistant/projects':
            self.empty = False
            self.data['project'].update({key:body[key] for key in ('title','goal','scope_id')})
            return deepcopy(self.data['project'])
        action = body.get('action',{})
        if action.get('kind')=='add_task':
            self.data['tasks'].append({**action,'task_id':str(uuid4()),'project_id':PID,'status':'todo',
                'completion_note':'','blocker':'','created_at':NOW,'updated_at':NOW})
            self.data['project']['revision'] += 1
        return {'project':deepcopy(self.data['project'])}

    def test_empty_project_setup_does_not_require_document_upload(self):
        app = self.run_app(empty=True).run()
        self.assertFalse(app.exception)
        self.assertTrue(any('任务、笔记与工作恢复无需上传文档' in item.value for item in app.info))
        self.assertFalse(app.chat_input)

    def test_create_project_persists_and_clears_only_successful_form(self):
        app = self.run_app(empty=True).run()
        app.text_input(key='assistant_create_project_0_title').set_value('我的项目')
        app.text_area(key='assistant_create_project_0_goal').set_value('完成第一版')
        with patch('assistant_ui._post',side_effect=self.post) as post:
            next(b for b in app.button if b.label=='创建项目').click().run()
        self.assertFalse(app.exception)
        self.assertEqual(post.call_args.args[3]['title'],'我的项目')
        self.assertIn('client_create_id',post.call_args.args[3])
        self.assertEqual(app.query_params['project'],[PID])
        self.assertEqual(app.text_input(key='assistant_create_project_1_title').value,'')

    def test_reminder_form_sends_aware_time_and_retries_original_operation(self):
        from datetime import date, time
        app = self.run_app().run()
        form = f'assistant_{PID}_reminder_0'
        app.text_input(key=form+'_title').set_value('明天复盘')
        app.date_input(key=form+'_date').set_value(date(2027,1,2))
        app.time_input(key=form+'_time').set_value(time(9,30))
        with patch('assistant_ui._post',side_effect=requests.ConnectionError('offline')) as post:
            next(b for b in app.button if b.label=='设置提醒').click().run()
        self.assertFalse(app.exception)
        original = deepcopy(post.call_args.args[3])
        self.assertEqual(original['due_at'],'2027-01-02T09:30:00+08:00')
        self.assertEqual(original['timezone'],'Asia/Shanghai')
        self.assertNotIn('expected_revision',original)
        with patch('assistant_ui._post',return_value={'reminder_id':str(uuid4())}) as retry:
            app.button(key='assistant_retry_pending').click().run()
        self.assertFalse(app.exception)
        self.assertEqual(retry.call_args.args[3],original)

    def test_notification_read_and_snooze_use_reminder_revision(self):
        from datetime import datetime
        rid,nid = str(uuid4()),str(uuid4())
        item = {'notification_id':nid,'reminder_id':rid,'project_id':PID,'title':'检查实验结果',
            'project_title':'演示项目','task_title':None,'due_at':NOW,'delivered_at':NOW,'read_at':None,
            'reminder_status':'delivered','reminder_revision':2,'generation':1,'current_generation':1}
        app = self.run_app()
        getter = self.get
        def notifications(api,user,path,**params):
            if path=='/assistant/notifications': return {'items':[item],'has_more':False,'unread_count':1}
            return getter(api,user,path,**params)
        with patch('assistant_ui._get',side_effect=notifications):
            app.run()
            with patch('assistant_ui._post',return_value={'read_at':NOW}) as post:
                app.button(key=nid+'_read').click().run()
            self.assertFalse(app.exception)
            self.assertEqual(post.call_args.args[2],f'/assistant/notifications/{nid}/read')
            with patch('assistant_ui._post',return_value={'status':'scheduled'}) as post:
                app.button(key=nid+'_snooze').click().run()
            self.assertFalse(app.exception)
            self.assertEqual(post.call_args.args[3]['expected_revision'],2)
            self.assertEqual(datetime.fromisoformat(post.call_args.args[3]['due_at']).utcoffset().total_seconds(),28800)
            item['current_generation'] = 2
            app.run()
            self.assertTrue(app.button(key=nid+'_snooze').disabled)

    def test_reminders_allow_cancellation_in_archived_project(self):
        data = self.workspace()
        data['project']['archived'] = True
        rid = str(uuid4())
        app = self.run_app(data)
        getter = self.get
        def reminders(api,user,path,**params):
            if path.endswith('/reminders'):
                return {'items':[{'reminder_id':rid,'title':'待取消','status':'scheduled','due_at':NOW,'reason':'','revision':1}], 'has_more':False}
            return getter(api,user,path,**params)
        with patch('assistant_ui._get',side_effect=reminders):
            app.run()
            self.assertTrue(next(b for b in app.button if b.label=='设置提醒').disabled)
            self.assertFalse(app.button(key=rid+'_cancel').disabled)
            with patch('assistant_ui._post',return_value={'status':'cancelled'}) as post:
                app.button(key=rid+'_cancel').click().run()
            self.assertFalse(app.exception)
            self.assertEqual(post.call_args.args[3]['expected_revision'],1)

    def test_scheduler_delay_is_visible_without_hiding_workspace(self):
        app = self.run_app()
        getter = self.get
        def status(api,user,path,**params):
            if path=='/assistant/reminders/status': return {'healthy':False,'last_tick_at':None}
            return getter(api,user,path,**params)
        with patch('assistant_ui._get',side_effect=status): app.run()
        self.assertFalse(app.exception)
        self.assertTrue(any('心跳延迟' in x.value for x in app.warning))
        self.assertTrue(any(b.label=='设置提醒' for b in app.button))

    def test_add_task_uses_current_revision_and_server_operation(self):
        app = self.run_app().run()
        app.text_input(key=f'assistant_{PID}_new_task_0_title').set_value('整理实验')
        app.number_input(key=f'assistant_{PID}_new_task_0_estimate').set_value(30)
        with patch('assistant_ui._post',side_effect=self.post) as post:
            next(b for b in app.button if b.label=='保存任务').click().run()
        self.assertFalse(app.exception)
        body = post.call_args.args[3]
        self.assertEqual((body['expected_revision'],body['action']['estimated_minutes']),(1,30))
        self.assertIn('operation_id',body)
        self.assertEqual(app.text_input(key=f'assistant_{PID}_new_task_1_title').value,'')
        self.assertTrue(app.dataframe)

    def test_uncertain_write_keeps_original_id_and_retry_does_not_make_new_request(self):
        app = self.run_app().run()
        app.text_input(key=f'assistant_{PID}_new_task_0_title').set_value('不能重复的任务')
        with patch('assistant_ui._post',side_effect=requests.ConnectionError('offline')) as post:
            next(b for b in app.button if b.label=='保存任务').click().run()
        self.assertFalse(app.exception)
        original = deepcopy(post.call_args.args[3])
        self.assertTrue(next(b for b in app.button if b.label=='保存任务').disabled)
        with patch('assistant_ui._post',side_effect=self.post) as retry:
            app.button(key='assistant_retry_pending').click().run()
        self.assertFalse(app.exception)
        self.assertEqual(original,retry.call_args.args[3])
        self.assertEqual(len(self.data['tasks']),1)

    def test_project_markup_escapes_user_input_and_review_exports(self):
        app = self.run_app(self.workspace('<img src=x onerror=alert(1)>')).run()
        self.assertFalse(app.exception)
        markup = '\n'.join(item.value for item in app.markdown)
        self.assertNotIn('<img src=x',markup)
        self.assertIn('&lt;img',markup)
        app.button(key='assistant_generate_review').click().run()
        self.assertFalse(app.exception)
        self.assertTrue(any('用户保存的变更' in item.value for item in app.markdown))
        self.assertTrue(any(b.label=='下载复盘 Markdown' for b in app.get('download_button')))

    def test_stale_proposal_is_visible_but_cannot_apply(self):
        workspace = self.workspace()
        proposal_id = str(uuid4())
        workspace['proposals'] = [{'proposal_id':proposal_id,'base_revision':0,'created_at':NOW,
            'source_turn_id':str(uuid4()),'action':{'kind':'add_task','title':'旧计划'}}]
        app = self.run_app(workspace).run()
        self.assertFalse(app.exception)
        self.assertTrue(app.button(key=proposal_id+'_apply').disabled)
        self.assertFalse(app.button(key=proposal_id+'_reject').disabled)

    def test_project_chat_sends_binding_and_routes_without_mutating_instantiated_widget(self):
        app = self.run_app().run()
        with patch('assistant_ui._post',return_value={'conversation_id':str(uuid4())}) as post,\
             patch('conversations_ui.load_conversation',return_value=True):
            app.button(key='assistant_start_chat').click().run()
        self.assertFalse(app.exception)
        self.assertEqual(post.call_args.args[3]['project_id'],PID)
        self.assertEqual(app.session_state['workspace_view'],'知识对话')

    def test_switching_back_to_workbench_replaces_chat_deep_link(self):
        app = self.run_app().run()
        app.session_state['active_conversation']['project_id'] = PID
        app.radio(key='workspace_view').set_value('知识对话').run()
        self.assertIn('conversation',app.query_params)
        app.radio(key='workspace_view').set_value('工作台').run()
        self.assertFalse(app.exception)
        self.assertNotIn('conversation',app.query_params)
        self.assertEqual(app.query_params['project'],[PID])

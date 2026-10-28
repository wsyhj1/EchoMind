"""Real isolated PostgreSQL project lifecycle and deterministic recovery; no provider access."""
import asyncio
from datetime import date, datetime, timedelta, timezone
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from assistant.models import ACTION, CreateProject
from assistant.service import build_review, parse_command, project_snapshot, resume_workspace
from assistant.store import AssistantConflict, AssistantStore
from conversation_store import ConversationStore
from evaluation_store import EvaluationStore
from observability import RunTrace


class AssistantRulesTests(unittest.TestCase):
    def task(self,**fields):
        return {'task_id':str(uuid4()),'title':'任务','description':'','category':'work','priority':'normal',
            'status':'todo','estimated_minutes':30,'due_date':None,'created_at':'2026-10-27T00:00:00+00:00',
            'completion_note':'','blocker':'',**fields}

    def workspace(self,tasks):
        return {'project':{'project_id':str(uuid4()),'title':'项目','revision':1},'tasks':tasks,'notes':[], 'resources':[]}

    def test_explicit_commands_keep_all_constraints_and_reject_unparsed_parts(self):
        action = parse_command('新增学习任务：学习SSE；预计30分钟；截止2026-11-01；优先级高')
        self.assertEqual((action.category,action.estimated_minutes,action.due_date,action.priority),('study',30,date(2026,11,1),'high'))
        self.assertEqual(parse_command('记录决定：先做工作台；暂缓外部连接').content,'先做工作台；暂缓外部连接')
        for text in ('帮我想个计划','新增任务：任务；预计半小时','新增任务：任务；预计30分钟；预计40分钟','新增任务：任务；截止2026-02-30'):
            with self.assertRaises(ValueError): parse_command(text)

    def test_completion_and_blocking_require_user_report_and_no_unknown_fields(self):
        for data in ({'kind':'update_task','task_id':str(uuid4()),'status':'done'},
                     {'kind':'update_task','task_id':str(uuid4()),'status':'blocked'},
                     {'kind':'add_task','title':'任务','user_id':2},
                     {'kind':'add_task','title':'  '},
                     {'kind':'add_task','title':'任务','estimated_minutes':True},
                     {'kind':'add_resource','resource_type':'link','label':'网址','reference':'javascript:alert(1)'}):
            with self.assertRaises(ValueError): ACTION.validate_python(data)

    def test_recovery_obeys_budget_and_does_not_turn_a_partial_step_into_completion(self):
        small = self.task(title='小任务',estimated_minutes=20)
        big = self.task(title='大任务',priority='high',estimated_minutes=120)
        blocked = self.task(status='blocked',blocker='缺资料')
        result = resume_workspace(self.workspace([big,small,blocked]),30,today=date(2026,10,27))
        self.assertEqual([step['task']['title'] for step in result['next_steps']],['小任务'])
        self.assertLessEqual(result['planned_minutes'],30)
        partial = resume_workspace(self.workspace([big]),15,today=date(2026,10,27))
        self.assertEqual(partial['next_steps'][0]['mode'],'partial_step')
        self.assertEqual(big['status'],'todo')
        unknown = resume_workspace(self.workspace([self.task(estimated_minutes=None)]),10,today=date(2026,10,27))
        self.assertEqual(unknown['next_steps'][0]['mode'],'scope_then_start')

    def test_snapshot_marks_omitted_state_and_caps_long_notes(self):
        workspace = self.workspace([self.task() for _ in range(35)])
        workspace['notes'] = [{'note_id':str(uuid4()),'content':'甲'*4000,'note_kind':'progress'}]*12
        snapshot = project_snapshot(workspace)
        self.assertEqual(snapshot['omitted']['active_tasks'],5)
        self.assertEqual(snapshot['omitted']['notes'],2)
        self.assertTrue(snapshot['recent_notes'][0]['content_truncated'])


@unittest.skipUnless(os.getenv('ECHOMIND_TEST_DATABASE_URL'),'Isolated PostgreSQL required')
class AssistantStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import asyncpg
        self.schema = 'assistant_test_'+uuid4().hex
        self.admin = await asyncpg.connect(os.environ['ECHOMIND_TEST_DATABASE_URL'])
        await self.admin.execute(f'CREATE SCHEMA "{self.schema}"')
        self.pool = await asyncpg.create_pool(os.environ['ECHOMIND_TEST_DATABASE_URL'],min_size=1,max_size=6,
            server_settings={'search_path':self.schema})
        await self.pool.execute('''CREATE TABLE users(user_id INT PRIMARY KEY); INSERT INTO users VALUES(1),(2);
            CREATE TABLE knowledge_bases(user_id INT,knowledge_base_id TEXT,PRIMARY KEY(user_id,knowledge_base_id));
            INSERT INTO knowledge_bases VALUES(1,'甲'),(1,'乙'),(2,'甲');
            CREATE TABLE file_metadata(user_id INT,knowledge_base_id TEXT,file_hash TEXT,file_name TEXT);
            INSERT INTO file_metadata VALUES(1,'甲',repeat('a',64),'甲.pdf'),(1,'乙',repeat('b',64),'乙.pdf'),(2,'甲',repeat('c',64),'私有.pdf');
            CREATE TABLE raw_conversations(id VARCHAR(50) PRIMARY KEY,user_id INT,thread_id VARCHAR(50),role TEXT,content TEXT,created_at TIMESTAMP);''')
        self.audit = EvaluationStore(self.pool)
        await self.audit.initialize()
        self.conversations = ConversationStore(self.pool)
        await self.conversations.initialize()
        self.store = AssistantStore(self.pool)
        await self.store.initialize()

    async def asyncTearDown(self):
        await self.pool.close()
        assert self.schema.startswith('assistant_test_') and self.schema.removeprefix('assistant_test_').isalnum()
        await self.admin.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        await self.admin.close()

    async def project(self,title='工作项目',scope='甲',user=1):
        return await self.store.create(user,CreateProject(client_create_id=uuid4(),title=title,goal='可演示',scope_id=scope))

    async def apply(self,project,action,key=None,revision=None):
        return await self.store.apply(project['user_id'],project['project_id'],key or uuid4(),revision or project['revision'],ACTION.validate_python(action))

    async def source(self,project):
        conv = await self.store.create_conversation(project['user_id'],project['project_id'],project['scope_id'],uuid4())
        trace = RunTrace(project['user_id'],conv['session_id'],'请制定计划',project['scope_id'])
        accepted = await self.conversations.accept_turn(project['user_id'],UUID(conv['conversation_id']),uuid4(),1,
            '请制定计划','请制定计划',project['scope_id'],{'values':{'memory_enabled':False}},trace,120)
        return conv,accepted['turn'],trace

    async def test_project_create_restart_idempotency_and_scope_isolation(self):
        payload = CreateProject(client_create_id=uuid4(),title='项目',scope_id='甲')
        first,again = await asyncio.gather(self.store.create(1,payload),self.store.create(1,payload))
        self.assertEqual(first['project_id'],again['project_id'])
        await self.store.initialize()
        restarted = AssistantStore(self.pool)
        self.assertEqual((await restarted.workspace(1,first['project_id']))['project']['title'],'项目')
        with self.assertRaises(LookupError): await restarted.workspace(2,first['project_id'])
        with self.assertRaises(LookupError): await self.project(scope='乙',user=2)
        with self.assertRaises(AssistantConflict): await self.store.create(1,payload.model_copy(update={'title':'不同内容'}))
        await self.pool.execute("DELETE FROM knowledge_bases WHERE user_id=1 AND knowledge_base_id='甲'")
        self.assertEqual(await self.store.list(1),[])
        with self.assertRaises(LookupError): await restarted.workspace(1,first['project_id'])

    async def test_atomic_action_replay_conflicting_keys_and_rollback(self):
        p = await self.project()
        key = uuid4()
        action = {'kind':'add_task','title':'整理实验','estimated_minutes':30}
        a,b = await asyncio.gather(self.apply(p,action,key),self.apply(p,action,key))
        self.assertEqual(a['entity']['task_id'],b['entity']['task_id'])
        self.assertEqual(sum(r['replayed'] for r in (a,b)),1)
        self.assertEqual(await self.pool.fetchval('SELECT count(*) FROM assistant_tasks'),1)
        with self.assertRaises(AssistantConflict): await self.apply(p,{'kind':'add_task','title':'不同任务'},key)
        p = a['project']
        with self.assertRaises(LookupError): await self.apply(p,{'kind':'update_task','task_id':str(uuid4()),'status':'in_progress'})
        self.assertEqual((await self.store.workspace(1,p['project_id']))['project']['revision'],2)
        self.assertEqual(await self.pool.fetchval('SELECT count(*) FROM assistant_events'),2)

    async def test_batch_plan_is_one_revision_and_completed_evidence_cannot_be_erased(self):
        p = await self.project()
        batch = await self.apply(p,{'kind':'add_tasks','tasks':[{'title':'学习事务','category':'study'}, {'title':'实现事务'}]})
        self.assertEqual((len(batch['entity']['tasks']),batch['project']['revision']),(2,2))
        task = batch['entity']['tasks'][0]
        done = await self.apply(batch['project'],{'kind':'update_task','task_id':task['task_id'],'status':'done','completion_note':'用户报告：练习完成'})
        with self.assertRaises(AssistantConflict):
            await self.apply(done['project'],{'kind':'update_task','task_id':task['task_id'],'completion_note':''})
        current = await self.store.workspace(1,p['project_id'])
        self.assertEqual(current['project']['revision'],3)
        self.assertTrue(next(item for item in current['tasks'] if item['task_id']==task['task_id'])['completion_note'])

    async def test_concurrent_different_actions_have_one_winner_without_lost_updates(self):
        p = await self.project()
        results = await asyncio.gather(self.apply(p,{'kind':'add_task','title':'甲'}),self.apply(p,{'kind':'add_task','title':'乙'}),return_exceptions=True)
        self.assertEqual(sum(isinstance(r,AssistantConflict) for r in results),1)
        workspace = await self.store.workspace(1,p['project_id'])
        self.assertEqual((len(workspace['tasks']),workspace['project']['revision']),(1,2))

    async def test_cross_project_tasks_and_documents_cannot_be_attached(self):
        a,b = await self.project(),await self.project('其他项目')
        task = await self.apply(a,{'kind':'add_task','title':'私有任务'})
        with self.assertRaises(LookupError): await self.apply(b,{'kind':'update_task','task_id':task['entity']['task_id'],'status':'in_progress'})
        for hash_value in ('b'*64,'c'*64):
            with self.assertRaises(LookupError): await self.apply(b,{'kind':'add_resource','resource_type':'document','label':'不应关联','reference':hash_value})
        result = await self.apply(b,{'kind':'add_resource','resource_type':'document','label':'资料','reference':'a'*64})
        self.assertEqual(result['entity']['knowledge_base_id'],'甲')
        await self.pool.execute("DELETE FROM file_metadata WHERE user_id=1 AND file_hash=repeat('a',64)")
        self.assertFalse((await self.store.workspace(1,b['project_id']))['resources'][0]['available'])

    async def test_note_correction_preserves_history_and_refuses_foreign_reference(self):
        a,b = await self.project(),await self.project('另一个项目')
        old = await self.apply(a,{'kind':'add_note','note_kind':'decision','content':'先做语音'})
        with self.assertRaises(AssistantConflict): await self.apply(b,{'kind':'add_note','note_kind':'decision','content':'越权更正','supersedes_note_id':old['entity']['note_id']})
        new = await self.apply(old['project'],{'kind':'add_note','note_kind':'decision','content':'改为先做工作台','supersedes_note_id':old['entity']['note_id']})
        workspace = await self.store.workspace(1,a['project_id'])
        self.assertEqual([n['content'] for n in workspace['notes']],['改为先做工作台'])
        self.assertEqual(await self.pool.fetchval('SELECT count(*) FROM assistant_notes'),2)
        self.assertEqual(str(await self.pool.fetchval('SELECT superseded_by FROM assistant_notes WHERE note_id=$1',UUID(old['entity']['note_id']))),new['entity']['note_id'])

    async def test_proposal_is_not_fact_and_apply_is_atomic_and_idempotent(self):
        p = await self.project()
        conv,turn,trace = await self.source(p)
        key = uuid4()
        proposal = await self.store.propose(1,p['project_id'],key,1,ACTION.validate_python({'kind':'add_task','title':'候选任务'}),turn['turn_id'])
        same = await self.store.propose(1,p['project_id'],key,1,ACTION.validate_python({'kind':'add_task','title':'候选任务'}),turn['turn_id'])
        self.assertEqual(proposal['proposal_id'],same['proposal_id'])
        self.assertEqual((await self.store.workspace(1,p['project_id']))['tasks'],[])
        trace.finish('已保存候选')
        await self.conversations.save_run(trace.snapshot())
        operation_id = uuid4()
        result = await self.store.decide(1,p['project_id'],proposal['proposal_id'],'apply',operation_id,1)
        replay = await self.store.decide(1,p['project_id'],proposal['proposal_id'],'apply',operation_id,1)
        self.assertEqual(result['entity']['task_id'],replay['entity']['task_id'])
        self.assertTrue(replay['replayed'])
        event = (await self.store.events(1,p['project_id']))['items'][0]
        self.assertEqual((event['source'],event['source_turn_id']),('assistant_proposal',str(turn['turn_id'])))

    async def test_stale_proposal_rejection_and_expired_source_do_not_write_facts(self):
        p = await self.project()
        _,turn,_ = await self.source(p)
        action = ACTION.validate_python({'kind':'add_note','content':'推断的进展'})
        proposal = await self.store.propose(1,p['project_id'],uuid4(),1,action,turn['turn_id'])
        change = await self.apply(p,{'kind':'add_task','title':'用户的新决定'})
        with self.assertRaises(AssistantConflict): await self.store.decide(1,p['project_id'],proposal['proposal_id'],'apply',uuid4(),2)
        rejected = await self.store.decide(1,p['project_id'],proposal['proposal_id'],'reject',uuid4(),2)
        self.assertEqual(rejected['status'],'rejected')
        await self.pool.execute("UPDATE chat_turns SET lease_until=clock_timestamp()-interval '1 second'")
        with self.assertRaises(AssistantConflict): await self.store.propose(1,p['project_id'],uuid4(),2,action,turn['turn_id'])
        self.assertEqual((await self.store.workspace(1,p['project_id']))['notes'],[])

    async def test_separate_project_conversations_restore_current_same_project_state(self):
        p = await self.project()
        conv,_,_ = await self.source(p)
        change = await self.apply(p,{'kind':'add_note','note_kind':'progress','content':'工作台已经可用'})
        second = await self.store.create_conversation(1,p['project_id'],'甲',uuid4())
        self.assertNotEqual(conv['conversation_id'],second['conversation_id'])
        for cid in (conv['conversation_id'],second['conversation_id']):
            snapshot = await self.store.for_conversation(1,cid)
            self.assertEqual(snapshot['notes'][0]['content'],'工作台已经可用')
        self.assertIsNone(await self.store.for_conversation(2,conv['conversation_id']))
        with self.assertRaises(AssistantConflict): await self.store.create_conversation(1,p['project_id'],'乙',uuid4())

    async def test_complete_reopen_archive_and_review_use_actual_events(self):
        p = await self.project()
        task = await self.apply(p,{'kind':'add_task','title':'实验'})
        done = await self.apply(task['project'],{'kind':'update_task','task_id':task['entity']['task_id'],'status':'done','completion_note':'我已运行测试'})
        self.assertIsNotNone(done['entity']['completed_at'])
        reopened = await self.apply(done['project'],{'kind':'update_task','task_id':task['entity']['task_id'],'status':'todo'})
        self.assertIsNone(reopened['entity']['completed_at'])
        self.assertEqual(reopened['entity']['completion_note'],'')
        workspace = await self.store.workspace(1,p['project_id'])
        since = datetime.now(timezone.utc)-timedelta(days=7)
        review = build_review(workspace,await self.store.events(1,p['project_id'],since=since),since)
        self.assertIn('当前已改为待开始',review['markdown'])
        self.assertEqual(review['completion_event_count'],1)
        self.assertIn('用户报告',review['markdown'])
        archived = await self.apply(reopened['project'],{'kind':'edit_project','archived':True})
        with self.assertRaises(AssistantConflict): await self.apply(archived['project'],{'kind':'add_task','title':'不应写入'})
        restored = await self.apply(archived['project'],{'kind':'edit_project','archived':False})
        self.assertFalse(restored['project']['archived'])

    async def test_api_identity_validation_commands_review_and_http_conflicts(self):
        from fastapi import FastAPI
        from httpx import ASGITransport,AsyncClient
        from auth import IdentityMiddleware
        from assistant_api import router
        app = FastAPI()
        app.state.assistant_store = self.store
        app.include_router(router)
        app.add_middleware(IdentityMiddleware)
        with patch.dict(os.environ,{'ECHOMIND_AUTH_MODE':'local','ECHOMIND_LOCAL_USER_ID':'1'}):
            async with AsyncClient(transport=ASGITransport(app=app,client=('127.0.0.1',1234)),base_url='http://127.0.0.1') as client:
                response = await client.post('/assistant/projects',json={'client_create_id':str(uuid4()),'title':'API项目','scope_id':'甲'})
                self.assertEqual(response.status_code,201,response.text)
                pid = response.json()['project_id']
                self.assertEqual((await client.get('/assistant/projects',params={'user_id':2})).status_code,403)
                request = {'operation_id':str(uuid4()),'expected_revision':1,'text':'新增任务：整理报告；预计30分钟'}
                result = await client.post(f'/assistant/projects/{pid}/commands',json=request)
                self.assertEqual(result.status_code,200,result.text)
                self.assertTrue((await client.post(f'/assistant/projects/{pid}/commands',json=request)).json()['replayed'])
                invalid = await client.post(f'/assistant/projects/{pid}/actions',json={'operation_id':str(uuid4()),'expected_revision':2,'action':{'kind':'add_task','title':'任务','user_id':2}})
                self.assertEqual(invalid.status_code,422)
                review = await client.get(f'/assistant/projects/{pid}/review')
                self.assertEqual(review.status_code,200,review.text)
                self.assertIn('整理报告',review.json()['markdown'])
                self.assertEqual((await client.get(f'/assistant/projects/{pid}/resume',params={'minutes':0})).status_code,422)

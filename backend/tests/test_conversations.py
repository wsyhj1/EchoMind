"""Real isolated PostgreSQL + offline agents; no private documents/providers."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from conversation_store import ConversationStore, ConversationConflict
from evaluation_store import EvaluationStore
from observability import RunTrace
from user_settings import SettingsValues


@unittest.skipUnless(os.getenv('ECHOMIND_TEST_DATABASE_URL'), 'Isolated PostgreSQL required')
class ConversationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import asyncpg
        self.schema = 'conversation_test_'+uuid4().hex
        self.admin = await asyncpg.connect(os.environ['ECHOMIND_TEST_DATABASE_URL'])
        await self.admin.execute(f'CREATE SCHEMA "{self.schema}"')
        self.pool = await asyncpg.create_pool(os.environ['ECHOMIND_TEST_DATABASE_URL'], min_size=1, max_size=5,
            server_settings={'search_path': self.schema})
        await self.pool.execute('''CREATE TABLE users(user_id INT PRIMARY KEY);
            INSERT INTO users VALUES(1),(2);
            CREATE TABLE knowledge_bases(user_id INT,knowledge_base_id TEXT,PRIMARY KEY(user_id,knowledge_base_id));
            INSERT INTO knowledge_bases VALUES(1,'甲'),(1,'乙'),(2,'甲');
            CREATE TABLE raw_conversations(id VARCHAR(50) PRIMARY KEY,user_id INT,thread_id VARCHAR(50),role TEXT,content TEXT,created_at TIMESTAMP);''')
        self.audit = EvaluationStore(self.pool)
        await self.audit.initialize()
        self.store = ConversationStore(self.pool)
        await self.store.initialize()

    async def asyncTearDown(self):
        await self.pool.close()
        assert self.schema.startswith('conversation_test_') and self.schema.removeprefix('conversation_test_').isalnum()
        await self.admin.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        await self.admin.close()

    async def create(self, scope='甲', user=1):
        return await self.store.create(user, scope, uuid4())

    async def accept(self, conversation, query='问题', key=None, revision=None):
        trace = RunTrace(conversation['user_id'], conversation['session_id'], query, conversation['scope_id'])
        accepted = await self.store.accept_turn(conversation['user_id'], UUID(conversation['conversation_id']),
            key or uuid4(), revision or conversation['revision'], query, query, conversation['scope_id'],
            {'values': SettingsValues(memory_enabled=False).model_dump()}, trace, 150)
        return accepted, trace

    async def complete(self, conversation, query='问题', answer='回答'):
        accepted, trace = await self.accept(conversation, query)
        trace.finish(answer)
        await self.store.save_run(trace.snapshot())
        return accepted, trace

    async def test_create_idempotency_scope_binding_and_user_access(self):
        key = uuid4()
        a, b = await asyncio.gather(self.store.create(1, '甲', key), self.store.create(1, '甲', key))
        self.assertEqual(a['conversation_id'], b['conversation_id'])
        await self.store.initialize()
        with self.assertRaises(ConversationConflict):
            await self.store.create(1, '乙', key)
        with self.assertRaises(LookupError):
            await self.store.get(2, a['conversation_id'])
        with self.assertRaises(LookupError):
            await self.store.create(2, '乙', uuid4())
        with self.assertRaises(ConversationConflict):
            await self.store.accept_turn(1, a['conversation_id'], uuid4(), 1, '问题', '问题', '乙', {},
                                        RunTrace(1,a['session_id'],'问题','乙'), 150)
        self.assertLessEqual(len('2147483647:'+a['session_id']), 50)

    async def test_same_kb_multiple_conversations_history_isolation_and_refresh(self):
        a, b = await self.create(), await self.create()
        await self.complete(a, '约束甲', '只属于甲的回答')
        await self.complete(b, '约束乙', '只属于乙的回答')
        restarted = ConversationStore(self.pool)
        history_a = await restarted.history(1, a['conversation_id'], before_sequence=2)
        history_b = await restarted.history(1, b['conversation_id'], before_sequence=2)
        self.assertEqual([r['model_query'] for r in history_a if not r.get('is_anchor')], ['约束甲'])
        self.assertEqual([r['model_query'] for r in history_b if not r.get('is_anchor')], ['约束乙'])
        messages = await restarted.messages(1, a['conversation_id'])
        self.assertEqual(messages['items'][0]['answer'], '只属于甲的回答')
        self.assertEqual((await restarted.by_session(1,a['session_id']))['conversation_id'],a['conversation_id'])
        self.assertEqual(len((await restarted.list(1,'甲'))['items']),2)

    async def test_same_conversation_concurrency_and_idempotent_replay(self):
        a = await self.create()
        key = uuid4()
        results = await asyncio.gather(self.accept(a,key=key), self.accept(a,key=key))
        self.assertEqual(sum(r[0]['replayed'] for r in results),1)
        winner = next(r for r in results if not r[0]['replayed'])
        with self.assertRaises(ConversationConflict):
            await self.accept(a,key=uuid4(),revision=2)
        with self.assertRaises(ConversationConflict):
            await self.accept(a,query='不同内容',key=key)
        with self.assertRaises(ConversationConflict):
            await self.store.edit(1,a['conversation_id'],2,archived=True)
        winner[1].finish('已确认')
        await self.store.save_run(winner[1].snapshot())
        replay,_ = await self.accept(a,key=key)
        self.assertTrue(replay['replayed'])
        self.assertEqual(replay['turn']['answer'],'已确认')
        self.assertEqual(await self.pool.fetchval('SELECT count(*) FROM evaluation_runs'),1)

    async def test_expired_execution_fences_late_save_and_can_start_new_turn(self):
        a = await self.create()
        accepted, trace = await self.accept(a)
        await self.pool.execute("UPDATE chat_turns SET lease_until=now()-interval '1 second'")
        items = await self.store.messages(1,a['conversation_id'])
        self.assertEqual(items['items'][0]['status'],'failed')
        trace.finish('迟到成功')
        with self.assertRaises(ConversationConflict):
            await self.store.save_run(trace.snapshot())
        updated = await self.store.get(1,a['conversation_id'])
        await self.complete(updated, '重试的新问题')
        history = await self.store.history(1,a['conversation_id'],before_sequence=3)
        self.assertEqual(len([r for r in history if not r.get('is_anchor')]),1)

    async def test_waiting_for_row_lock_cannot_extend_an_expired_lease(self):
        a = await self.create()
        _,trace = await self.accept(a)
        trace.finish('不应提交的迟到回答')
        async with self.pool.acquire() as conn:
            transaction = conn.transaction()
            await transaction.start()
            await conn.fetchval('SELECT revision FROM chat_conversations WHERE conversation_id=$1 FOR UPDATE',UUID(a['conversation_id']))
            await conn.execute("UPDATE chat_turns SET lease_until=clock_timestamp()+interval '0.1 seconds'")
            saving = asyncio.create_task(self.store.save_run(trace.snapshot()))
            await asyncio.sleep(.25)
            await transaction.commit()
            with self.assertRaises(ConversationConflict):
                await saving
        self.assertEqual((await self.store.messages(1,a['conversation_id']))['items'][0]['status'],'failed')

    async def test_rename_archive_restore_revision_and_literal_search(self):
        a = await self.create()
        updated = await self.store.edit(1,a['conversation_id'],1,title='计划_%',archived=True)
        self.assertFalse((await self.store.list(1,'甲'))['items'])
        self.assertEqual(len((await self.store.list(1,'甲',archived=True,search='_%'))['items']),1)
        with self.assertRaises(ConversationConflict):
            await self.store.edit(1,a['conversation_id'],1,title='旧标签页')
        with self.assertRaises(ConversationConflict):
            await self.accept(updated)
        restored = await self.store.edit(1,a['conversation_id'],2,archived=False)
        await self.complete(restored)

    async def test_message_and_directory_pagination_scope_deletion(self):
        a = await self.create()
        for number in range(5):
            await self.complete(await self.store.get(1,a['conversation_id']),str(number))
        latest = await self.store.messages(1,a['conversation_id'],limit=2)
        older = await self.store.messages(1,a['conversation_id'],before=latest['next_cursor'],limit=2)
        self.assertEqual([r['sequence'] for r in latest['items']],[4,5])
        self.assertEqual([r['sequence'] for r in older['items']],[2,3])
        await self.create()
        page = await self.store.list(1,'甲',limit=1)
        # Changing the cursor row after the first page must not move its anchor.
        cursor_conversation = page['items'][0]
        await self.store.edit(1,cursor_conversation['conversation_id'],cursor_conversation['revision'],title='其他页改名')
        more = await self.store.list(1,'甲',limit=1,cursor=page['next_cursor'])
        self.assertNotEqual(page['items'][0]['conversation_id'],more['items'][0]['conversation_id'])
        await self.pool.execute("DELETE FROM knowledge_bases WHERE user_id=1 AND knowledge_base_id='甲'")
        with self.assertRaises(LookupError):
            await self.store.messages(1,a['conversation_id'])
        self.assertEqual(await self.pool.fetchval('SELECT count(*) FROM chat_turns'),5)

    async def test_legacy_mixed_scopes_only_indexed_without_reassignment(self):
        for kb in ('甲','乙'):
            trace = RunTrace(1,'mixed-old','旧问题',kb)
            trace.finish('旧回答')
            await self.audit.save_run(trace.snapshot())
        await self.complete(await self.create())
        legacy = await self.store.legacy_sessions(1)
        self.assertEqual(len(legacy),1)
        self.assertEqual(legacy[0]['scopes'],['乙','甲'])
        self.assertTrue(legacy[0]['read_only'])
        self.assertEqual(await self.store.legacy_sessions(2),[])

    async def test_quotas_allow_separate_conversations_and_reject_extra_execution(self):
        conversations = [await self.create() for _ in range(3)]
        with patch.dict(os.environ, {'CHAT_MAX_ACTIVE_PER_USER':'2'}):
            results = await asyncio.gather(*(self.accept(c) for c in conversations),return_exceptions=True)
        self.assertEqual(sum(isinstance(r,ConversationConflict) for r in results),1)
        self.assertEqual(await self.pool.fetchval("SELECT count(*) FROM chat_turns WHERE status='running'"),2)

    async def test_history_timeout_prevents_model_start_and_persists_failure(self):
        from fastapi import FastAPI
        from httpx import ASGITransport,AsyncClient
        import conversations_api
        a = await self.create()
        app = FastAPI()
        app.include_router(conversations_api.router)
        app.state.conversation_store=self.store
        app.state.evaluation_store=self.audit
        app.state.user_settings_store=SimpleNamespace(get=AsyncMock(return_value={
            'values':SettingsValues(memory_enabled=False).model_dump(),'revision':1}))
        async def slow_history(*args,**kwargs):
            await asyncio.sleep(3)
        async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
            with patch.object(conversations_api.ChatLimits,'from_env',return_value=SimpleNamespace(total_seconds=2)),\
                 patch.object(self.store,'history',side_effect=slow_history),\
                 patch.object(conversations_api,'stream_agent_response') as stream:
                result=await client.post(f"/conversations/{a['conversation_id']}/turns/stream?user_id=1",
                    json={'query':'受限请求','client_turn_id':str(uuid4()),'expected_revision':1})
            stream.assert_not_called()
            self.assertEqual(result.status_code,200)
            self.assertEqual(json.loads(result.text.splitlines()[-1])['status'],'failed')
            self.assertEqual((await self.store.messages(1,a['conversation_id']))['items'][0]['status'],'failed')

    async def test_memory_copy_is_derived_and_run_deletion_cannot_remove_messages(self):
        a = await self.create()
        accepted, trace = await self.accept(a)
        trace.config['memory_enabled'] = True
        trace.finish('记忆回答')
        await self.store.save_run(trace.snapshot())
        self.assertEqual(await self.pool.fetchval('SELECT count(*) FROM raw_conversations'),2)
        import asyncpg
        with self.assertRaises(asyncpg.ForeignKeyViolationError):
            await self.pool.execute('DELETE FROM evaluation_runs WHERE run_id=$1',UUID(trace.run_id))
        self.assertEqual(len((await self.store.messages(1,a['conversation_id']))['items']),1)

    async def test_real_api_stream_offline_model_and_replay_do_not_repeat_model_call(self):
        from fastapi import FastAPI
        from httpx import ASGITransport, AsyncClient
        from langgraph.checkpoint.memory import InMemorySaver
        from test_skill_multiturn import RecordingModel
        import agent
        import conversations_api
        app = FastAPI()
        app.include_router(conversations_api.router)
        app.state.conversation_store = self.store
        app.state.evaluation_store = self.audit
        app.state.user_settings_store = SimpleNamespace(get=AsyncMock(return_value={
            'values':SettingsValues(memory_enabled=False).model_dump(),'revision':1}))
        model = RecordingModel()
        # A new cache on every request simulates checkpoint loss between turns.
        @asynccontextmanager
        async def connection(**kwargs):
            saver = InMemorySaver()
            saver.asetup = AsyncMock()
            yield saver
        async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
            a = (await client.post('/conversations?user_id=1',json={'scope_id':'甲','client_create_id':str(uuid4())})).json()
            path = f"/conversations/{a['conversation_id']}/turns/stream?user_id=1"
            first = {'query':'&摘要 用户约束甲','client_turn_id':str(uuid4()),'expected_revision':1}
            with patch.object(agent,'model',model),patch.object(agent.AsyncRedisSaver,'from_conn_string',side_effect=connection):
                result = await client.post(path,json=first)
                self.assertEqual(result.status_code,200,result.text)
                events = [json.loads(line) for line in result.text.splitlines()]
                self.assertEqual(events[-1]['status'],'complete',events)
                replay = await client.post(path,json=first)
                self.assertEqual(replay.status_code,200)
                self.assertEqual(len(model.seen),1)
                state = (await client.get(f"/conversations/{a['conversation_id']}?user_id=1")).json()
                second = await client.post(path,json={'query':'继续，记住约束','client_turn_id':str(uuid4()),'expected_revision':state['revision']})
                self.assertEqual(json.loads(second.text.splitlines()[-1])['status'],'complete',second.text)
            self.assertEqual(len(model.seen),2)
            self.assertIn('用户约束甲',str([m.content for m in model.seen[-1]]))
            self.assertNotIn('本轮技能工作流',str([m.content for m in model.seen[-1] if m.type=='system']))
            denied = await client.get(f"/conversations/{a['conversation_id']}/messages?user_id=2")
            self.assertEqual(denied.status_code,404)
            invalid = await client.post(path,json={**first,'query':'改写内容'})
            self.assertEqual(invalid.status_code,409)
            self.assertEqual(len(model.seen),2)
            run = await self.audit.get_run(events[0]['run_id'],1)
            self.assertTrue(run['config']['context_packs'])
            self.assertEqual(run['config']['context_source'],'postgresql_completed_turns')

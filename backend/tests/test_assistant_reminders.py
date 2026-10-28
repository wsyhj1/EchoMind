"""Real isolated PostgreSQL: clock catch-up, atomic delivery, retries and ownership."""
import asyncio
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

sys.path.insert(0,str(Path(__file__).resolve().parent))
import test_personal_assistant as fixtures
from assistant.models import ACTION
from assistant.reminders import CreateReminder, ReadNotification, ReminderDecision, ReminderStore, SnoozeReminder, run_reminder_worker
from assistant.store import AssistantConflict


def future():
    return datetime.now(ZoneInfo('Asia/Shanghai'))+timedelta(hours=1)


class ReminderValidationTests(unittest.TestCase):
    def test_aware_timezone_and_dst_validation(self):
        data = {'operation_id':uuid4(),'title':'复盘','due_at':future()}
        self.assertEqual(CreateReminder(**data).timezone,'Asia/Shanghai')
        for change in ({'due_at':'2026-11-01T09:00:00'}, {'timezone':'not-a-zone'},
                       {'due_at':'2026-11-01T09:00:00+00:00'},
                       {'due_at':'2027-03-14T02:30:00-05:00','timezone':'America/New_York'},
                       {'user_id':2},{'title':'  '}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                CreateReminder(**{**data,**change})
        # Both repeated-hour instants are explicit and valid.
        for offset in ('-04:00','-05:00'):
            CreateReminder(**{**data,'due_at':'2026-11-01T01:30:00'+offset,'timezone':'America/New_York'})


@unittest.skipUnless(os.getenv('ECHOMIND_TEST_DATABASE_URL'),'Isolated PostgreSQL required')
class ReminderStoreTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = fixtures.AssistantStoreTests.asyncTearDown
    project = fixtures.AssistantStoreTests.project
    apply = fixtures.AssistantStoreTests.apply

    async def asyncSetUp(self):
        await fixtures.AssistantStoreTests.asyncSetUp(self)
        self.reminders = ReminderStore(self.pool)
        await self.reminders.initialize()

    async def reminder(self, project, **changes):
        payload = CreateReminder(operation_id=uuid4(),title='复盘今天',due_at=future(),**changes)
        return await self.reminders.create(project['user_id'],project['project_id'],payload)

    async def overdue(self, reminder):
        await self.pool.execute("UPDATE assistant_reminders SET due_at=clock_timestamp()-interval '1 hour' WHERE reminder_id=$1",UUID(reminder['reminder_id']))

    async def test_create_retry_after_due_and_independent_project_revision(self):
        p = await self.project()
        payload = CreateReminder(operation_id=uuid4(),title='复盘',due_at=future())
        first,second = await asyncio.gather(*(self.reminders.create(1,p['project_id'],payload) for _ in range(2)))
        self.assertEqual(first['reminder_id'],second['reminder_id'])
        self.assertEqual(sum(r['replayed'] for r in (first,second)),1)
        await self.overdue(first)
        await self.reminders.dispatch_due()
        self.assertTrue((await self.reminders.create(1,p['project_id'],payload))['replayed'])
        with self.assertRaises(AssistantConflict):
            await self.reminders.create(1,p['project_id'],payload.model_copy(update={'title':'不同内容'}))
        self.assertEqual((await self.store.workspace(1,p['project_id']))['project']['revision'],1)
        self.assertEqual(await self.pool.fetchval('SELECT count(*) FROM assistant_events'),1)

    async def test_restart_catch_up_and_two_workers_deliver_once(self):
        p = await self.project()
        r = await self.reminder(p)
        await self.overdue(r)
        restarted = ReminderStore(self.pool)
        await restarted.initialize()
        results = await asyncio.gather(self.reminders.dispatch_due(),restarted.dispatch_due())
        self.assertEqual(sum(x['delivered'] for x in results),1)
        self.assertEqual((await restarted.dispatch_due())['delivered'],0)
        inbox = await restarted.inbox(1)
        self.assertEqual((inbox['unread_count'],len(inbox['items'])),(1,1))
        item = inbox['items'][0]
        self.assertGreater(datetime.fromisoformat(item['delivered_at']),datetime.fromisoformat(item['due_at']))
        self.assertTrue((await restarted.status())['healthy'])

    async def test_delivery_and_inbox_rollback_together(self):
        p = await self.project()
        r = await self.reminder(p)
        await self.overdue(r)
        await self.pool.execute("""CREATE FUNCTION fail_delivery() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN IF NEW.status='delivered' THEN RAISE EXCEPTION 'injected interruption'; END IF; RETURN NEW; END $$;
            CREATE TRIGGER fail_delivery BEFORE UPDATE ON assistant_reminders FOR EACH ROW EXECUTE FUNCTION fail_delivery();""")
        with self.assertRaises(Exception): await self.reminders.dispatch_due()
        self.assertEqual(await self.pool.fetchval('SELECT count(*) FROM assistant_notifications'),0)
        self.assertEqual(await self.pool.fetchval('SELECT status FROM assistant_reminders'),'scheduled')
        await self.pool.execute('DROP TRIGGER fail_delivery ON assistant_reminders')
        self.assertEqual((await self.reminders.dispatch_due())['delivered'],1)

    async def test_cancel_before_due_and_racing_delivery_are_serialized(self):
        p = await self.project()
        first = await self.reminder(p)
        payload = ReminderDecision(operation_id=uuid4(),expected_revision=1)
        cancelled = await self.reminders.change(1,p['project_id'],first['reminder_id'],'cancel',payload)
        self.assertEqual(cancelled['status'],'cancelled')
        self.assertTrue((await self.reminders.change(1,p['project_id'],first['reminder_id'],'cancel',payload))['replayed'])
        await self.overdue(first)
        self.assertEqual((await self.reminders.dispatch_due())['delivered'],0)
        second = await self.reminder(p)
        await self.overdue(second)
        result = await asyncio.gather(self.reminders.dispatch_due(),self.reminders.change(1,p['project_id'],second['reminder_id'],
            'cancel',ReminderDecision(operation_id=uuid4(),expected_revision=1)),return_exceptions=True)
        row = await self.pool.fetchrow('SELECT * FROM assistant_reminders WHERE reminder_id=$1',UUID(second['reminder_id']))
        if isinstance(result[1],AssistantConflict):
            self.assertEqual(row['status'],'delivered')
            await self.reminders.change(1,p['project_id'],second['reminder_id'],'cancel',ReminderDecision(operation_id=uuid4(),expected_revision=row['revision']))
        else:
            self.assertEqual(row['status'],'cancelled')
        self.assertEqual((await self.reminders.inbox(1))['unread_count'],0)

    async def test_snooze_creates_next_occurrence_without_duplicate_or_task_mutation(self):
        p = await self.project()
        task = await self.apply(p,{'kind':'add_task','title':'学习事务','category':'study'})
        r = await self.reminder(task['project'],task_id=task['entity']['task_id'])
        await self.overdue(r)
        await self.reminders.dispatch_due()
        payload = SnoozeReminder(operation_id=uuid4(),expected_revision=2,due_at=future())
        snoozed = await self.reminders.change(1,p['project_id'],r['reminder_id'],'snooze',payload)
        self.assertEqual((snoozed['generation'],snoozed['status']),(2,'scheduled'))
        self.assertTrue((await self.reminders.change(1,p['project_id'],r['reminder_id'],'snooze',payload))['replayed'])
        self.assertEqual((await self.reminders.inbox(1))['unread_count'],0)
        await self.overdue(r)
        await self.reminders.dispatch_due()
        inbox = await self.reminders.inbox(1,unread=False)
        self.assertEqual((len(inbox['items']),inbox['unread_count']),(2,1))
        self.assertEqual({n['generation'] for n in inbox['items']},{1,2})
        self.assertEqual((await self.store.workspace(1,p['project_id']))['tasks'][0]['status'],'todo')
        with self.assertRaises(AssistantConflict):
            await self.reminders.change(1,p['project_id'],r['reminder_id'],'snooze',payload.model_copy(update={'operation_id':uuid4()}))

    async def test_closed_task_archived_project_and_deleted_scope_suppress_at_delivery(self):
        task_project = await self.project()
        task = await self.apply(task_project,{'kind':'add_task','title':'已处理事项'})
        r1 = await self.reminder(task['project'],task_id=task['entity']['task_id'])
        await self.apply(task['project'],{'kind':'update_task','task_id':task['entity']['task_id'],'status':'done','completion_note':'已验证'})
        archived = await self.project('归档')
        r2 = await self.reminder(archived)
        await self.apply(archived,{'kind':'edit_project','archived':True})
        scoped = await self.project('范围删除',scope='乙')
        r3 = await self.reminder(scoped)
        await self.pool.execute("DELETE FROM knowledge_bases WHERE user_id=1 AND knowledge_base_id='乙'")
        for reminder in (r1,r2,r3): await self.overdue(reminder)
        self.assertEqual(await self.reminders.dispatch_due(),{'delivered':0,'cancelled':3})
        self.assertEqual(set(await self.pool.fetch('SELECT reason FROM assistant_reminders')),set(await self.pool.fetch("SELECT unnest(ARRAY['task_closed','project_archived','scope_unavailable']) AS reason")))
        self.assertEqual((await self.reminders.inbox(1))['items'],[])

    async def test_ownership_cross_project_scope_and_read_isolation(self):
        a,b = await self.project(),await self.project('乙项目')
        task = await self.apply(a,{'kind':'add_task','title':'私有'})
        with self.assertRaises(LookupError): await self.reminder(b,task_id=task['entity']['task_id'])
        r = await self.reminder(a)
        with self.assertRaises(LookupError): await self.reminders.list(2,a['project_id'])
        with self.assertRaises(LookupError):
            await self.reminders.change(1,b['project_id'],r['reminder_id'],'cancel',ReminderDecision(operation_id=uuid4(),expected_revision=1))
        await self.overdue(r)
        await self.reminders.dispatch_due()
        self.assertEqual((await self.reminders.inbox(2))['unread_count'],0)
        n = (await self.reminders.inbox(1))['items'][0]
        payload = ReadNotification(operation_id=uuid4())
        with self.assertRaises(LookupError): await self.reminders.read(2,n['notification_id'],payload)
        read = await self.reminders.read(1,n['notification_id'],payload)
        self.assertIsNotNone(read['read_at'])
        self.assertTrue((await self.reminders.read(1,n['notification_id'],payload))['replayed'])
        await self.pool.execute("DELETE FROM knowledge_bases WHERE user_id=1 AND knowledge_base_id='甲'")
        self.assertEqual((await self.reminders.inbox(1,unread=False))['items'],[])
        with self.assertRaises(LookupError): await self.reminders.read(1,n['notification_id'],payload)

    async def test_future_horizon_pagination_and_batch_limit(self):
        p = await self.project()
        for due in (future()-timedelta(days=1),future()+timedelta(days=366)):
            with self.assertRaises(AssistantConflict):
                await self.reminders.create(1,p['project_id'],CreateReminder(operation_id=uuid4(),title='越界',due_at=due))
        reminders = [await self.reminder(p) for _ in range(3)]
        for r in reminders: await self.overdue(r)
        self.assertEqual((await self.reminders.dispatch_due(batch_size=2))['delivered'],2)
        self.assertEqual((await self.reminders.dispatch_due(batch_size=2))['delivered'],1)
        listing = await self.reminders.list(1,p['project_id'],limit=2)
        self.assertTrue(listing['has_more'])
        last = await self.reminders.list(1,p['project_id'],limit=2,offset=2)
        self.assertEqual(len(last['items']),1)
        first = await self.reminders.inbox(1,limit=2)
        second = await self.reminders.inbox(1,limit=2,offset=2)
        self.assertEqual(first['unread_count'],3)
        self.assertFalse(set(x['notification_id'] for x in first['items']) & set(x['notification_id'] for x in second['items']))

    async def test_worker_loop_recovers_from_failure_and_cancels_cleanly(self):
        p = await self.project()
        r = await self.reminder(p)
        await self.overdue(r)
        dispatch = self.reminders.dispatch_due
        calls = 0
        async def intermittent():
            nonlocal calls
            calls += 1
            if calls==1: raise RuntimeError('injected transient failure')
            return await dispatch()
        with patch.object(self.reminders,'dispatch_due',side_effect=intermittent), self.assertLogs('assistant.reminders',level='ERROR'):
            worker = asyncio.create_task(run_reminder_worker(self.reminders,interval=.01))
            try:
                async with asyncio.timeout(3):
                    while not (await self.reminders.inbox(1))['unread_count']:
                        await asyncio.sleep(.01)
            finally:
                worker.cancel()
                with self.assertRaises(asyncio.CancelledError): await worker
        self.assertGreaterEqual(calls,2)

    async def test_http_validation_identity_conflicts_and_inbox(self):
        from fastapi import FastAPI
        from httpx import ASGITransport, AsyncClient
        from auth import IdentityMiddleware
        from assistant_api import router
        p = await self.project()
        app = FastAPI()
        app.state.assistant_store = self.store
        app.state.reminder_store = self.reminders
        app.include_router(router)
        app.add_middleware(IdentityMiddleware)
        with patch.dict(os.environ,{'ECHOMIND_AUTH_MODE':'local','ECHOMIND_LOCAL_USER_ID':'1'}):
            async with AsyncClient(transport=ASGITransport(app=app,client=('127.0.0.1',1234)),base_url='http://127.0.0.1') as client:
                path = f"/assistant/projects/{p['project_id']}/reminders"
                data = {'operation_id':str(uuid4()),'title':'HTTP提醒','due_at':future().isoformat()}
                self.assertEqual((await client.post(path,json={**data,'due_at':'2026-12-01T12:00:00'})).status_code,422)
                self.assertEqual((await client.post(path,json={**data,'user_id':2})).status_code,422)
                self.assertEqual((await client.get('/assistant/notifications',params={'user_id':2})).status_code,403)
                response = await client.post(path,json=data)
                self.assertEqual(response.status_code,201,response.text)
                r = response.json()
                self.assertTrue((await client.post(path,json=data)).json()['replayed'])
                await self.overdue(r)
                await self.reminders.dispatch_due()
                self.assertTrue((await client.get('/assistant/reminders/status')).json()['healthy'])
                n = (await client.get('/assistant/notifications')).json()['items'][0]
                stale = await client.post(path+'/'+r['reminder_id']+'/cancel',json={'operation_id':str(uuid4()),'expected_revision':1})
                self.assertEqual(stale.status_code,409)
                read = await client.post('/assistant/notifications/'+n['notification_id']+'/read',json={'operation_id':str(uuid4())})
                self.assertEqual(read.status_code,200,read.text)
                self.assertEqual((await client.get('/assistant/notifications')).json()['unread_count'],0)

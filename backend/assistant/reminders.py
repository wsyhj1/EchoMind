"""Durable in-app delivery; no provider, browser session, or external notification required."""
import asyncio
from datetime import datetime, timedelta
import logging
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, Field, model_validator

from assistant.models import StrictModel
from assistant.store import AssistantConflict, AssistantStore, decode, encode, fingerprint, public


class ReminderTime(StrictModel):
    due_at: AwareDatetime
    timezone: str = Field(default='Asia/Shanghai', max_length=100)

    @model_validator(mode='after')
    def valid_zone(self):
        try:
            zone = ZoneInfo(self.timezone)
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError('请使用有效的IANA时区，例如Asia/Shanghai。') from None
        if self.due_at.utcoffset() != self.due_at.astimezone(zone).utcoffset():
            raise ValueError('时间偏移与所选时区不符，请核对当地时间。')
        return self


class CreateReminder(ReminderTime):
    operation_id: UUID
    title: str = Field(min_length=1, max_length=200)
    task_id: UUID | None = None


class ReminderDecision(StrictModel):
    operation_id: UUID
    expected_revision: int = Field(ge=1, strict=True)


class SnoozeReminder(ReminderTime, ReminderDecision):
    pass


class ReadNotification(StrictModel):
    operation_id: UUID


class ReminderStore:
    def __init__(self, pool):
        self.pool = pool
        self.projects = AssistantStore(pool)

    async def initialize(self):
        await self.pool.execute((Path(__file__).parents[1]/'migrations/011_assistant_reminders.sql').read_text('utf-8'))

    async def _receipt(self, conn, user, key, mark):
        await conn.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,0))', f'reminder-op:{user}:{key}')
        old = await conn.fetchrow('SELECT * FROM assistant_reminder_operations WHERE user_id=$1 AND operation_id=$2', user, key)
        if old:
            if old['fingerprint'] != mark:
                raise AssistantConflict('此提醒操作标识已用于不同内容。')
            return {**decode(old['result']), 'replayed': True}

    async def _save(self, conn, user, key, mark, row):
        result = {**public(row), 'replayed': False}
        await conn.execute('''INSERT INTO assistant_reminder_operations(user_id,operation_id,fingerprint,result)
            VALUES($1,$2,$3,$4::jsonb)''', user, key, mark, encode(result))
        return result

    async def _time(self, conn, due):
        now = await conn.fetchval('SELECT clock_timestamp()')
        if not now < due <= now+timedelta(days=365):
            raise AssistantConflict('提醒时间须晚于现在，且在未来365天内。')

    async def _eligible(self, conn, project, task_id):
        if project['archived']:
            raise AssistantConflict('项目已归档，不能设置提醒。')
        if task_id:
            task = await conn.fetchrow('SELECT * FROM assistant_tasks WHERE project_id=$1 AND task_id=$2', project['project_id'], task_id)
            if not task:
                raise LookupError('关联任务不存在或不属于该项目。')
            if task['status'] in ('done','cancelled'):
                raise AssistantConflict('任务已完成或取消，无需设置提醒。')

    async def create(self, user, project_id, payload):
        mark = fingerprint(['create', str(project_id), payload.model_dump(mode='json')])
        async with self.pool.acquire() as conn, conn.transaction():
            project = await self.projects._project(conn, user, project_id, lock=True)
            old = await self._receipt(conn, user, payload.operation_id, mark)
            if old: return old
            await self._eligible(conn, project, payload.task_id)
            await self._time(conn, payload.due_at)
            await self._capacity(conn, project['project_id'])
            row = await conn.fetchrow('''INSERT INTO assistant_reminders
                (reminder_id,user_id,project_id,task_id,title,due_at,timezone) VALUES($1,$2,$3,$4,$5,$6,$7) RETURNING *''',
                uuid4(), user, project['project_id'], payload.task_id, payload.title, payload.due_at, payload.timezone)
            return await self._save(conn, user, payload.operation_id, mark, row)

    async def _capacity(self, conn, pid):
        if await conn.fetchval("SELECT count(*) FROM assistant_reminders WHERE project_id=$1 AND status='scheduled'", pid) >= 100:
            raise AssistantConflict('每个项目最多100项待提醒事项，请先取消不需要的提醒。')

    async def change(self, user, project_id, reminder_id, decision: Literal['cancel','snooze'], payload):
        mark = fingerprint([decision, str(project_id), str(reminder_id), payload.model_dump(mode='json')])
        async with self.pool.acquire() as conn, conn.transaction():
            project = await self.projects._project(conn, user, project_id, lock=True)
            old = await self._receipt(conn, user, payload.operation_id, mark)
            if old: return old
            row = await conn.fetchrow('SELECT * FROM assistant_reminders WHERE project_id=$1 AND reminder_id=$2 FOR UPDATE',
                project['project_id'], UUID(str(reminder_id)))
            if not row: raise LookupError('提醒不存在或不可访问。')
            if row['revision'] != payload.expected_revision:
                raise AssistantConflict('提醒已更新，请刷新后重试。')
            if row['status'] == 'cancelled': raise AssistantConflict('提醒已取消。')
            if decision == 'snooze':
                if row['status'] != 'delivered': raise AssistantConflict('只有已送达的提醒可以稍后提醒；待提醒事项可取消后重新设置。')
                await self._eligible(conn, project, row['task_id'])
                await self._time(conn, payload.due_at)
                await self._capacity(conn, project['project_id'])
                updated = await conn.fetchrow('''UPDATE assistant_reminders SET status='scheduled',due_at=$2,timezone=$3,
                    generation=generation+1,revision=revision+1,updated_at=now(),reason='' WHERE reminder_id=$1 RETURNING *''',
                    row['reminder_id'], payload.due_at, payload.timezone)
            elif decision == 'cancel':
                updated = await conn.fetchrow("""UPDATE assistant_reminders SET status='cancelled',reason='user_cancelled',
                    revision=revision+1,updated_at=now() WHERE reminder_id=$1 RETURNING *""", row['reminder_id'])
            else:
                raise ValueError('Unknown reminder decision')
            await conn.execute('UPDATE assistant_notifications SET read_at=COALESCE(read_at,now()) WHERE reminder_id=$1', row['reminder_id'])
            return await self._save(conn, user, payload.operation_id, mark, updated)

    async def list(self, user, project_id, *, offset=0, limit=30):
        async with self.pool.acquire() as conn, conn.transaction(isolation='repeatable_read', readonly=True):
            project = await self.projects._project(conn, user, project_id)
            rows = await conn.fetch('''SELECT r.*,t.title AS task_title FROM assistant_reminders r
                LEFT JOIN assistant_tasks t USING(task_id) WHERE r.project_id=$1
                ORDER BY (r.status='scheduled') DESC,r.due_at DESC,r.reminder_id LIMIT $2 OFFSET $3''',
                project['project_id'], limit+1, offset)
            return {'items':[public(row) for row in rows[:limit]],'has_more':len(rows)>limit}

    async def inbox(self, user, *, unread=True, offset=0, limit=20):
        visible = """FROM assistant_notifications n JOIN assistant_reminders r USING(reminder_id)
            JOIN assistant_projects p ON p.project_id=r.project_id WHERE r.user_id=$1 AND
            (p.scope_id='默认知识库' OR EXISTS(SELECT 1 FROM knowledge_bases k WHERE k.user_id=p.user_id AND k.knowledge_base_id=p.scope_id))"""
        async with self.pool.acquire() as conn, conn.transaction(isolation='repeatable_read', readonly=True):
            count = await conn.fetchval('SELECT count(*) '+visible+' AND n.read_at IS NULL', user)
            rows = await conn.fetch('''SELECT n.*,r.project_id,r.task_id,r.revision AS reminder_revision,
                r.status AS reminder_status,r.generation AS current_generation,r.timezone '''+visible+
                (' AND n.read_at IS NULL' if unread else '')+' ORDER BY n.delivered_at DESC,n.notification_id DESC LIMIT $2 OFFSET $3',
                user, limit+1, offset)
            return {'items':[public(row) for row in rows[:limit]],'has_more':len(rows)>limit,'unread_count':count}

    async def read(self, user, notification_id, payload):
        mark = fingerprint(['read', str(notification_id), payload.model_dump(mode='json')])
        async with self.pool.acquire() as conn, conn.transaction():
            # Resolve ownership first; lock ordering remains project -> operation -> reminder/notification.
            pid = await conn.fetchval('''SELECT r.project_id FROM assistant_notifications n
                JOIN assistant_reminders r USING(reminder_id) WHERE n.notification_id=$1 AND r.user_id=$2''', UUID(str(notification_id)), user)
            if not pid: raise LookupError('通知不存在或不可访问。')
            await self.projects._project(conn, user, pid, lock=True)
            old = await self._receipt(conn, user, payload.operation_id, mark)
            if old: return old
            row = await conn.fetchrow('UPDATE assistant_notifications SET read_at=COALESCE(read_at,now()) WHERE notification_id=$1 RETURNING *', UUID(str(notification_id)))
            return await self._save(conn, user, payload.operation_id, mark, row)

    async def status(self):
        row = await self.pool.fetchrow("""SELECT last_tick_at,clock_timestamp()-last_tick_at < interval '30 seconds' AS healthy
            FROM assistant_scheduler_state WHERE name='reminders'""")
        return public(row) if row else {'last_tick_at':None,'healthy':False}

    async def dispatch_due(self, *, batch_size=100):
        """Claim projects first, consistent with user writes; a crash rolls back inbox and delivery together."""
        delivered = cancelled = 0
        async with self.pool.acquire() as conn, conn.transaction():
            projects = await conn.fetch('''SELECT p.* FROM assistant_projects p WHERE EXISTS
                (SELECT 1 FROM assistant_reminders r WHERE r.project_id=p.project_id AND r.status='scheduled' AND r.due_at<=now())
                ORDER BY p.project_id LIMIT $1 FOR UPDATE OF p SKIP LOCKED''', batch_size)
            remaining = batch_size
            for project in projects:
                accessible = project['scope_id']=='默认知识库' or await conn.fetchval(
                    'SELECT EXISTS(SELECT 1 FROM knowledge_bases WHERE user_id=$1 AND knowledge_base_id=$2)', project['user_id'], project['scope_id'])
                rows = await conn.fetch('''SELECT r.*,t.status AS task_status,t.title AS task_title FROM assistant_reminders r
                    LEFT JOIN assistant_tasks t USING(task_id) WHERE r.project_id=$1 AND r.status='scheduled' AND r.due_at<=now()
                    ORDER BY r.due_at,r.reminder_id LIMIT $2 FOR UPDATE OF r''', project['project_id'], remaining)
                for row in rows:
                    reason = ('project_archived' if project['archived'] else 'scope_unavailable' if not accessible else
                        'task_closed' if row['task_status'] in ('done','cancelled') else '')
                    if not reason:
                        await conn.execute('''INSERT INTO assistant_notifications
                            (notification_id,reminder_id,generation,title,project_title,task_title,due_at)
                            VALUES($1,$2,$3,$4,$5,$6,$7) ON CONFLICT(reminder_id,generation) DO NOTHING''',
                            uuid4(), row['reminder_id'], row['generation'], row['title'], project['title'], row['task_title'], row['due_at'])
                        delivered += 1
                    else:
                        cancelled += 1
                    await conn.execute('''UPDATE assistant_reminders SET status=$2,reason=$3,revision=revision+1,updated_at=now()
                        WHERE reminder_id=$1''', row['reminder_id'], 'cancelled' if reason else 'delivered', reason)
                remaining -= len(rows)
                if remaining <= 0: break
            await conn.execute("""INSERT INTO assistant_scheduler_state(name,last_tick_at) VALUES('reminders',clock_timestamp())
                ON CONFLICT(name) DO UPDATE SET last_tick_at=EXCLUDED.last_tick_at""")
        return {'delivered':delivered,'cancelled':cancelled}


async def run_reminder_worker(store, *, interval=5):
    while True:
        try:
            result = await store.dispatch_due()
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.getLogger(__name__).exception('Reminder delivery failed; pending rows will retry.')
            result = {}
        # Drain a backlog without adding five seconds per full batch.
        await asyncio.sleep(0.1 if sum(result.values()) >= 100 else interval)

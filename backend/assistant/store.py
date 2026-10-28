"""Project mutations, receipts, and audit events commit in one PostgreSQL transaction."""
from datetime import date, datetime
from hashlib import sha256
import json
from pathlib import Path
from uuid import UUID, uuid4

from assistant.models import ACTION, AddNote, AddResource, AddTask, AddTasks, EditProject, UpdateTask, action_data


class AssistantConflict(ValueError):
    pass


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def decode(value):
    return json.loads(value) if isinstance(value, str) else value


def public(row):
    result = dict(row)
    for key, value in result.items():
        if isinstance(value, (UUID, date, datetime)):
            result[key] = value.isoformat() if isinstance(value, (date, datetime)) else str(value)
        elif key in ('payload', 'action', 'result') and value is not None:
            result[key] = decode(value)
    return result


def fingerprint(value):
    return sha256(encode(value).encode()).hexdigest()


class AssistantStore:
    def __init__(self, pool):
        self.pool = pool

    async def initialize(self):
        await self.pool.execute((Path(__file__).parents[1]/'migrations/010_personal_assistant.sql').read_text('utf-8'))

    async def _scope(self, conn, user_id, scope_id):
        if scope_id == '默认知识库':
            allowed = await conn.fetchval('SELECT EXISTS(SELECT 1 FROM users WHERE user_id=$1)', user_id)
        else:
            allowed = await conn.fetchval('SELECT EXISTS(SELECT 1 FROM knowledge_bases WHERE user_id=$1 AND knowledge_base_id=$2)', user_id, scope_id)
        if not allowed:
            raise LookupError('项目资料范围不存在或不可访问。')

    async def _project(self, conn, user_id, project_id, *, lock=False):
        row = await conn.fetchrow('SELECT * FROM assistant_projects WHERE user_id=$1 AND project_id=$2'+(' FOR UPDATE' if lock else ''), user_id, UUID(str(project_id)))
        if not row:
            raise LookupError('项目不存在或不可访问。')
        await self._scope(conn, user_id, row['scope_id'])
        return row

    async def create(self, user_id, payload):
        data = payload.model_dump(mode='json')
        mark = fingerprint(data)
        async with self.pool.acquire() as conn, conn.transaction():
            await self._scope(conn, user_id, payload.scope_id)
            await conn.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,0))', f'assistant-create:{user_id}')
            existing = await conn.fetchrow('SELECT * FROM assistant_projects WHERE user_id=$1 AND client_create_id=$2', user_id, payload.client_create_id)
            if existing:
                if existing['create_fingerprint'] != mark:
                    raise AssistantConflict('此创建标识已用于不同项目内容。')
                return public(existing)
            if await conn.fetchval('SELECT count(*) FROM assistant_projects WHERE user_id=$1', user_id) >= 100:
                raise AssistantConflict('项目数量已达100个，请先整理已有项目。')
            row = await conn.fetchrow('''INSERT INTO assistant_projects
                (project_id,user_id,client_create_id,create_fingerprint,title,goal,scope_id)
                VALUES($1,$2,$3,$4,$5,$6,$7) RETURNING *''', uuid4(), user_id, payload.client_create_id, mark, payload.title, payload.goal, payload.scope_id)
            await conn.execute('''INSERT INTO assistant_events(event_id,project_id,revision,kind,source,payload)
                VALUES($1,$2,1,'create_project','user_form',$3::jsonb)''', uuid4(), row['project_id'], encode({'project': public(row)}))
            return public(row)

    async def list(self, user_id, *, archived=False):
        rows = await self.pool.fetch('''SELECT p.*,
            (SELECT count(*) FROM assistant_tasks t WHERE t.project_id=p.project_id AND status NOT IN ('done','cancelled')) AS open_tasks,
            (SELECT count(*) FROM assistant_tasks t WHERE t.project_id=p.project_id AND status='done') AS done_tasks
            FROM assistant_projects p WHERE p.user_id=$1 AND p.archived=$2 AND
            (p.scope_id='默认知识库' OR EXISTS(SELECT 1 FROM knowledge_bases k WHERE k.user_id=p.user_id AND k.knowledge_base_id=p.scope_id))
            ORDER BY p.updated_at DESC,p.project_id LIMIT 100''', user_id, archived)
        return [public(row) for row in rows]

    async def workspace(self, user_id, project_id):
        # Repeatable-read binds every section to one revision without a long write lock.
        async with self.pool.acquire() as conn, conn.transaction(isolation='repeatable_read', readonly=True):
            project = await self._project(conn, user_id, project_id)
            pid = project['project_id']
            tasks = await conn.fetch('SELECT * FROM assistant_tasks WHERE project_id=$1 ORDER BY created_at,task_id LIMIT 200', pid)
            notes = await conn.fetch('SELECT * FROM assistant_notes WHERE project_id=$1 AND superseded_by IS NULL ORDER BY created_at DESC,note_id DESC LIMIT 200', pid)
            resources = await conn.fetch('''SELECT r.*,
                CASE WHEN r.resource_type='link' THEN true ELSE EXISTS(SELECT 1 FROM file_metadata f
                WHERE f.user_id=$2 AND f.knowledge_base_id=r.knowledge_base_id AND f.file_hash=r.reference) END AS available
                FROM assistant_resources r WHERE r.project_id=$1 ORDER BY r.created_at DESC LIMIT 100''', pid, user_id)
            proposals = await conn.fetch("SELECT * FROM assistant_proposals WHERE project_id=$1 AND status='pending' ORDER BY created_at DESC LIMIT 100", pid)
            events = await conn.fetch('SELECT * FROM assistant_events WHERE project_id=$1 ORDER BY revision DESC LIMIT 31', pid)
            conversations = await conn.fetch('''SELECT c.conversation_id,c.title,c.updated_at,c.archived FROM chat_conversations c
                JOIN assistant_project_conversations a USING(conversation_id) WHERE a.user_id=$1 AND a.project_id=$2
                ORDER BY c.updated_at DESC LIMIT 20''', user_id, pid)
            return {'project': public(project), 'tasks': [public(r) for r in tasks], 'notes': [public(r) for r in notes],
                'resources': [public(r) for r in resources], 'proposals': [public(r) for r in proposals],
                'events': [public(r) for r in events[:30]], 'events_has_more': len(events)>30,
                'conversations': [public(r) for r in conversations]}

    async def _source(self, conn, user_id, project_id, source_turn_id, *, active=False):
        if source_turn_id is None:
            return
        row = await conn.fetchrow('''SELECT t.status,t.lease_until FROM chat_turns t
            JOIN assistant_project_conversations a USING(conversation_id)
            WHERE t.turn_id=$1 AND a.user_id=$2 AND a.project_id=$3''', UUID(str(source_turn_id)), user_id, project_id)
        if not row:
            raise LookupError('来源轮次不属于当前项目。')
        if active and not await conn.fetchval('''SELECT EXISTS(SELECT 1 FROM chat_turns
            WHERE turn_id=$1 AND status='running' AND lease_until>=clock_timestamp())''', UUID(str(source_turn_id))):
            raise AssistantConflict('来源执行已结束，未保存迟到候选。')

    async def _receipt(self, conn, user_id, project_id, operation_id, mark):
        await conn.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,0))', f'assistant-op:{user_id}:{operation_id}')
        old = await conn.fetchrow('SELECT * FROM assistant_operations WHERE user_id=$1 AND operation_id=$2', user_id, UUID(str(operation_id)))
        if old:
            if old['project_id'] != project_id or old['fingerprint'] != mark:
                raise AssistantConflict('此操作标识已用于不同内容。')
            return {**decode(old['result']), 'replayed': True}

    async def _capacity(self, conn, table, project_id, limit):
        # Only internal literal table names reach this query.
        if table not in ('assistant_tasks', 'assistant_notes', 'assistant_resources'):
            raise ValueError('Unknown capacity table')
        if await conn.fetchval(f'SELECT count(*) FROM {table} WHERE project_id=$1', project_id) >= limit:
            raise AssistantConflict(f'本项目此类记录已达{limit}条上限。')

    async def apply(self, user_id, project_id, operation_id, expected_revision, action, *, source='user_form'):
        if source not in ('user_form', 'user_command'):
            raise ValueError('Invalid direct action source')
        async with self.pool.acquire() as conn, conn.transaction():
            project = await self._project(conn, user_id, project_id, lock=True)
            mark = fingerprint({'action': action_data(action), 'source': source, 'expected_revision': expected_revision})
            previous = await self._receipt(conn, user_id, project['project_id'], operation_id, mark)
            if previous:
                return previous
            return await self._mutate(conn, project, operation_id, mark, expected_revision, action, source, None)

    async def _mutate(self, conn, project, operation_id, mark, expected_revision, action, source, source_turn_id):
        pid = project['project_id']
        if project['revision'] != expected_revision:
            raise AssistantConflict('项目已发生变化，请刷新后核对再操作。')
        if project['archived'] and not (isinstance(action, EditProject) and action.archived is False):
            raise AssistantConflict('项目已归档，请先恢复项目。')
        await self._source(conn, project['user_id'], pid, source_turn_id)
        before = None
        if isinstance(action, AddTask):
            await self._capacity(conn, 'assistant_tasks', pid, 200)
            entity = await self._insert_task(conn,pid,action)
        elif isinstance(action, AddTasks):
            count = await conn.fetchval('SELECT count(*) FROM assistant_tasks WHERE project_id=$1',pid)
            if count+len(action.tasks)>200:
                raise AssistantConflict('批量计划超出项目200个任务上限，整批未保存。')
            entity = {'tasks':[public(await self._insert_task(conn,pid,task)) for task in action.tasks]}
        elif isinstance(action, UpdateTask):
            old = await conn.fetchrow('SELECT * FROM assistant_tasks WHERE project_id=$1 AND task_id=$2', pid, action.task_id)
            if not old:
                raise LookupError('任务不存在或不属于当前项目。')
            before = public(old)
            changes = action.model_dump(exclude_unset=True)
            allowed = ['title','description','status','category','priority','estimated_minutes','due_date','completion_note','blocker']
            values = {key: changes.get(key, old[key]) for key in allowed}
            completed = old['completed_at']
            if 'status' in changes:
                if action.status == 'done' and old['status'] != 'done':
                    completed = await conn.fetchval('SELECT clock_timestamp()')
                elif action.status != 'done':
                    completed = None
                    values['completion_note'] = ''
                if action.status != 'blocked':
                    values['blocker'] = ''
            if values['status']=='done' and not values['completion_note'].strip():
                raise AssistantConflict('完成记录不能清空依据；需要时请重新打开任务。')
            if values['status']=='blocked' and not values['blocker'].strip():
                raise AssistantConflict('受阻任务必须保留阻碍原因。')
            if values['status']!='done':
                values['completion_note'],completed = '',None
            if values['status']!='blocked':
                values['blocker'] = ''
            entity = await conn.fetchrow('''UPDATE assistant_tasks SET title=$3,description=$4,status=$5,category=$6,
                priority=$7,estimated_minutes=$8,due_date=$9,completion_note=$10,blocker=$11,completed_at=$12,updated_at=now()
                WHERE project_id=$1 AND task_id=$2 RETURNING *''', pid, action.task_id, *[values[k] for k in allowed], completed)
        elif isinstance(action, AddNote):
            await self._capacity(conn, 'assistant_notes', pid, 200)
            if action.supersedes_note_id:
                old = await conn.fetchrow('SELECT * FROM assistant_notes WHERE project_id=$1 AND note_id=$2 AND superseded_by IS NULL', pid, action.supersedes_note_id)
                if not old or old['note_kind'] != action.note_kind:
                    raise AssistantConflict('要更正的记录不可访问、类型不同或已被更正。')
                before = public(old)
            entity = await conn.fetchrow('''INSERT INTO assistant_notes(note_id,project_id,note_kind,content,source,source_turn_id)
                VALUES($1,$2,$3,$4,$5,$6) RETURNING *''', uuid4(), pid, action.note_kind, action.content, source,
                UUID(str(source_turn_id)) if source_turn_id else None)
            if action.supersedes_note_id:
                await conn.execute('UPDATE assistant_notes SET superseded_by=$3 WHERE project_id=$1 AND note_id=$2', pid, action.supersedes_note_id, entity['note_id'])
        elif isinstance(action, AddResource):
            await self._capacity(conn, 'assistant_resources', pid, 100)
            kb = None
            if action.resource_type == 'document':
                rows = await conn.fetch('''SELECT f.knowledge_base_id FROM file_metadata f JOIN knowledge_bases k
                    ON k.user_id=f.user_id AND k.knowledge_base_id=f.knowledge_base_id
                    WHERE f.user_id=$1 AND f.file_hash=$2 AND ($3='默认知识库' OR f.knowledge_base_id=$3)''', project['user_id'], action.reference, project['scope_id'])
                if len(rows) != 1:
                    raise LookupError('文档不存在、不在项目范围内或来源不唯一。')
                kb = rows[0]['knowledge_base_id']
            if await conn.fetchval('SELECT EXISTS(SELECT 1 FROM assistant_resources WHERE project_id=$1 AND resource_type=$2 AND reference=$3)', pid, action.resource_type, action.reference):
                raise AssistantConflict('此资料已关联到项目。')
            entity = await conn.fetchrow('''INSERT INTO assistant_resources(resource_id,project_id,resource_type,label,reference,knowledge_base_id)
                VALUES($1,$2,$3,$4,$5,$6) RETURNING *''', uuid4(), pid, action.resource_type, action.label, action.reference, kb)
        elif isinstance(action, EditProject):
            before = public(project)
            changes = action.model_dump(exclude_unset=True)
            entity = await conn.fetchrow('''UPDATE assistant_projects SET title=$3,goal=$4,archived=$5
                WHERE user_id=$1 AND project_id=$2 RETURNING *''', project['user_id'], pid,
                changes.get('title', project['title']), changes.get('goal', project['goal']), changes.get('archived', project['archived']))
        else:
            raise ValueError('Unknown action')
        updated = await conn.fetchrow('''UPDATE assistant_projects SET revision=revision+1,updated_at=now()
            WHERE user_id=$1 AND project_id=$2 RETURNING *''', project['user_id'], pid)
        event_id = uuid4()
        result = {'project': public(updated), 'entity': public(entity), 'event_id': str(event_id), 'replayed': False}
        await conn.execute('''INSERT INTO assistant_events(event_id,project_id,revision,kind,source,source_turn_id,payload)
            VALUES($1,$2,$3,$4,$5,$6,$7::jsonb)''', event_id, pid, updated['revision'], action.kind, source,
            UUID(str(source_turn_id)) if source_turn_id else None, encode({'action': action_data(action), 'before': before, 'entity': public(entity)}))
        await conn.execute('''INSERT INTO assistant_operations(user_id,operation_id,project_id,fingerprint,result)
            VALUES($1,$2,$3,$4,$5::jsonb)''', project['user_id'], UUID(str(operation_id)), pid, mark, encode(result))
        return result

    async def _insert_task(self,conn,project_id,task):
        return await conn.fetchrow('''INSERT INTO assistant_tasks
            (task_id,project_id,title,description,category,priority,estimated_minutes,due_date)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8) RETURNING *''',uuid4(),project_id,task.title,task.description,
            task.category,task.priority,task.estimated_minutes,task.due_date)

    async def propose(self, user_id, project_id, request_key, expected_revision, action, source_turn_id):
        async with self.pool.acquire() as conn, conn.transaction():
            project = await self._project(conn, user_id, project_id, lock=True)
            mark = fingerprint({'project_id': str(project_id), 'revision': expected_revision, 'action': action_data(action), 'turn': str(source_turn_id)})
            old = await conn.fetchrow('SELECT * FROM assistant_proposals WHERE user_id=$1 AND request_key=$2', user_id, request_key)
            if old:
                if old['fingerprint'] != mark:
                    raise AssistantConflict('候选标识已用于不同内容。')
                return public(old)
            if project['archived'] or project['revision'] != expected_revision:
                raise AssistantConflict('项目状态已变化，请重新读取后生成候选。')
            await self._source(conn, user_id, project['project_id'], source_turn_id, active=True)
            if await conn.fetchval("SELECT count(*) FROM assistant_proposals WHERE project_id=$1 AND status='pending'", project['project_id']) >= 100:
                raise AssistantConflict('待处理候选已达100条，请先处理。')
            row = await conn.fetchrow('''INSERT INTO assistant_proposals
                (proposal_id,user_id,project_id,request_key,fingerprint,base_revision,action,source_turn_id)
                VALUES($1,$2,$3,$4,$5,$6,$7::jsonb,$8) RETURNING *''', uuid4(), user_id, project['project_id'], request_key,
                mark, expected_revision, encode(action_data(action)), UUID(str(source_turn_id)))
            return public(row)

    async def decide(self, user_id, project_id, proposal_id, decision, operation_id, expected_revision):
        if decision not in ('apply', 'reject'):
            raise ValueError('Unknown proposal decision')
        async with self.pool.acquire() as conn, conn.transaction():
            project = await self._project(conn, user_id, project_id, lock=True)
            pid = project['project_id']
            mark = fingerprint({'proposal_id': str(proposal_id), 'decision': decision, 'revision': expected_revision})
            previous = await self._receipt(conn, user_id, pid, operation_id, mark)
            if previous:
                return previous
            row = await conn.fetchrow('SELECT * FROM assistant_proposals WHERE user_id=$1 AND project_id=$2 AND proposal_id=$3 FOR UPDATE', user_id, pid, UUID(str(proposal_id)))
            if not row:
                raise LookupError('候选不存在或不可访问。')
            if row['status'] != 'pending':
                raise AssistantConflict('此候选已处理，请刷新查看结果。')
            if decision == 'apply':
                if row['base_revision'] != expected_revision:
                    raise AssistantConflict('候选依据的项目版本已变化，请核对后重新生成。')
                result = await self._mutate(conn, project, operation_id, mark, expected_revision,
                    ACTION.validate_python(decode(row['action'])), 'assistant_proposal', row['source_turn_id'])
                status = 'applied'
            else:
                if project['revision'] != expected_revision:
                    raise AssistantConflict('项目已变化，请刷新后重试。')
                result = {'project': public(project), 'proposal_id': str(proposal_id), 'status': 'rejected', 'replayed': False}
                status = 'rejected'
                await conn.execute('INSERT INTO assistant_operations(user_id,operation_id,project_id,fingerprint,result) VALUES($1,$2,$3,$4,$5::jsonb)', user_id, operation_id, pid, mark, encode(result))
            await conn.execute('UPDATE assistant_proposals SET status=$2,result=$3::jsonb,decided_at=now() WHERE proposal_id=$1', row['proposal_id'], status, encode(result))
            return result

    async def create_conversation(self, user_id, project_id, scope_id, client_create_id):
        async with self.pool.acquire() as conn, conn.transaction():
            project = await self._project(conn, user_id, project_id, lock=True)
            if project['scope_id'] != scope_id or project['archived']:
                raise AssistantConflict('项目已归档或会话资料范围与项目不同。')
            await conn.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,0))', f'assistant-conversation:{user_id}:{client_create_id}')
            existing = await conn.fetchrow('SELECT c.*,a.project_id FROM chat_conversations c LEFT JOIN assistant_project_conversations a USING(conversation_id) WHERE c.user_id=$1 AND c.client_create_id=$2', user_id, client_create_id)
            if existing:
                if existing['scope_id'] != scope_id or existing['project_id'] != project['project_id']:
                    raise AssistantConflict('此会话创建标识已用于其他项目或范围。')
                return public(existing)
            cid = uuid4()
            row = await conn.fetchrow('''INSERT INTO chat_conversations(conversation_id,user_id,session_id,client_create_id,scope_id)
                VALUES($1,$2,$3,$4,$5) ON CONFLICT(user_id,client_create_id) DO NOTHING RETURNING *''', cid, user_id, 'c_'+cid.hex, client_create_id, scope_id)
            if not row:
                raise AssistantConflict('此会话创建标识已用于其他会话，请核对后重试。')
            await conn.execute('INSERT INTO assistant_project_conversations(conversation_id,user_id,project_id) VALUES($1,$2,$3)', cid, user_id, project['project_id'])
            return {**public(row), 'project_id': str(project['project_id'])}

    async def for_conversation(self, user_id, conversation_id):
        pid = await self.pool.fetchval('SELECT project_id FROM assistant_project_conversations WHERE user_id=$1 AND conversation_id=$2', user_id, UUID(str(conversation_id)))
        return await self.workspace(user_id, pid) if pid else None

    async def events(self, user_id, project_id, *, before_revision=None, since=None, limit=100):
        async with self.pool.acquire() as conn:
            project = await self._project(conn, user_id, project_id)
            rows = await conn.fetch('''SELECT * FROM assistant_events WHERE project_id=$1
                AND ($2::bigint IS NULL OR revision<$2) AND ($3::timestamptz IS NULL OR created_at>=$3)
                ORDER BY revision DESC LIMIT $4''', project['project_id'], before_revision, since, limit+1)
            return {'items': [public(r) for r in rows[:limit]], 'has_more': len(rows)>limit,
                'next_cursor': rows[limit-1]['revision'] if len(rows)>limit else None}

"""User-owned conversations and durable, ordered turns. No provider calls."""
import hashlib
import base64
import json
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4, uuid5, NAMESPACE_URL

from evaluation_store import decode, encode
from runtime_limits import bounded

DEFAULT_SCOPE = '默认知识库'


class ConversationConflict(ValueError):
    pass


def public_conversation(row):
    result = dict(row)
    for key in ('conversation_id', 'client_create_id', 'created_at', 'updated_at'):
        result[key] = str(result[key])
    return result


class ConversationStore:
    def __init__(self, pool):
        self.pool = pool

    async def initialize(self):
        await self.pool.execute((Path(__file__).parent/'migrations/009_conversations.sql').read_text(encoding='utf-8'))

    async def _check_scope(self, conn, user_id, scope_id):
        if scope_id == DEFAULT_SCOPE:
            exists = await conn.fetchval('SELECT EXISTS(SELECT 1 FROM users WHERE user_id=$1)', user_id)
        else:
            exists = await conn.fetchval('''SELECT EXISTS(SELECT 1 FROM knowledge_bases
                WHERE user_id=$1 AND knowledge_base_id=$2)''', user_id, scope_id)
        if not exists:
            raise LookupError('知识范围不可访问，请重新选择知识库。')

    async def _get(self, conn, user_id, conversation_id, lock=False):
        row = await conn.fetchrow('''SELECT * FROM chat_conversations
            WHERE user_id=$1 AND conversation_id=$2''' + (' FOR UPDATE' if lock else ''),
            user_id, UUID(str(conversation_id)))
        if not row:
            raise LookupError('会话不存在或不可访问。')
        # Preserve records when a library is removed, but fail closed on reads too.
        if not row['read_only']:
            await self._check_scope(conn, user_id, row['scope_id'])
        return row

    async def create(self, user_id, scope_id, client_create_id):
        async with self.pool.acquire() as conn, conn.transaction():
            await self._check_scope(conn, user_id, scope_id)
            existing = await conn.fetchrow('''SELECT * FROM chat_conversations
                WHERE user_id=$1 AND client_create_id=$2''', user_id, client_create_id)
            if existing:
                if existing['scope_id'] != scope_id:
                    raise ConversationConflict('创建请求已用于另一个知识范围。')
                return public_conversation(existing)
            conversation_id = uuid4()
            row = await conn.fetchrow('''INSERT INTO chat_conversations
                (conversation_id,user_id,session_id,client_create_id,scope_id)
                VALUES($1,$2,$3,$4,$5) ON CONFLICT(user_id,client_create_id)
                DO UPDATE SET client_create_id=excluded.client_create_id RETURNING *''',
                conversation_id, user_id, 'c_'+conversation_id.hex, client_create_id, scope_id)
            if row['scope_id'] != scope_id:
                raise ConversationConflict('创建请求已用于另一个知识范围。')
            return public_conversation(row)

    async def get(self, user_id, conversation_id):
        async with self.pool.acquire() as conn:
            return public_conversation(await self._get(conn, user_id, conversation_id))

    async def by_session(self, user_id, session_id):
        value = await self.pool.fetchval('''SELECT conversation_id FROM chat_conversations
            WHERE user_id=$1 AND session_id=$2''', user_id, session_id)
        return await self.get(user_id, value) if value else None

    async def list(self, user_id, scope_id, *, archived=False, search='', cursor=None, limit=30):
        async with self.pool.acquire() as conn:
            await self._check_scope(conn, user_id, scope_id)
            anchor = None
            if cursor:
                try:
                    anchor = json.loads(base64.urlsafe_b64decode(cursor+'='*(-len(cursor)%4)))
                    if anchor['user_id'] != user_id or anchor['scope_id'] != scope_id:
                        raise ValueError('scope')
                    anchor = {'updated_at': datetime.fromisoformat(anchor['updated_at']),
                              'conversation_id': UUID(anchor['conversation_id'])}
                    if anchor['updated_at'].tzinfo is None:
                        raise ValueError('timezone')
                except (ValueError, KeyError, TypeError):
                    raise ConversationConflict('分页位置无效，请刷新会话列表。') from None
            rows = await conn.fetch('''SELECT * FROM chat_conversations
                WHERE user_id=$1 AND scope_id=$2 AND archived=$3 AND strpos(lower(title),lower($4))>0
                AND ($5::timestamptz IS NULL OR (updated_at,conversation_id)<($5,$6::uuid))
                ORDER BY updated_at DESC,conversation_id DESC LIMIT $7''', user_id, scope_id, archived, search,
                anchor['updated_at'] if anchor else None, anchor['conversation_id'] if anchor else None, limit+1)
            next_cursor = None
            if len(rows)>limit:
                last = rows[limit-1]
                next_cursor = base64.urlsafe_b64encode(encode({'user_id': user_id, 'scope_id': scope_id,
                    'updated_at': last['updated_at'].isoformat(), 'conversation_id': str(last['conversation_id'])}).encode()).decode().rstrip('=')
            return {'items': [public_conversation(r) for r in rows[:limit]],
                    'next_cursor': next_cursor}

    async def edit(self, user_id, conversation_id, expected_revision, *, title=None, archived=None):
        async with self.pool.acquire() as conn, conn.transaction():
            row = await self._get(conn, user_id, conversation_id, lock=True)
            await self._expire(conn, row)
            if row['revision'] != expected_revision:
                raise ConversationConflict('会话已更新，请刷新后重试。')
            if await conn.fetchval("SELECT EXISTS(SELECT 1 FROM chat_turns WHERE conversation_id=$1 AND status='running')", row['conversation_id']):
                raise ConversationConflict('会话正在回答，请等待结束后再修改。')
            updated = await conn.fetchrow('''UPDATE chat_conversations SET title=COALESCE($2,title),
                archived=COALESCE($3,archived),revision=revision+1,updated_at=now()
                WHERE conversation_id=$1 RETURNING *''', row['conversation_id'], title, archived)
            return public_conversation(updated)

    async def _expire(self, conn, conversation):
        # No takeover or resume: a lost/expired execution becomes a failed turn.
        rows = await conn.fetch('''UPDATE chat_turns SET status='failed',completed_at=now(),lease_until=NULL
            WHERE conversation_id=$1 AND status='running' AND lease_until<clock_timestamp() RETURNING run_id''', conversation['conversation_id'])
        for row in rows:
            await conn.execute('''UPDATE evaluation_runs SET status='failed',snapshot=snapshot ||
                '{"status":"failed","error":"会话执行中断或超时，请重新发送。"}'::jsonb WHERE run_id=$1''', row['run_id'])

    async def accept_turn(self, user_id, conversation_id, client_turn_id, expected_revision,
                          raw_query, model_query, scope_id, settings, trace, lease_seconds):
        fingerprint = hashlib.sha256(encode([raw_query, scope_id]).encode()).hexdigest()
        async with self.pool.acquire() as conn, conn.transaction():
            conversation = await self._get(conn, user_id, conversation_id, lock=True)
            if conversation['scope_id'] != scope_id:
                raise ConversationConflict('会话绑定的知识范围与请求不一致。')
            await self._expire(conn, conversation)
            previous = await conn.fetchrow('''SELECT * FROM chat_turns
                WHERE conversation_id=$1 AND client_turn_id=$2''', conversation['conversation_id'], client_turn_id)
            if previous:
                if previous['request_fingerprint'] != fingerprint:
                    raise ConversationConflict('此发送标识已用于其他内容。')
                return {'replayed': True, 'turn': dict(previous), 'conversation': public_conversation(conversation)}
            if conversation['archived'] or conversation['read_only']:
                raise ConversationConflict('请先恢复会话，或新建对话。')
            if conversation['revision'] != expected_revision:
                raise ConversationConflict('会话已更新，请刷新后再发送。')
            if await conn.fetchval("SELECT EXISTS(SELECT 1 FROM chat_turns WHERE conversation_id=$1 AND status='running')", conversation['conversation_id']):
                raise ConversationConflict('这个会话正在回答，请等待结束后再发送。')
            # Serialize only the short quota reservation, across processes too.
            await conn.execute('SELECT pg_advisory_xact_lock(205351,901)')
            user_count = await conn.fetchval("SELECT count(*) FROM chat_turns t JOIN chat_conversations c USING(conversation_id) WHERE c.user_id=$1 AND t.status='running' AND t.lease_until>=clock_timestamp()", user_id)
            global_count = await conn.fetchval("SELECT count(*) FROM chat_turns WHERE status='running' AND lease_until>=clock_timestamp()")
            if user_count >= bounded('CHAT_MAX_ACTIVE_PER_USER',4,1,20) or global_count >= bounded('CHAT_MAX_ACTIVE_TOTAL',12,1,100):
                raise ConversationConflict('同时运行的会话已达上限，请等待部分回答结束。')
            scope_snapshot = await conn.fetch('SELECT knowledge_base_id FROM knowledge_bases WHERE user_id=$1 ORDER BY knowledge_base_id', user_id)
            scope_snapshot = [r['knowledge_base_id'] for r in scope_snapshot] if scope_id == DEFAULT_SCOPE else [scope_id]
            turn_id = uuid4()
            trace.config.update({'conversation_id': str(conversation_id), 'turn_id': str(turn_id),
                                 'client_turn_id': str(client_turn_id), 'context_mode': 'contextual',
                                 'scope_snapshot': scope_snapshot})
            await conn.execute('''INSERT INTO evaluation_runs(run_id,user_id,session_id,knowledge_base_id,status,snapshot)
                VALUES($1,$2,$3,$4,'running',$5::jsonb)''', UUID(trace.run_id), user_id,
                conversation['session_id'], scope_id, encode(trace.snapshot()))
            sequence = await conn.fetchval('SELECT COALESCE(max(sequence),0)+1 FROM chat_turns WHERE conversation_id=$1', conversation['conversation_id'])
            turn = await conn.fetchrow('''INSERT INTO chat_turns
                (turn_id,conversation_id,client_turn_id,sequence,request_fingerprint,user_text,model_query,
                 status,run_id,settings,scope_snapshot,lease_until)
                VALUES($1,$2,$3,$4,$5,$6,$7,'running',$8,$9::jsonb,$10::jsonb,clock_timestamp()+$11*interval '1 second') RETURNING *''',
                turn_id, conversation['conversation_id'], client_turn_id, sequence, fingerprint, raw_query, model_query,
                UUID(trace.run_id), encode(settings), encode(scope_snapshot), lease_seconds)
            title = ' '.join(raw_query.split())[:60] or '新对话'
            updated = await conn.fetchrow('''UPDATE chat_conversations SET
                title=CASE WHEN NOT EXISTS(SELECT 1 FROM chat_turns WHERE conversation_id=$1 AND sequence<>$2)
                    AND title='新对话' THEN $3 ELSE title END,revision=revision+1,updated_at=now()
                WHERE conversation_id=$1 RETURNING *''', conversation['conversation_id'], sequence, title)
            return {'replayed': False, 'turn': dict(turn), 'conversation': public_conversation(updated)}

    async def save_run(self, snapshot):
        """Atomic final message + audit; an expired worker cannot publish success."""
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow('''SELECT c.* FROM chat_conversations c JOIN chat_turns t USING(conversation_id)
                WHERE t.run_id=$1 AND c.user_id=$2 FOR UPDATE OF c''', UUID(snapshot['run_id']), snapshot['user_id'])
            if not row:
                raise LookupError('会话执行不存在。')
            turn = await conn.fetchrow('''UPDATE chat_turns SET answer=$2,status=$3,completed_at=now(),lease_until=NULL
                WHERE run_id=$1 AND status='running' AND lease_until>=clock_timestamp() RETURNING turn_id''',
                UUID(snapshot['run_id']), snapshot['answer'], snapshot['status'])
            if not turn:
                raise ConversationConflict('执行已失效；迟到结果未提交。')
            await conn.execute('UPDATE evaluation_runs SET status=$2,snapshot=$3::jsonb WHERE run_id=$1',
                               UUID(snapshot['run_id']), snapshot['status'], encode(snapshot))
            # This derived copy feeds the existing optional memory extractor;
            # chat_turns remains the sole authority for UI/history restoration.
            if snapshot['status'] == 'complete' and snapshot['config'].get('memory_enabled'):
                thread_id = f"{snapshot['user_id']}:{row['session_id']}"
                for role, text in [('human', snapshot['query']), ('ai', snapshot['answer'])]:
                    await conn.execute('''INSERT INTO raw_conversations(id,user_id,thread_id,role,content,created_at)
                        VALUES($1,$2,$3,$4,$5,clock_timestamp()) ON CONFLICT(id) DO NOTHING''',
                        str(uuid5(NAMESPACE_URL, snapshot['run_id']+':'+role)), snapshot['user_id'], thread_id, role, text)
            await conn.execute('''UPDATE chat_conversations SET revision=revision+1,updated_at=now()
                WHERE conversation_id=$1''', row['conversation_id'])

    async def messages(self, user_id, conversation_id, *, before=None, limit=40):
        async with self.pool.acquire() as conn, conn.transaction():
            conversation = await self._get(conn, user_id, conversation_id, lock=True)
            await self._expire(conn, conversation)
            rows = await conn.fetch('''SELECT t.*,r.snapshot FROM chat_turns t JOIN evaluation_runs r USING(run_id)
                WHERE conversation_id=$1 AND ($2::bigint IS NULL OR sequence<$2)
                ORDER BY sequence DESC LIMIT $3''', conversation['conversation_id'], before, limit+1)
            items = []
            for row in reversed(rows[:limit]):
                snapshot = decode(row['snapshot'])
                items.append({'turn_id': str(row['turn_id']), 'sequence': row['sequence'],
                    'query': row['user_text'], 'answer': row['answer'], 'status': row['status'],
                    'run_id': str(row['run_id']), 'started_at': row['created_at'].isoformat(),
                    'error': snapshot.get('error'), 'timings': snapshot.get('timings'),
                    'personalization': snapshot.get('personalization'), 'skill': snapshot.get('config',{}).get('skill')})
            return {'conversation': public_conversation(conversation), 'items': items,
                    'next_cursor': rows[limit-1]['sequence'] if len(rows)>limit else None}

    async def history(self, user_id, conversation_id, *, before_sequence):
        async with self.pool.acquire() as conn:
            conversation = await self._get(conn, user_id, conversation_id)
            rows = await conn.fetch('''SELECT turn_id,model_query,answer FROM chat_turns
                WHERE conversation_id=$1 AND sequence<$2 AND status='complete'
                ORDER BY sequence DESC LIMIT 100''', conversation['conversation_id'], before_sequence)
            anchor = await conn.fetchrow('''SELECT turn_id,model_query FROM chat_turns
                WHERE conversation_id=$1 AND sequence<$2 ORDER BY sequence LIMIT 1''',
                conversation['conversation_id'], before_sequence)
            # Preserve the user's original goal independently of recent-window
            # trimming. This is user text, not an inferred summary or evidence.
            return ([{**dict(anchor), 'is_anchor':True}] if anchor else [])+[dict(r) for r in reversed(rows)]

    async def assert_active(self, user_id, conversation_id, run_id):
        async with self.pool.acquire() as conn:
            await self._get(conn, user_id, conversation_id)
            active = await conn.fetchval('''SELECT EXISTS(SELECT 1 FROM chat_turns
                WHERE conversation_id=$1 AND run_id=$2 AND status='running' AND lease_until>=clock_timestamp())''',
                UUID(str(conversation_id)), UUID(str(run_id)))
            if not active:
                raise ConversationConflict('执行已失效，请刷新会话。')

    async def legacy_sessions(self, user_id):
        # Read-only index: never assigns mixed/unknown historical scopes to a KB.
        rows = await self.pool.fetch('''SELECT r.session_id,max(r.created_at) AS updated_at,
            array_agg(DISTINCT r.knowledge_base_id ORDER BY r.knowledge_base_id) AS scopes,count(*) AS run_count
            FROM evaluation_runs r WHERE r.user_id=$1 AND NOT EXISTS
            (SELECT 1 FROM chat_conversations c WHERE c.user_id=r.user_id AND c.session_id=r.session_id)
            GROUP BY r.session_id ORDER BY max(r.created_at) DESC,r.session_id LIMIT 100''', user_id)
        return [{'session_id': r['session_id'], 'updated_at': r['updated_at'].isoformat(),
                 'scopes': r['scopes'], 'run_count': r['run_count'], 'read_only': True} for r in rows]

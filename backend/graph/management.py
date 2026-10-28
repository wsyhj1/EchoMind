"""Scoped read-only graph status and explicit, consented single-file queue controls."""
import json

from graph.checkpoints import job_limits, management_exists, service_config
from graph.repository import GraphRepository, graph_exists, pending_delete
from graph.settings import GraphSettings


class GraphManagementError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message
        super().__init__(message)


def real_scope(user_id, kb):
    if type(user_id) is not int or user_id < 1 or not isinstance(kb, str) or not kb.strip() or len(kb) > 128 or kb == '默认知识库':
        raise GraphManagementError(422, 'InvalidScope', '请选择当前用户的实际知识库')


def versions(settings):
    return tuple(dict.fromkeys(getattr(settings, 'readable_versions', (settings.version,))))


class GraphManagement:
    def __init__(self, pool, settings=None):
        self.pool = pool
        self.settings = settings or GraphSettings.from_env()
        self.repository = GraphRepository(pool)

    async def _check_kb(self, user, kb):
        real_scope(user, kb)
        if not await self.pool.fetchval('SELECT EXISTS(SELECT 1 FROM knowledge_bases WHERE user_id=$1 AND knowledge_base_id=$2)', user, kb):
            raise GraphManagementError(404, 'KnowledgeBaseNotFound', '知识库不存在或不属于当前用户')

    async def submit(self, user, kb, file_hash, *, consent=False, force=False, consent_target=None):
        real_scope(user, kb)
        if consent is not True:
            raise GraphManagementError(422, 'ExternalModelConsentRequired', '需要明确同意将该文档发送至所示外部模型服务')
        target, target_hash = service_config()
        if not target['host'] or not target['model']:
            raise GraphManagementError(422, 'GraphServiceNotConfigured', '请先配置有效的模型服务目标')
        if consent_target != target_hash:
            raise GraphManagementError(409, 'ConsentTargetChanged', '模型服务目标已改变，请刷新并重新勾选同意')
        await self._check_kb(user, kb)
        await self.repository.initialize()
        parent_limit, call_limit = job_limits()
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", 'graph-submit:' + json.dumps([user, kb, file_hash, self.settings.version]))
            source = await conn.fetchval('''SELECT file_hash FROM file_metadata WHERE user_id=$1
                AND knowledge_base_id=$2 AND file_hash=$3 FOR SHARE''', user, kb, file_hash)
            if not source:
                raise GraphManagementError(404, 'FileNotFound', '文件不存在或不属于当前知识库')
            if await pending_delete(conn, user, kb):
                raise GraphManagementError(409, 'DeletionPending', '知识库删除补偿尚未完成')
            total = await conn.fetchval('SELECT count(*) FROM parent_chunks WHERE user_id=$1 AND knowledge_base_id=$2 AND file_hash=$3', user, kb, file_hash)
            if not 1 <= total <= parent_limit:
                raise GraphManagementError(422, 'GraphParentLimitExceeded', '文档父块为空或超过本作业父块上限')
            row = await conn.fetchrow('''SELECT *,locked_until>now() AS lease_valid FROM kg_index_jobs
                WHERE user_id=$1 AND knowledge_base_id=$2 AND file_hash=$3 AND extraction_version=$4 FOR UPDATE''', user, kb, file_hash, self.settings.version)
            if row and row['status'] in ('PENDING', 'RUNNING') and not row['cancel_requested'] and (row['status'] == 'PENDING' or row['lease_valid']):
                if row['status'] == 'PENDING':
                    if row['model_calls'] >= row['call_limit']:
                        raise GraphManagementError(409, 'GraphCallLimitExceeded', '该作业调用预算已耗尽')
                    await conn.execute('''UPDATE kg_index_jobs SET explicit_requested=true,consented_at=now(),
                        consent_config_sha256=$2,consent_host=$3,consent_model=$4,requested_at=now() WHERE job_id=$1''',
                        row['job_id'], target_hash, target['host'], target['model'])
                return {'job_id': str(row['job_id']), 'status': row['status'], 'queued': True, 'idempotent': True, 'generation': row['generation']}
            if row and row['status'] == 'RUNNING' and row['lease_valid']:
                raise GraphManagementError(409, 'CancellationInProgress', '请等待正在取消的作业停止')
            ready = row and row['status'] == 'READY'
            if not row:
                ready = await conn.fetchval("SELECT EXISTS(SELECT 1 FROM kg_index_jobs WHERE user_id=$1 AND knowledge_base_id=$2 AND file_hash=$3 AND extraction_version=ANY($4::text[]) AND status='READY')", user, kb, file_hash, list(versions(self.settings)))
            if ready and not force:
                raise GraphManagementError(409, 'ReadyRequiresForce', '文件已就绪；确认重建后需要明确勾选 force')
            if row and not ready and row['model_calls'] >= row['call_limit']:
                raise GraphManagementError(409, 'GraphCallLimitExceeded', '该作业调用预算已耗尽，普通重试不会重置预算')
            from uuid import uuid4
            if not row:
                job_id = await conn.fetchval('''INSERT INTO kg_index_jobs(job_id,user_id,knowledge_base_id,file_hash,extraction_version,status,
                    explicit_requested,consented_at,consent_config_sha256,parent_limit,call_limit,parents_total,requested_at,consent_host,consent_model)
                    VALUES($1,$2,$3,$4,$5,'PENDING',true,now(),$6,$7,$8,$9,now(),$10,$11) RETURNING job_id''',
                    uuid4(), user, kb, file_hash, self.settings.version, target_hash, parent_limit, call_limit, total, target['host'], target['model'])
                generation = 1
            else:
                job_id = row['job_id']
                generation = row['generation'] + (1 if ready and force else 0)
                await conn.execute('''UPDATE kg_index_jobs SET status='PENDING',retry_count=0,explicit_requested=true,
                    consented_at=now(),consent_config_sha256=$2,cancel_requested=false,locked_until=NULL,lease_id=NULL,
                    last_error=NULL,finished_at=NULL,requested_at=now(),generation=$3,parents_total=$4,
                    model_calls=CASE WHEN $5 THEN 0 ELSE model_calls END,
                    parents_completed=CASE WHEN $5 THEN 0 ELSE parents_completed END,
                    parent_limit=CASE WHEN $5 THEN $6 ELSE parent_limit END,
                    call_limit=CASE WHEN $5 THEN $7 ELSE call_limit END,consent_host=$8,consent_model=$9 WHERE job_id=$1''',
                    job_id, target_hash, generation, total, bool(ready and force), parent_limit, call_limit, target['host'], target['model'])
                if ready and force:
                    await conn.execute('DELETE FROM kg_parent_checkpoints WHERE job_id=$1', job_id)
        return {'job_id': str(job_id), 'status': 'PENDING', 'queued': True, 'idempotent': False, 'generation': generation}

    async def cancel(self, user, kb, file_hash):
        await self._check_kb(user, kb)
        if not await management_exists(self.pool):
            raise GraphManagementError(409, 'ManagementSchemaUnavailable', '图谱管理迁移尚未初始化')
        async with self.pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch('''SELECT * FROM kg_index_jobs WHERE user_id=$1 AND knowledge_base_id=$2
                AND file_hash=$3 AND extraction_version=ANY($4::text[])
                ORDER BY (extraction_version=$5) DESC,created_at DESC FOR UPDATE''', user, kb, file_hash, list(versions(self.settings)), self.settings.version)
            if not rows:
                raise GraphManagementError(404, 'JobNotFound', '当前版本没有索引作业')
            unfinished = [row for row in rows if row['status'] != 'READY']
            if not unfinished:
                raise GraphManagementError(409, 'JobAlreadyReady', '作业已经完成，取消不会删除现有图谱')
            for row in unfinished:
                status = 'RUNNING' if row['status'] == 'RUNNING' else 'STALE'
                await conn.execute('''UPDATE kg_index_jobs SET cancel_requested=true,status=$2,
                last_error=CASE WHEN $2='STALE' THEN 'GraphJobCancelled' ELSE last_error END,
                finished_at=CASE WHEN $2='STALE' THEN now() ELSE finished_at END WHERE job_id=$1''', row['job_id'], status)
        status = 'RUNNING' if any(row['status'] == 'RUNNING' for row in unfinished) else 'STALE'
        return {'job_id': str(unfinished[0]['job_id']), 'job_ids': [str(row['job_id']) for row in unfinished], 'status': status, 'cancel_requested': True,
                'message': '已阻止新的模型调用；已经发出的外部请求无法撤回'}

    async def status(self, user, kb, *, limit=50, offset=0):
        await self._check_kb(user, kb)
        limit, offset = min(max(int(limit), 1), 100), min(max(int(offset), 0), 100000)
        files = [dict(row) for row in await self.pool.fetch('''SELECT f.file_hash,f.file_name,count(p.parent_id) AS parents_total
            FROM file_metadata f LEFT JOIN parent_chunks p ON (p.file_hash,p.user_id,p.knowledge_base_id)=(f.file_hash,f.user_id,f.knowledge_base_id)
            WHERE f.user_id=$1 AND f.knowledge_base_id=$2 GROUP BY f.file_hash,f.file_name
            ORDER BY f.file_name,f.file_hash LIMIT $3 OFFSET $4''', user, kb, limit, offset)]
        exists = await graph_exists(self.pool)
        managed = exists and await management_exists(self.pool)
        jobs = [dict(row) for row in await self.pool.fetch('SELECT * FROM kg_index_jobs WHERE user_id=$1 AND knowledge_base_id=$2 AND file_hash=ANY($3::text[])', user, kb, [f['file_hash'] for f in files])] if exists else []
        for file in files:
            choices = [j for j in jobs if j['file_hash'] == file['file_hash']]
            ready = next((j for version in versions(self.settings) for j in choices if j['extraction_version'] == version and j['status'] == 'READY'), None)
            chosen = next((j for j in choices if j['extraction_version'] == self.settings.version), None) or ready or (choices[-1] if choices else None)
            file.update(job_id=str(chosen['job_id']) if chosen else None, status=chosen['status'] if chosen else 'UNINDEXED',
                extraction_version=chosen['extraction_version'] if chosen else self.settings.version,
                effective_ready_version=ready['extraction_version'] if ready else None,
                error_type=chosen['last_error'] if chosen else None, cancel_requested=bool(chosen and chosen.get('cancel_requested')),
                explicit_requested=bool(chosen and chosen.get('explicit_requested')), model_calls=chosen.get('model_calls', 0) if chosen else 0,
                lifetime_model_calls=chosen.get('lifetime_model_calls', 0) if chosen else 0,
                generation=chosen.get('generation', 1) if chosen else 1,
                max_parents=chosen.get('parent_limit', job_limits()[0]) if chosen else job_limits()[0],
                max_calls=chosen.get('call_limit', job_limits()[1]) if chosen else job_limits()[1])
            checkpoints_expected = chosen and chosen.get('parents_completed', 0) > 0
            file['progress_verified'] = bool(managed and checkpoints_expected)
            if chosen and chosen['status'] == 'READY' and not checkpoints_expected:
                file['parents_completed'] = file['parents_total']
            elif chosen and managed:
                file['parents_completed'] = await self.pool.fetchval('''SELECT count(*) FROM kg_parent_checkpoints c JOIN parent_chunks p
                    ON (p.parent_id,p.user_id,p.knowledge_base_id,p.file_hash)=(c.parent_id,c.user_id,c.knowledge_base_id,c.file_hash)
                    WHERE c.job_id=$1 AND c.generation=$2 AND c.source_sha256=encode(sha256(convert_to(p.text,'UTF8')),'hex')
                    AND ($3::boolean OR c.config_sha256=$4)''', chosen['job_id'], chosen['generation'], chosen['status']=='READY', service_config()[1])
            else:
                file['parents_completed'] = 0
            file.update(await self._counts(user, kb, file['file_hash'], file['effective_ready_version']) if exists else
                        dict(raw_relations=0, usable_relations=0, rejected_relations=0, raw_entities=0, usable_entities=0, rejected_entities=0, counts_complete=True))
        parent_limit, call_limit = job_limits()
        return {'schema_ready': bool(managed), 'knowledge_base_id': kb, 'extraction_version': self.settings.version,
                'service': service_config()[0], 'limits': {'max_parents': parent_limit, 'max_calls': call_limit},
                'documents': files, 'limit': limit, 'offset': offset, 'has_more': len(files) == limit}

    async def _counts(self, user, kb, file_hash, effective):
        from graph.semantic_guard import quote_supports_relation
        raw = await self.pool.fetchval('SELECT count(DISTINCT relation_id) FROM kg_relation_evidence WHERE user_id=$1 AND knowledge_base_id=$2 AND file_hash=$3', user, kb, file_hash)
        entities = await self.pool.fetchval('SELECT count(DISTINCT entity_id) FROM kg_entity_mentions WHERE user_id=$1 AND knowledge_base_id=$2 AND file_hash=$3', user, kb, file_hash)
        rows = await self.pool.fetch('''SELECT DISTINCT ON (r.relation_id) r.relation_id,r.subject_id,r.object_id,r.predicate,e.quote,
            s.canonical_name AS subject,s.aliases AS subject_aliases,s.entity_type AS subject_type,
            t.canonical_name AS target,t.aliases AS target_aliases,t.entity_type AS target_type,
            strpos(lower(regexp_replace(p.text,'[[:space:]]','','g')),lower(regexp_replace(e.quote,'[[:space:]]','','g')))>0 AS source_valid
            FROM kg_relations r JOIN kg_relation_evidence e ON e.relation_id=r.relation_id
            JOIN kg_entities s ON s.entity_id=r.subject_id JOIN kg_entities t ON t.entity_id=r.object_id
            JOIN parent_chunks p ON (p.parent_id,p.user_id,p.knowledge_base_id,p.file_hash)=(e.parent_id,e.user_id,e.knowledge_base_id,e.file_hash)
            JOIN kg_index_jobs j ON j.job_id=e.job_id WHERE e.user_id=$1 AND e.knowledge_base_id=$2 AND e.file_hash=$3
            AND j.status='READY' AND e.extraction_version=$4 ORDER BY r.relation_id,e.parent_id LIMIT 1001''', user, kb, file_hash, effective) if effective else []
        if len(rows) > 1000:
            return dict(raw_relations=raw, raw_entities=entities, usable_relations=None, rejected_relations=None,
                        usable_entities=None, rejected_entities=None, counts_complete=False)
        accepted, used_entities = set(), set()
        for row in rows:
            if row['source_valid'] and quote_supports_relation(row['subject'], row['predicate'], row['target'], row['quote'],
                subject_aliases=row['subject_aliases'], target_aliases=row['target_aliases'],
                subject_type=row['subject_type'], target_type=row['target_type']):
                accepted.add(row['relation_id'])
                used_entities.update((row['subject_id'], row['object_id']))
        return dict(raw_relations=raw, usable_relations=len(accepted), rejected_relations=raw-len(accepted),
                    raw_entities=entities, usable_entities=len(used_entities), rejected_entities=max(0, entities-len(used_entities)), counts_complete=True)

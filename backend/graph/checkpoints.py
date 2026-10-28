"""Lease-bound persistent parent checkpoints and hard model-request budgets."""
import hashlib
import json
import os
from urllib.parse import urlsplit

from graph.schemas import validated


class GraphJobCancelled(RuntimeError):
    pass


class GraphLeaseLost(RuntimeError):
    pass


class GraphCallLimitExceeded(RuntimeError):
    pass


class GraphParentLimitExceeded(RuntimeError):
    pass


class ConsentTargetChanged(RuntimeError):
    pass


class GraphSourceChanged(RuntimeError):
    pass


class GraphExtractionVersionMismatch(RuntimeError):
    pass


def service_config():
    model = os.getenv('GRAPH_EXTRACTION_MODEL', os.getenv('AGENT_BASE_MODEL', os.getenv('SUMMARIZATION_MODEL', 'qwen-turbo')))
    base_url = os.getenv('BASE_URL')
    if base_url is None:
        base_url = os.getenv('OPENAI_BASE_URL')
    if base_url is None:
        base_url = 'https://api.openai.com/v1'
    try:
        host = urlsplit(base_url).hostname or ''
    except ValueError:
        host = ''
    fingerprint = hashlib.sha256(json.dumps({'model': model, 'base_url': base_url}, sort_keys=True).encode()).hexdigest()
    return {'model': model[:160], 'host': host[:253], 'target_id': fingerprint}, fingerprint


def job_limits():
    parents = int(os.getenv('GRAPH_JOB_MAX_PARENTS', '200'))
    calls = int(os.getenv('GRAPH_JOB_MAX_CALLS', '400'))
    if not 1 <= parents <= 200 or not 1 <= calls <= 400:
        raise ValueError('Graph job limits must be within 1..200 parents and 1..400 model calls')
    return parents, calls


async def management_exists(conn):
    return await conn.fetchval("SELECT to_regclass('kg_parent_checkpoints') IS NOT NULL")


def text_hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


class CheckpointStore:
    def __init__(self, pool):
        self.pool = pool

    async def active(self, job, conn=None, *, lock=False):
        source = conn or self.pool
        row = await source.fetchrow("""SELECT *,locked_until>now() AS lease_valid FROM kg_index_jobs
            WHERE job_id=$1 AND lease_id=$2""" + (' FOR UPDATE' if lock else ''), job['job_id'], job['lease_id'])
        if row is not None and row['cancel_requested']:
            raise GraphJobCancelled()
        if (not row or row['status'] != 'RUNNING' or not row['lease_valid'] or row['generation'] != job['generation']
            or any(row[key] != job[key] for key in ('user_id', 'knowledge_base_id', 'file_hash', 'extraction_version'))):
            raise GraphLeaseLost()
        if row['explicit_requested'] and row['consent_config_sha256'] != service_config()[1]:
            raise ConsentTargetChanged()
        return row

    async def reserve_call(self, job):
        async with self.pool.acquire() as conn, conn.transaction():
            row = await self.active(job, conn, lock=True)
            if row['model_calls'] >= row['call_limit']:
                raise GraphCallLimitExceeded()
            await conn.execute('''UPDATE kg_index_jobs SET model_calls=model_calls+1,
                lifetime_model_calls=lifetime_model_calls+1 WHERE job_id=$1 AND lease_id=$2''', job['job_id'], job['lease_id'])

    async def load(self, job, parent):
        self.check_parent(job, parent)
        async with self.pool.acquire() as conn, conn.transaction():
            await self.active(job, conn, lock=True)
            row = await conn.fetchrow('''SELECT extraction,rejected_relations FROM kg_parent_checkpoints
                WHERE job_id=$1 AND parent_id=$2 AND user_id=$3 AND knowledge_base_id=$4 AND file_hash=$5
                AND extraction_version=$6 AND generation=$7 AND source_sha256=$8 AND config_sha256=$9''',
                job['job_id'], parent['parent_id'], job['user_id'], job['knowledge_base_id'], job['file_hash'],
                job['extraction_version'], job['generation'], text_hash(parent['text']), service_config()[1])
            if not row:
                return None
            # Adoption is allowed only while holding the new active lease lock.
            await conn.execute('''UPDATE kg_parent_checkpoints SET lease_id=$3 WHERE job_id=$1 AND parent_id=$2
                AND generation=$4''', job['job_id'], parent['parent_id'], job['lease_id'], job['generation'])
            raw = json.loads(row['extraction']) if isinstance(row['extraction'], str) else row['extraction']
            extraction, extra_rejected = validated(raw, parent['text'])
            if extra_rejected:
                raise GraphSourceChanged('Checkpoint no longer satisfies extraction validation')
            return extraction, row['rejected_relations']

    async def save(self, job, parent, result):
        self.check_parent(job, parent)
        extraction, rejected = result
        extraction, additional = validated(extraction, parent['text'])
        if type(rejected) is not int or rejected < 0:
            raise ValueError('Invalid rejected relation count')
        async with self.pool.acquire() as conn, conn.transaction():
            await self.active(job, conn, lock=True)
            current = await conn.fetchval('''SELECT text FROM parent_chunks WHERE parent_id=$1 AND user_id=$2
                AND knowledge_base_id=$3 AND file_hash=$4 FOR SHARE''', parent['parent_id'], job['user_id'], job['knowledge_base_id'], job['file_hash'])
            if current != parent['text']:
                raise GraphSourceChanged()
            await conn.execute('''INSERT INTO kg_parent_checkpoints(job_id,parent_id,user_id,knowledge_base_id,file_hash,
                extraction_version,generation,lease_id,source_sha256,config_sha256,extraction,rejected_relations)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb,$12)
                ON CONFLICT(job_id,parent_id,generation) DO UPDATE SET lease_id=excluded.lease_id,
                source_sha256=excluded.source_sha256,config_sha256=excluded.config_sha256,extraction=excluded.extraction,
                rejected_relations=excluded.rejected_relations,updated_at=now()''', job['job_id'], parent['parent_id'],
                job['user_id'], job['knowledge_base_id'], job['file_hash'], job['extraction_version'], job['generation'],
                job['lease_id'], text_hash(current), service_config()[1], extraction.model_dump_json(), rejected + additional)
            await conn.execute('''UPDATE kg_index_jobs j SET parents_completed=(SELECT count(*) FROM kg_parent_checkpoints c
                JOIN parent_chunks p ON (p.parent_id,p.user_id,p.knowledge_base_id,p.file_hash)=(c.parent_id,c.user_id,c.knowledge_base_id,c.file_hash)
                WHERE c.job_id=j.job_id AND c.generation=j.generation
                AND c.source_sha256=encode(sha256(convert_to(p.text,'UTF8')),'hex') AND c.config_sha256=$2)
                WHERE j.job_id=$1''', job['job_id'], service_config()[1])

    async def finish_cancel(self, job):
        await self.pool.execute("""UPDATE kg_index_jobs SET status='STALE',last_error='GraphJobCancelled',finished_at=now(),
            locked_until=NULL WHERE job_id=$1 AND lease_id=$2 AND status='RUNNING'""", job['job_id'], job['lease_id'])

    @staticmethod
    def check_parent(job, parent):
        if any(parent[key] != job[key] for key in ('user_id', 'knowledge_base_id', 'file_hash')):
            raise GraphSourceChanged('Parent source scope differs from job')

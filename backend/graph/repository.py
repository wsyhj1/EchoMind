"""Versioned graph storage. All data reads and writes carry tenant and KB scope."""
import hashlib
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from graph.schemas import normalize, validated


def stable_id(*parts):
    return uuid5(NAMESPACE_URL, json.dumps(parts, ensure_ascii=False, default=str))


async def graph_exists(conn):
    return await conn.fetchval("SELECT to_regclass('kg_index_jobs') IS NOT NULL")


async def prune_graph(conn, user_id, kb):
    if not await graph_exists(conn):
        return
    await conn.execute("""DELETE FROM kg_relations r WHERE user_id=$1 AND knowledge_base_id=$2
        AND NOT EXISTS(SELECT 1 FROM kg_relation_evidence e WHERE e.relation_id=r.relation_id)""", user_id, kb)
    await conn.execute("""DELETE FROM kg_entities n WHERE user_id=$1 AND knowledge_base_id=$2
        AND NOT EXISTS(SELECT 1 FROM kg_entity_mentions m WHERE m.entity_id=n.entity_id)
        AND NOT EXISTS(SELECT 1 FROM kg_relations r WHERE r.subject_id=n.entity_id OR r.object_id=n.entity_id)""", user_id, kb)


async def queue_delete(conn, user_id, kb, file_hash=""):
    if await graph_exists(conn):
        await conn.execute("""INSERT INTO kb_delete_tasks(task_id,user_id,knowledge_base_id,file_hash)
            VALUES($1,$2,$3,$4) ON CONFLICT(user_id,knowledge_base_id,file_hash)
            DO UPDATE SET status='PENDING',attempts=0,last_error=NULL""", uuid4(), user_id, kb, file_hash)


async def pending_delete(conn, user_id, kb):
    return await graph_exists(conn) and await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM kb_delete_tasks WHERE user_id=$1 AND knowledge_base_id=$2 AND status<>'DONE')", user_id, kb)


async def acknowledge_delete(pool, user_id, kb, file_hash=""):
    if await graph_exists(pool):
        await pool.execute("UPDATE kb_delete_tasks SET status='DONE',last_error=NULL WHERE user_id=$1 AND knowledge_base_id=$2 AND file_hash=$3", user_id, kb, file_hash)


class GraphRepository:
    def __init__(self, pool):
        self.pool = pool

    async def initialize(self):
        directory = Path(__file__).resolve().parents[1] / 'migrations'
        paths = [directory / name for name in ('003_knowledge_graph.sql', '005_graph_management.sql', '006_graph_domains.sql')
                 if (directory / name).exists()]
        # Domain migration filename may evolve before its first application.
        paths += [path for path in sorted(directory.glob('006_*.sql')) if path not in paths]
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('echomind-kg:' || current_schema()))")
            await conn.execute("CREATE TABLE IF NOT EXISTS kg_schema_migrations(version TEXT PRIMARY KEY,sha256 TEXT NOT NULL,applied_at TIMESTAMPTZ DEFAULT now())")
            for path in sorted(paths):
                version = path.name.split('_', 1)[0]
                sql = path.read_text(encoding='utf-8')
                digest = hashlib.sha256(sql.encode()).hexdigest()
                previous = await conn.fetchval('SELECT sha256 FROM kg_schema_migrations WHERE version=$1', version)
                if previous and previous != digest:
                    raise RuntimeError('Applied graph migration checksum differs: ' + version)
                if not previous:
                    await conn.execute(sql)
                    await conn.execute('INSERT INTO kg_schema_migrations VALUES($1,$2,now())', version, digest)

    async def enqueue(self, user_id, kb, file_hash, version, *, force=False):
        if kb == "默认知识库":
            raise ValueError("A stored graph requires a real KB")
        sql = """INSERT INTO kg_index_jobs(job_id,user_id,knowledge_base_id,file_hash,extraction_version,status)
            SELECT $1::uuid,$2::int,$3::varchar(128),$4::varchar(64),$5::varchar(32),'PENDING'
            FROM file_metadata WHERE user_id=$2 AND knowledge_base_id=$3 AND file_hash=$4
            ON CONFLICT(user_id,knowledge_base_id,file_hash,extraction_version) DO UPDATE
            SET status=CASE WHEN $6 AND kg_index_jobs.status<>'RUNNING' THEN 'PENDING' ELSE kg_index_jobs.status END,
                retry_count=CASE WHEN $6 AND kg_index_jobs.status<>'RUNNING' THEN 0 ELSE kg_index_jobs.retry_count END
            RETURNING job_id"""
        from graph.checkpoints import management_exists, job_limits
        args = (uuid4(), user_id, kb, file_hash, version, force)
        if not await management_exists(self.pool):
            return await self.pool.fetchval(sql, *args)
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", 'graph-submit:' + json.dumps([user_id, kb, file_hash, version]))
            previous = await conn.fetchrow('''SELECT * FROM kg_index_jobs WHERE user_id=$1 AND knowledge_base_id=$2
                AND file_hash=$3 AND extraction_version=$4 FOR UPDATE''', user_id, kb, file_hash, version)
            job_id = await conn.fetchval(sql, *args)
            if job_id and previous is None:
                parents, calls = job_limits()
                await conn.execute('UPDATE kg_index_jobs SET parent_limit=$2,call_limit=$3 WHERE job_id=$1', job_id, parents, calls)
            elif job_id and force and previous['status'] != 'RUNNING':
                rebuild = previous['status'] == 'READY'
                parents, calls = job_limits()
                await conn.execute('''UPDATE kg_index_jobs SET cancel_requested=false,last_error=NULL,
                    generation=generation+CASE WHEN $2 THEN 1 ELSE 0 END,
                    model_calls=CASE WHEN $2 THEN 0 ELSE model_calls END,
                    parents_completed=CASE WHEN $2 THEN 0 ELSE parents_completed END,
                    parent_limit=CASE WHEN $2 THEN $3 ELSE parent_limit END,
                    call_limit=CASE WHEN $2 THEN $4 ELSE call_limit END WHERE job_id=$1''', job_id, rebuild, parents, calls)
                if rebuild:
                    await conn.execute('DELETE FROM kg_parent_checkpoints WHERE job_id=$1', job_id)
            return job_id

    async def claim(self, user_id=None, kb=None, version=None, file_hash=None, *, explicit_only=False, auto_only=False):
        from graph.checkpoints import management_exists
        managed = await management_exists(self.pool)
        if explicit_only and not managed:
            return None
        extra_filter = ''
        if managed:
            await self.pool.execute("""UPDATE kg_index_jobs SET status='STALE',last_error='GraphJobCancelled',locked_until=NULL,
                finished_at=now() WHERE cancel_requested AND (status='PENDING' OR (status='RUNNING' AND locked_until<now()))
                AND ($1::int IS NULL OR user_id=$1) AND ($2::text IS NULL OR knowledge_base_id=$2)
                AND ($3::text IS NULL OR file_hash=$3) AND ($4::text IS NULL OR extraction_version=$4)""", user_id, kb, file_hash, version)
            extra_filter = ' AND NOT cancel_requested'
            if explicit_only:
                extra_filter += ' AND explicit_requested'
            elif auto_only:
                extra_filter += ' AND NOT explicit_requested'
        lease = uuid4()
        await self.pool.execute("""UPDATE kg_index_jobs SET status='FAILED',last_error='lease_expired',locked_until=NULL
            WHERE status='RUNNING' AND locked_until<now() AND retry_count>=3
            AND ($1::int IS NULL OR user_id=$1) AND ($2::text IS NULL OR knowledge_base_id=$2)
            AND ($3::text IS NULL OR file_hash=$3) AND ($4::text IS NULL OR extraction_version=$4)""", user_id, kb, file_hash, version)
        row = await self.pool.fetchrow("""UPDATE kg_index_jobs SET status='RUNNING',retry_count=retry_count+1,
            lease_id=$1,locked_until=now()+interval '120 seconds',started_at=now(),finished_at=NULL,last_error=NULL
            WHERE job_id=(SELECT job_id FROM kg_index_jobs WHERE retry_count<3
                AND (status='PENDING' OR (status='RUNNING' AND locked_until<now()))
                AND ($2::int IS NULL OR user_id=$2) AND ($3::text IS NULL OR knowledge_base_id=$3)
                AND ($4::text IS NULL OR extraction_version=$4)
                AND ($5::text IS NULL OR file_hash=$5)
                """ + extra_filter + """ ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING *""", lease, user_id, kb, version, file_hash)
        return dict(row) if row else None

    async def heartbeat(self, job):
        return await self.pool.execute("UPDATE kg_index_jobs SET locked_until=now()+interval '120 seconds' WHERE job_id=$1 AND lease_id=$2 AND status='RUNNING'", job["job_id"], job["lease_id"])

    async def fail(self, job, error_type):
        await self.pool.execute("""UPDATE kg_index_jobs SET status='FAILED',last_error=$3,finished_at=now(),locked_until=NULL
            WHERE job_id=$1 AND lease_id=$2 AND status='RUNNING'""", job["job_id"], job["lease_id"], error_type[:100])

    async def parents(self, job):
        rows = await self.pool.fetch("""SELECT parent_id,user_id,knowledge_base_id,file_hash,text FROM parent_chunks
            WHERE user_id=$1 AND knowledge_base_id=$2 AND file_hash=$3 ORDER BY parent_id LIMIT 1001""",
            job["user_id"], job["knowledge_base_id"], job["file_hash"])
        if len(rows) > 1000 or not rows:
            raise ValueError("Empty file or parent limit exceeded")
        return [dict(row) for row in rows]

    async def publish(self, job, parents, extracted):
        user, kb, version = job["user_id"], job["knowledge_base_id"], job["extraction_version"]
        stats = {"parents": len(parents), "relations": 0, "rejected_relations": 0, "entities": 0}
        async with self.pool.acquire() as conn, conn.transaction():
            cancel_filter = ' AND NOT cancel_requested' if 'cancel_requested' in job else ''
            valid = await conn.fetchval("SELECT job_id FROM kg_index_jobs WHERE job_id=$1 AND lease_id=$2 AND status='RUNNING' AND locked_until>now()" + cancel_filter + ' FOR UPDATE', job["job_id"], job["lease_id"])
            if not valid:
                raise RuntimeError("Graph lease lost")
            current = await conn.fetch("""SELECT parent_id,text FROM parent_chunks WHERE user_id=$1 AND knowledge_base_id=$2
                AND file_hash=$3 ORDER BY parent_id FOR SHARE""", user, kb, job["file_hash"])
            if [(p["parent_id"], p["text"]) for p in current] != [(p["parent_id"], p["text"]) for p in parents]:
                raise RuntimeError("Source changed while extracting")
            await conn.execute("DELETE FROM kg_relation_evidence WHERE job_id=$1", job["job_id"])
            await conn.execute("DELETE FROM kg_entity_mentions WHERE job_id=$1", job["job_id"])
            for parent, (extraction, rejected) in zip(parents, extracted, strict=True):
                extraction, additionally_rejected = validated(extraction, parent["text"])
                rejected += additionally_rejected
                stats["rejected_relations"] += rejected
                local = {}
                for entity in extraction.entities:
                    eid = stable_id(user, kb, entity.type, normalize(entity.name), normalize(entity.qualifier))
                    local[entity.local_id] = eid
                    await conn.execute("""INSERT INTO kg_entities(entity_id,user_id,knowledge_base_id,entity_type,canonical_name,normalized_name,qualifier,aliases)
                        VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb) ON CONFLICT(entity_id) DO UPDATE SET
                        aliases=COALESCE((SELECT jsonb_agg(DISTINCT value) FROM jsonb_array_elements(kg_entities.aliases || excluded.aliases)),'[]'::jsonb)""",
                        eid, user, kb, entity.type, entity.name, normalize(entity.name), normalize(entity.qualifier), json.dumps([normalize(a) for a in entity.aliases]))
                    await conn.execute("""INSERT INTO kg_entity_mentions VALUES($1,$2,$3,$4,$5,$6,$7) ON CONFLICT DO NOTHING""",
                        eid, parent["parent_id"], user, kb, job["file_hash"], version, job["job_id"])
                    stats["entities"] += 1
                for relation in extraction.relations:
                    rid = stable_id(user, kb, local[relation.subject_local_id], relation.predicate, local[relation.object_local_id], version)
                    await conn.execute("""INSERT INTO kg_relations VALUES($1,$2,$3,$4,$5,$6,$7) ON CONFLICT DO NOTHING""",
                        rid, user, kb, local[relation.subject_local_id], relation.predicate, local[relation.object_local_id], version)
                    quote_hash = hashlib.sha256(relation.evidence_quote.encode()).hexdigest()
                    await conn.execute("""INSERT INTO kg_relation_evidence VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) ON CONFLICT DO NOTHING""",
                        stable_id(rid, parent["parent_id"], quote_hash), rid, user, kb, parent["parent_id"], job["file_hash"],
                        relation.evidence_quote, quote_hash, version, job["job_id"])
                    stats["relations"] += 1
            await prune_graph(conn, user, kb)
            await conn.execute("""UPDATE kg_index_jobs SET status='READY',stats=$3::jsonb,finished_at=now(),locked_until=NULL
                WHERE job_id=$1 AND lease_id=$2""", job["job_id"], job["lease_id"], json.dumps(stats))
        return stats

    async def status(self, user, kb):
        return [dict(r) for r in await self.pool.fetch("""SELECT file_hash,extraction_version,status,retry_count,last_error,stats
            FROM kg_index_jobs WHERE user_id=$1 AND knowledge_base_id=$2 ORDER BY created_at""", user, kb)]

    async def backfill_files(self, user, kb, version, limit, file_hash=None, force=False):
        return [dict(row) for row in await self.pool.fetch("""SELECT f.file_hash FROM file_metadata f
            LEFT JOIN kg_index_jobs j ON j.user_id=f.user_id AND j.knowledge_base_id=f.knowledge_base_id
                AND j.file_hash=f.file_hash AND j.extraction_version=$3
            WHERE f.user_id=$1 AND f.knowledge_base_id=$2 AND ($4::text IS NULL OR f.file_hash=$4)
            AND (j.status IS NULL OR j.status='PENDING'
                OR (j.status='RUNNING' AND j.locked_until<now())
                OR ($5::boolean AND j.status IN ('READY','FAILED','STALE')))
            ORDER BY f.file_hash LIMIT $6""",
            user, kb, version, file_hash, force, limit)]

    async def drain_delete(self, milvus, *, user_id=None, kb=None, limit=20):
        done = 0
        # Keep the row locked while deleting. Separate consumers cannot claim the
        # same work; a crash rolls back status and the idempotent delete can retry.
        for _ in range(limit):
            async with self.pool.acquire() as conn, conn.transaction():
                row = await conn.fetchrow("""SELECT * FROM kb_delete_tasks WHERE status='PENDING' AND attempts<5
                    AND ($1::int IS NULL OR user_id=$1) AND ($2::text IS NULL OR knowledge_base_id=$2)
                    ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1""", user_id, kb)
                if not row:
                    break
                try:
                    import asyncio
                    if row["file_hash"]:
                        operation = milvus.delete_file_chunks(row["knowledge_base_id"], row["file_hash"], row["user_id"])
                    else:
                        operation = milvus.delete_knowledge_file_chunks(row["knowledge_base_id"], row["user_id"])
                    await asyncio.wait_for(operation, 10)
                except Exception as error:
                    await conn.execute("UPDATE kb_delete_tasks SET attempts=attempts+1,last_error=$2 WHERE task_id=$1", row["task_id"], type(error).__name__)
                    break
                else:
                    await conn.execute("UPDATE kb_delete_tasks SET status='DONE',last_error=NULL WHERE task_id=$1", row["task_id"])
                    done += 1
        return done

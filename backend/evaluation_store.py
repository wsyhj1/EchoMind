"""Durable local runs and leased evaluation jobs; shared with the isolated worker."""
import hashlib
import json
from pathlib import Path
from uuid import UUID, uuid4


def encode(value):
    return json.dumps(value, ensure_ascii=False, default=str, allow_nan=False)


def decode(value):
    return json.loads(value) if isinstance(value, str) else value


class EvaluationStore:
    def __init__(self, pool):
        self.pool = pool

    async def initialize(self):
        sql = (Path(__file__).parent / "migrations" / "001_evaluation.sql").read_text(encoding="utf-8")
        async with self.pool.acquire() as connection:
            await connection.execute(sql)

    async def save_run(self, snapshot):
        await self.pool.execute(
            """INSERT INTO evaluation_runs(run_id,user_id,session_id,knowledge_base_id,status,snapshot)
               VALUES($1,$2,$3,$4,$5,$6::jsonb) ON CONFLICT(run_id)
               DO UPDATE SET status=excluded.status,snapshot=excluded.snapshot""",
            UUID(snapshot["run_id"]), snapshot["user_id"], snapshot["session_id"],
            snapshot["knowledge_base_id"], snapshot["status"], encode(snapshot))

    async def get_run(self, run_id, user_id):
        value = await self.pool.fetchval("SELECT snapshot FROM evaluation_runs WHERE run_id=$1 AND user_id=$2",
                                         UUID(str(run_id)), user_id)
        return decode(value) if value else None

    async def list_runs(self, user_id, session_id, limit=50):
        values = await self.pool.fetch("""SELECT r.snapshot,
            (SELECT j.job_id FROM evaluation_jobs j WHERE j.run_id=r.run_id ORDER BY j.created_at DESC LIMIT 1) AS job_id
            FROM evaluation_runs r WHERE r.user_id=$1 AND r.session_id=$2
            ORDER BY r.created_at DESC LIMIT $3""", user_id, session_id, limit)
        return [{**decode(row["snapshot"]), "evaluation_job_id": str(row["job_id"]) if row["job_id"] else None}
                for row in reversed(values)]

    async def create_job(self, run_id, user_id, payload):
        run = await self.get_run(run_id, user_id)
        if not run:
            raise LookupError("Run not found")
        if run["status"] != "complete":
            raise ValueError("Only completed answers can be evaluated")
        fingerprint = hashlib.sha256(encode([str(run_id), payload]).encode()).hexdigest()
        row = await self.pool.fetchrow("""INSERT INTO evaluation_jobs(job_id,run_id,user_id,fingerprint,payload)
            VALUES($1,$2,$3,$4,$5::jsonb) ON CONFLICT(fingerprint) DO UPDATE SET fingerprint=excluded.fingerprint
            RETURNING job_id""", uuid4(), UUID(str(run_id)), user_id, fingerprint, encode(payload))
        return await self.get_job(row["job_id"], user_id)

    async def get_job(self, job_id, user_id):
        row = await self.pool.fetchrow("SELECT * FROM evaluation_jobs WHERE job_id=$1 AND user_id=$2",
                                       UUID(str(job_id)), user_id)
        if not row:
            return None
        result = dict(row)
        for field in ("job_id", "run_id", "lease_id", "created_at", "updated_at", "available_at", "locked_until"):
            if result[field] is not None:
                result[field] = str(result[field])
        result["payload"] = decode(result["payload"])
        result["result"] = decode(result["result"]) if result["result"] is not None else None
        return result

    async def claim(self):
        await self.pool.execute("""UPDATE evaluation_jobs SET status='failed',error='Worker lease expired after maximum attempts',
            updated_at=now() WHERE status='running' AND locked_until<now() AND attempts>=3""")
        lease = uuid4()
        row = await self.pool.fetchrow("""UPDATE evaluation_jobs SET status='running',attempts=attempts+1,
            locked_until=now()+interval '120 seconds',lease_id=$1,updated_at=now()
            WHERE job_id=(SELECT job_id FROM evaluation_jobs WHERE attempts<3 AND
                ((status='queued' AND available_at<=now()) OR (status='running' AND locked_until<now()))
                ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING *""", lease)
        return dict(row) if row else None

    async def retry_job(self, job_id, user_id):
        job = await self.get_job(job_id, user_id)
        if job is None:
            raise LookupError("Evaluation not found")
        if job["status"] in ("queued", "running"):
            return job
        failed_metrics = any(s.get("status") == "failed" for s in (job.get("result") or {}).get("scores", {}).values())
        if job["attempts"] >= 3 or not (job["status"] == "failed" or failed_metrics):
            raise ValueError("Only failed evaluations with fewer than 3 attempts can be retried")
        await self.pool.execute("""UPDATE evaluation_jobs SET status='queued',result=NULL,error=NULL,
            locked_until=NULL,available_at=now(),updated_at=now()
            WHERE job_id=$1 AND user_id=$2 AND status IN ('completed','failed') AND attempts<3""",
            UUID(str(job_id)), user_id)
        return await self.get_job(job_id, user_id)

    async def heartbeat(self, job):
        return await self.pool.execute("""UPDATE evaluation_jobs SET locked_until=now()+interval '120 seconds'
            WHERE job_id=$1 AND lease_id=$2 AND status='running'""", job["job_id"], job["lease_id"])

    async def complete(self, job, result):
        return await self.pool.execute("""UPDATE evaluation_jobs SET status='completed',result=$3::jsonb,
            error=NULL,locked_until=NULL,updated_at=now() WHERE job_id=$1 AND lease_id=$2 AND status='running'""",
            job["job_id"], job["lease_id"], encode(result))

    async def fail(self, job, error):
        return await self.pool.execute("""UPDATE evaluation_jobs SET
            status=CASE WHEN attempts<3 THEN 'queued' ELSE 'failed' END,error=$3,
            available_at=now()+interval '30 seconds',locked_until=NULL,updated_at=now()
            WHERE job_id=$1 AND lease_id=$2 AND status='running'""", job["job_id"], job["lease_id"], str(error)[:1000])

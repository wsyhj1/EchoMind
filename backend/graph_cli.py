"""Scoped graph backfill/status and persistent deletion compensation."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))


async def backfill_state(pool, args, version):
    """Describe all source files in the requested scope, including absent jobs."""
    rows = await pool.fetch("""SELECT CASE WHEN j.status='RUNNING' AND j.locked_until<now() AND j.retry_count>=3
        THEN 'EXHAUSTED' ELSE COALESCE(j.status,'UNINDEXED') END AS status,count(*) AS count
        FROM file_metadata f LEFT JOIN kg_index_jobs j
        ON j.user_id=f.user_id AND j.knowledge_base_id=f.knowledge_base_id
            AND j.file_hash=f.file_hash AND j.extraction_version=$3
        WHERE f.user_id=$1 AND f.knowledge_base_id=$2 AND ($4::text IS NULL OR f.file_hash=$4)
        GROUP BY 1""", args.user_id, args.knowledge_base_id, version, args.file_hash)
    counts = {row["status"]: row["count"] for row in rows}
    total = sum(counts.values())
    remaining = total - counts.get("READY", 0)
    missing = bool(args.file_hash) and total == 0
    return {"files": total, "status_counts": counts, "remaining": remaining,
            "requires_force": counts.get("FAILED", 0) + counts.get("STALE", 0) + counts.get("EXHAUSTED", 0),
            "requested_file_missing": missing, "complete": remaining == 0 and not missing}


async def execute_backfill(repository, settings, args, files):
    """Only claim files included in this invocation's bounded plan."""
    from graph.indexer import GraphIndexer
    results = []
    indexer = GraphIndexer(repository, settings)
    for file in files:
        if args.force:
            # A crashed third attempt is still RUNNING until lease cleanup.
            # Retire only this expired job before the explicit retry reset.
            await repository.pool.execute("""UPDATE kg_index_jobs SET status='FAILED',last_error='lease_expired',locked_until=NULL
                WHERE user_id=$1 AND knowledge_base_id=$2 AND file_hash=$3 AND extraction_version=$4
                AND status='RUNNING' AND locked_until<now() AND retry_count>=3""",
                args.user_id, args.knowledge_base_id, file["file_hash"], settings.version)
        await repository.enqueue(args.user_id, args.knowledge_base_id, file["file_hash"], settings.version, force=args.force)
        attempted = await indexer.run_pending(args.user_id, args.knowledge_base_id, limit=1, file_hash=file["file_hash"])
        results.extend(attempted)
        if any(result is None for result in attempted):
            break  # Preserve the existing stop-on-model-failure behavior.
    return results


async def run(args):
    import asyncpg
    from dotenv import load_dotenv
    from graph.repository import GraphRepository, graph_exists
    from graph.settings import GraphSettings
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=3, command_timeout=30)
    try:
        repository = GraphRepository(pool)
        if args.action == "migrate":
            if args.execute:
                await repository.initialize()
            print("Migrations applied" if args.execute else "Dry run: graph migrations 003/005/006, --execute to apply")
            return
        if args.action in ('queue', 'cancel'):
            from graph.management import GraphManagement
            from graph.checkpoints import service_config
            if not args.execute:
                print(json.dumps({'dry_run': True, 'action': args.action, 'file_hash': args.file_hash,
                    'service': service_config()[0], 'message': 'queue --execute requires --consent-external-model and --consent-target from status'}, ensure_ascii=False, indent=2))
                return
            manager = GraphManagement(pool)
            result = await manager.cancel(args.user_id, args.knowledge_base_id, args.file_hash) if args.action == 'cancel' else await manager.submit(
                args.user_id, args.knowledge_base_id, args.file_hash, consent=args.consent_external_model, force=args.force, consent_target=args.consent_target)
            print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
            return
        if args.action == "status":
            exists = await graph_exists(pool)
            rows = await repository.status(args.user_id, args.knowledge_base_id) if exists else []
            deletes = [dict(r) for r in await pool.fetch("SELECT file_hash,status,attempts,last_error FROM kb_delete_tasks WHERE user_id=$1 AND knowledge_base_id=$2", args.user_id, args.knowledge_base_id)] if exists else []
            from graph.management import GraphManagement, GraphManagementError
            try:
                management = await GraphManagement(pool).status(args.user_id, args.knowledge_base_id)
            except GraphManagementError as error:
                management = {'error_type': error.code}
            print(json.dumps({"jobs": rows, "deletion_compensation": deletes, 'management': management}, ensure_ascii=False, default=str, indent=2))
            return
        settings = GraphSettings.from_env()
        if args.execute:
            await repository.initialize()
        if await graph_exists(pool):
            files = await repository.backfill_files(args.user_id, args.knowledge_base_id, settings.version,
                                                     args.max_files, args.file_hash, args.force)
        else:
            files = await pool.fetch("""SELECT file_hash FROM file_metadata WHERE user_id=$1 AND knowledge_base_id=$2
                AND ($3::text IS NULL OR file_hash=$3) ORDER BY file_hash LIMIT $4""", args.user_id, args.knowledge_base_id, args.file_hash, args.max_files)
        if not args.execute:
            plan = {"dry_run": True, "selected_files": len(files),
                    "message": "--execute permits indexing/model calls or deletion compensation"}
            if args.action == "reindex" and await graph_exists(pool):
                plan.update(await backfill_state(pool, args, settings.version))
            print(json.dumps(plan, ensure_ascii=False, indent=2))
            return
        if args.action == "retry-deletes":
            from milvus_client import get_milvus_client
            client = await get_milvus_client()
            try:
                # Explicit retry resets the bounded automatic retry budget.
                await pool.execute("UPDATE kb_delete_tasks SET attempts=0 WHERE user_id=$1 AND knowledge_base_id=$2 AND status='PENDING'", args.user_id, args.knowledge_base_id)
                print("Cleaned:", await repository.drain_delete(client, user_id=args.user_id, kb=args.knowledge_base_id))
            finally:
                await client.close()
        else:
            results = await execute_backfill(repository, settings, args, files)
            jobs = await repository.status(args.user_id, args.knowledge_base_id)
            selected_hashes = {file["file_hash"] for file in files}
            not_ready = sum(job["status"] != "READY" for job in jobs if job["file_hash"] in selected_hashes and job["extraction_version"] == settings.version)
            state = await backfill_state(pool, args, settings.version)
            print(json.dumps({"processed": len(results), "failed_attempts": sum(result is None for result in results),
                              "selected_not_ready": not_ready, "not_ready": state["remaining"],
                              **state, "jobs": jobs}, ensure_ascii=False, default=str, indent=2))
            if any(result is None for result in results) or not_ready or state["requires_force"] or state["requested_file_missing"]:
                raise SystemExit(1)
            if state["remaining"]:
                raise SystemExit(2)  # Successful batch; further scoped work remains.
    finally:
        await pool.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("migrate", "reindex", "status", "retry-deletes", 'queue', 'cancel'))
    parser.add_argument("--user-id", type=int)
    parser.add_argument("--knowledge-base-id")
    parser.add_argument("--file-hash")
    parser.add_argument("--max-files", type=int, default=20)
    parser.add_argument("--force", action="store_true", help="Explicitly rebuild READY/FAILED jobs")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument('--consent-external-model', action='store_true')
    parser.add_argument('--consent-target', help='service.target_id shown by status; confirms the displayed model service')
    args = parser.parse_args()
    if args.action != "migrate" and (not args.user_id or args.user_id < 1 or not args.knowledge_base_id or args.knowledge_base_id == "默认知识库"):
        parser.error("A positive user-id and real knowledge-base-id are required")
    if not 1 <= args.max_files <= 100:
        parser.error("max-files must be between 1 and 100")
    if args.action in ('queue', 'cancel') and not args.file_hash:
        parser.error('queue/cancel require a single --file-hash')
    asyncio.run(run(args))


if __name__ == "__main__":
    main()

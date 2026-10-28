"""Index the existing isolated PDF/DOCX corpus with the real graph extractor."""
import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from evaluation.rag_corpus import connect


async def run(args):
    from graph.repository import GraphRepository
    from graph.indexer import GraphIndexer
    from graph.settings import GraphSettings
    state = json.loads(args.state.read_text(encoding="utf-8"))
    if args.execute and args.output.exists():
        raise FileExistsError("Refusing to overwrite graph index results; choose a new output path")
    if state["status"] != "ready":
        raise ValueError("The vector corpus must be READY before graph indexing")
    database, manager = await connect(state)
    try:
        repository = GraphRepository(database.pool)
        if not args.execute:
            print(f"Dry run: {len(state['documents'])} isolated files; --execute calls the graph extraction model")
            return
        await repository.initialize()
        settings = GraphSettings.from_env()
        for file in state["documents"]:
            await repository.enqueue(file["user_id"], file["knowledge_base_id"], file["file_hash"], settings.version, force=args.force)
        results = await GraphIndexer(repository, settings).run_pending(limit=len(state["documents"]))
        jobs = [dict(r) for r in await database.pool.fetch("SELECT user_id,knowledge_base_id,file_hash,extraction_version,status,retry_count,last_error,stats FROM kg_index_jobs WHERE extraction_version=$1 ORDER BY created_at", settings.version)]
        report = {"created_at": datetime.now(timezone.utc).isoformat(), "settings": asdict(settings),
                  "extraction_model": os.getenv("GRAPH_EXTRACTION_MODEL", os.getenv("AGENT_BASE_MODEL", os.getenv("SUMMARIZATION_MODEL", "qwen-turbo"))),
                  "corpus_schema": state["schema"], "corpus_version": state["corpus_version"],
                  "jobs": jobs, "processed": len(results), "failed_attempts": sum(r is None for r in results),
                  "failed": sum(job["status"] == "FAILED" for job in jobs),
                  "not_ready": sum(job["status"] != "READY" for job in jobs)}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, default=str, indent=2), encoding="utf-8")
        print("Graph jobs:", len(jobs), "failed:", report["failed"], "not_ready:", report["not_ready"], flush=True)
        if report["not_ready"]:
            raise SystemExit(1)
    finally:
        await database.pool.close()
        await manager.client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=Path(".runtime/rag_accuracy/corpus.json"))
    parser.add_argument("--output", type=Path, default=Path(".runtime/rag_accuracy/phase2/index.json"))
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()

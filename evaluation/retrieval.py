"""Retriever-only evaluation against an isolated fixed corpus, without Agent routing.

Dry-run is the default. Legacy mode loads verified frozen pre-change modules, so
the original dirty worktree baseline can be reproduced after later code changes.
"""
import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from time import perf_counter
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from evaluation.rag_corpus import ROOT, connect, normalize
from evaluation.rag_metrics import evidence_metrics, summarize

MODES = ("legacy", "vector_only", "vector_plus_graph", "vector_plus_check", "vector_plus_graph_plus_check")


def load_cases(path):
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    ids = [c["question_id"] for c in cases]
    if not cases or len(ids) != len(set(ids)):
        raise ValueError("Dataset must have nonempty unique question_ids")
    for case in cases:
        if not isinstance(case.get("user_id"), int) or case["user_id"] < 1 or not case.get("query") or not case.get("knowledge_base_id"):
            raise ValueError("Each question needs query, positive user_id and knowledge_base_id")
        if bool(case["gold_sources"]) != case["answerable"]:
            raise ValueError("Answerable cases need nonempty Gold sources; no-answer cases need empty sources")
    return cases


def frozen_module(name, directory, manifest):
    path = directory / f"{name}.py"
    expected = manifest["source_sha256"][f"backend/{name}.py"]
    if sha256(path.read_bytes()).hexdigest() != expected:
        raise ValueError(f"Frozen legacy module changed: {name}")
    spec = importlib.util.spec_from_file_location(f"legacy_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def resolve_gold(case, state, database):
    gold = []
    for source in case["gold_sources"]:
        ref = state["source_map"][source["doc_id"]]
        if ref["user_id"] != case["user_id"] or (case["knowledge_base_id"] != "默认知识库" and ref["knowledge_base_id"] != case["knowledge_base_id"]):
            raise ValueError("Gold contains out-of-scope evidence")
        row = await database.pool.fetchrow(
            "SELECT text FROM parent_chunks WHERE parent_id=$1 AND user_id=$2 AND knowledge_base_id=$3",
            ref["parent_id"], ref["user_id"], ref["knowledge_base_id"])
        if not row or normalize(source["quote"]) not in normalize(row["text"]):
            raise ValueError("Gold quote does not resolve to the indexed parent")
        gold.append(ref["parent_id"])
    return list(dict.fromkeys(gold))


async def run(args, cases):
    from observability import RunTrace
    import documents_process
    import knowledeg_base_manager
    from schemas import ContextSchema
    state = json.loads(args.state.read_text(encoding="utf-8"))
    if state["status"] != "ready":
        raise ValueError("Corpus is not READY; finish ingestion first")
    manifest_path = ROOT / "docs/rag_accuracy/baseline_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    legacy = legacy_manager = legacy_database = legacy_documents = None
    if args.mode == "legacy":
        directory = ROOT / ".runtime/rag_accuracy/legacy_source"
        legacy = frozen_module("tools", directory, manifest)
        legacy_manager = frozen_module("knowledeg_base_manager", directory, manifest)
        legacy_database = frozen_module("postgresql_client", directory, manifest)
        legacy_documents = frozen_module("documents_process", directory, manifest)
    database, manager = await connect(state)
    if legacy_manager:
        manager = legacy_manager.KnowledgeBaseManager(manager.client, manager.embeddings, manager.collection_name, manager.dense_dim)
        # Bind the original metadata fetch implementation, not a later replacement.
        import types
        database.get_parent_records = types.MethodType(legacy_database.PostgreSQLParentClient.get_parent_records, database)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        raise FileExistsError("Refusing to overwrite a measured run; choose a new output path")
    dataset_hash = sha256(args.dataset.read_bytes()).hexdigest()
    records = []
    model_keys = ("AGENT_BASE_MODEL", "EMBEDDING_MODEL", "dense_dimension", "RERANK_MODEL")
    config = {"mode": args.mode, "top_k": args.top_k, "rerank_enabled": not args.no_rerank,
              "graph_enabled": args.mode in ("vector_plus_graph", "vector_plus_graph_plus_check"),
              "evidence_enabled": args.mode in ("vector_plus_check", "vector_plus_graph_plus_check"),
              "retrieval_mode": args.retrieval_mode, "corpus_version": state["corpus_version"],
              "corpus_schema": state["schema"], "collection": state["collection"],
              "dataset_sha256": dataset_hash, "git_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
              "corpus_digest": sha256(json.dumps(state["source_map"], sort_keys=True).encode()).hexdigest(),
              "baseline_source_sha256": manifest["source_sha256"],
              "models": {key: os.getenv(key) for key in model_keys},
              "runtime_source_sha256": {str(path.relative_to(ROOT)).replace("\\", "/"): sha256(path.read_bytes()).hexdigest()
                                        for path in sorted((ROOT / "backend").rglob("*.py"))} if not legacy else manifest["source_sha256"],
              "created_at": datetime.now(timezone.utc).isoformat()}
    config["benchmark_source_sha256"] = {str(path.relative_to(ROOT)).replace("\\", "/"): sha256(path.read_bytes()).hexdigest()
                                         for path in sorted((ROOT / "evaluation").glob("*.py"))}
    if config["graph_enabled"]:
        from graph.settings import GraphSettings
        config["graph_settings"] = asdict(GraphSettings.from_env())
        config["graph_source_sha256"] = {path.name: sha256(path.read_bytes()).hexdigest()
                                          for path in sorted((ROOT / "backend/graph").glob("*.py"))}
    from rag.evidence_settings import EvidenceSettings
    config["evidence_settings"] = asdict(EvidenceSettings.from_env())
    args.output.with_suffix(".manifest.json").write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        # Use the actual code path outside a LangGraph stream with status output suppressed.
        with patch.object(knowledeg_base_manager, "get_stream_writer", return_value=lambda _: None), \
                args.output.open("x", encoding="utf-8") as stream:
            for case in cases:
                trace = RunTrace(case["user_id"], "retriever_eval", case["query"], case["knowledge_base_id"], config.copy())
                start = perf_counter()
                row = {"question_id": case["question_id"], "category": case["category"],
                       "mode": args.mode, "gold_review": case["gold_review"], "status": "completed"}
                gold = None
                try:
                    gold = await resolve_gold(case, state, database)
                    with trace.activate():
                        context = ContextSchema(case["user_id"], case["knowledge_base_id"], args.top_k,
                                                rerank_enabled=not args.no_rerank, retrieval_mode=args.retrieval_mode)
                        if legacy:
                            with patch.object(legacy, "get_stream_writer", return_value=lambda _: None), \
                                    patch.object(legacy_manager, "get_stream_writer", return_value=lambda _: None), \
                                    patch.object(legacy, "get_milvus_client", AsyncMock(return_value=manager)), \
                                    patch.object(legacy, "get_postgresql_client", AsyncMock(return_value=database)), \
                                    patch.object(legacy, "rerank_documents", legacy_documents.rerank_documents):
                                await legacy.search_knowledge_base.coroutine(case["query"], SimpleNamespace(context=context))
                        else:
                            from rag.retrieval_service import RetrievalService
                            from rag.types import RetrievalScope, RetrievalOptions
                            result = await RetrievalService(manager, database, documents_process.rerank_documents).retrieve_context(
                                case["query"], RetrievalScope(case["user_id"], case["knowledge_base_id"]), args.top_k,
                                RetrievalOptions(rerank_enabled=not args.no_rerank, retrieval_mode=args.retrieval_mode,
                                                 graph_enabled=config["graph_enabled"], evidence_enabled=config["evidence_enabled"]))
                            row["retrieval_trace"] = asdict(result.trace)
                            row["candidate_records"] = [asdict(c) for c in result.candidates]
                    trace.finish("")
                    final = [c for c in trace.contexts if c["stage"] == "final"]
                    if any(c.get("knowledge_base_id") != case["knowledge_base_id"] for c in final) and case["knowledge_base_id"] != "默认知识库":
                        raise ValueError("Retrieved evidence outside knowledge-base scope")
                    row["final_parent_ids"] = [c["parent_id"] for c in final]
                    row["gold_parent_ids"] = gold
                    row["metrics"] = evidence_metrics(row["final_parent_ids"], gold, args.top_k, answerable=case["answerable"])
                    row["contexts"] = trace.contexts
                    row["call_counts"] = {kind: sum(span["kind"] == kind for span in trace.spans)
                                          for kind in ("embedding", "rerank", "evidence_retrieval", "evidence_rerank")}
                    row["checker_llm_calls"] = 0
                    row["fallback_reasons"] = list(dict.fromkeys(
                        [s["kind"] for s in trace.spans if s["attributes"].get("fallback")]
                        + row.get("retrieval_trace", {}).get("fallback_reasons", [])))
                except Exception as error:
                    # Exception classes are useful and cannot leak document/API credentials.
                    row.update(status="failed", error_type=type(error).__name__)
                    row["metrics"] = evidence_metrics([], gold or [], args.top_k, answerable=case["answerable"]) if gold is not None else {}
                row["steps_ms"] = {kind: value["duration_ms"] for kind, value in trace.timings()["stages"].items()}
                row["steps_ms"]["total"] = (perf_counter() - start) * 1000
                records.append(row)
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                stream.flush()
                print(args.mode, case["question_id"], row["status"], flush=True)
        report = {"config": config, "gold_status": "provisional_until_human_review", **summarize(records)}
        args.output.with_suffix(".summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report["overall"], ensure_ascii=False), flush=True)
    finally:
        await database.pool.close()
        await manager.client.close()
    return sum(r["status"] == "failed" for r in records)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=Path("evaluation/datasets/rag_smoke_v1.jsonl"))
    parser.add_argument("--state", type=Path, default=Path(".runtime/rag_accuracy/corpus.json"))
    parser.add_argument("--mode", choices=MODES, default="vector_only")
    parser.add_argument("--output", type=Path, default=Path(".runtime/rag_accuracy/runs/retrieval.jsonl"))
    parser.add_argument("--top-k", type=int, choices=range(1, 21), default=5)
    parser.add_argument("--retrieval-mode", choices=("hybrid", "dense"), default="hybrid")
    parser.add_argument("--no-rerank", action="store_true")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    cases = load_cases(args.dataset)
    print(f"Validated {len(cases)} cases; mode={args.mode}; Gold requires human review.", flush=True)
    if not args.execute:
        print("Dry run. --execute calls embedding and optional reranking APIs.")
        return
    if asyncio.run(run(args, cases)):
        raise SystemExit(1)


if __name__ == "__main__":
    main()

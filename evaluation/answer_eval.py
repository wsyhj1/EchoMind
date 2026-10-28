"""Actual Agent/tool QA replay against the isolated corpus, separate from retrieval.

Records raw answers, routing, evidence and cost. Correctness and unsupported-claim
rates stay unavailable until human review or the optional existing RAGAS judge.
"""
import argparse
import asyncio
from collections import Counter
from contextlib import ExitStack
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from evaluation.rag_corpus import ROOT, connect
from evaluation.retrieval import MODES, frozen_module, load_cases, resolve_gold
from evaluation.rag_metrics import evidence_metrics, summarize


def source_fingerprints(root=ROOT):
    return {str(path.relative_to(root)).replace("\\", "/"): sha256(path.read_bytes()).hexdigest()
            for path in sorted([*(root / "backend").rglob("*.py"), *(root / "evaluation").glob("*.py")])}


def gold_review_summary(cases):
    """Preserve declared provenance; agent review must not become human approval."""
    counts = dict(sorted(Counter(case.get("gold_review", "unspecified") for case in cases).items()))
    return {"status": next(iter(counts)) if len(counts) == 1 else "mixed",
            "counts": counts, "basis": "dataset_declared_status",
            "answer_review_status": "pending_independent_review"}


async def run(args, cases):
    from observability import RunTrace
    from evaluation.engine import final_knowledge
    source_before = source_fingerprints()
    dataset_hash_before = sha256(args.dataset.read_bytes()).hexdigest()
    state = json.loads(args.state.read_text(encoding="utf-8"))
    if state["status"] != "ready":
        raise ValueError("Corpus is not READY")
    database, manager = await connect(state)
    import agent
    import tools
    judge_config = None
    if args.judge:
        from evaluation_config import evaluator_config
        # Keep the existing RAGAS framework's model settings and metric names.
        judge_config = evaluator_config()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        raise FileExistsError("Refusing to overwrite QA results")
    rows = []
    try:
        with ExitStack() as stack:
            graph_enabled = args.mode in ("vector_plus_graph", "vector_plus_graph_plus_check")
            evidence_enabled = args.mode in ("vector_plus_check", "vector_plus_graph_plus_check")
            if args.mode == "legacy":
                manifest = json.loads((ROOT / "docs/rag_accuracy/baseline_manifest.json").read_text(encoding="utf-8"))
                directory = ROOT / ".runtime/rag_accuracy/legacy_source"
                legacy_tools = frozen_module("tools", directory, manifest)
                legacy_manager = frozen_module("knowledeg_base_manager", directory, manifest)
                legacy_database = frozen_module("postgresql_client", directory, manifest)
                legacy_docs = frozen_module("documents_process", directory, manifest)
                manager = legacy_manager.KnowledgeBaseManager(manager.client, manager.embeddings, manager.collection_name, manager.dense_dim)
                import types
                database.get_parent_records = types.MethodType(legacy_database.PostgreSQLParentClient.get_parent_records, database)
                stack.enter_context(patch.object(agent, "search_knowledge_base", legacy_tools.search_knowledge_base))
                stack.enter_context(patch.object(legacy_tools, "get_milvus_client", AsyncMock(return_value=manager)))
                stack.enter_context(patch.object(legacy_tools, "get_postgresql_client", AsyncMock(return_value=database)))
                stack.enter_context(patch.object(legacy_tools, "rerank_documents", legacy_docs.rerank_documents))
            stack.enter_context(patch.object(tools, "get_milvus_client", AsyncMock(return_value=manager)))
            stack.enter_context(patch.object(tools, "get_postgresql_client", AsyncMock(return_value=database)))
            stream = stack.enter_context(args.output.open("x", encoding="utf-8"))
            for case in cases:
                row = {"question_id": case["question_id"], "category": case["category"], "mode": args.mode,
                       "gold_review": case["gold_review"], "answer_accuracy": None, "unsupported_claim_rate": None,
                       "answer_review_status": "pending", "gold_answer": case["gold_answer"]}
                trace = RunTrace(case["user_id"], "rag_eval_" + uuid4().hex, case["query"], case["knowledge_base_id"])
                try:
                    gold = await resolve_gold(case, state, database)
                    events = [json.loads(line) async for line in agent.stream_agent_response(
                        case["query"], case["knowledge_base_id"], case["user_id"], trace=trace,
                        session_id=trace.session_id, memory_enabled=False, evaluation_mode=True,
                        graph_enabled=graph_enabled, evidence_enabled=evidence_enabled,
                        top_k=args.top_k, rerank_enabled=not args.no_rerank)]
                    snapshot = trace.snapshot()
                    knowledge = final_knowledge(snapshot)
                    ids = [context["parent_id"] for context in knowledge]
                    row.update(status=trace.status, run=snapshot, gold_parent_ids=gold, final_parent_ids=ids,
                               routing_failure=case["answerable"] and not knowledge,
                               metrics=evidence_metrics(ids, gold, args.top_k, answerable=case["answerable"]),
                               steps_ms={"total": snapshot["timings"]["server_total_ms"]})
                    if judge_config:
                        from evaluation.engine import RagasEvaluator
                        evaluator = RagasEvaluator(judge_config, os.getenv("EVAL_API_KEY") or os.getenv("DASHSCOPE_API_KEY"))
                        row["evaluation"] = await evaluator.evaluate(snapshot, {
                            "metrics": ["faithfulness", "factual_correctness", "context_recall"],
                            "reference": case["gold_answer"], "reference_context_ids": gold,
                            "requires_retrieval": case["answerable"]})
                except Exception as error:
                    row.update(status="failed", error_type=type(error).__name__)
                rows.append(row)
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                stream.flush()
                print(case["question_id"], row["status"], flush=True)
        source_after = source_fingerprints()
        declared_gold_review = gold_review_summary(cases)
        args.output.with_suffix(".summary.json").write_text(json.dumps({
            "entrypoint": "actual_agent", "mode": args.mode,
            "gold_status": declared_gold_review["status"], "gold_review_summary": declared_gold_review,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "dataset_sha256": dataset_hash_before,
            "dataset_unchanged_during_run": dataset_hash_before == sha256(args.dataset.read_bytes()).hexdigest(),
            "runtime_source_sha256": {k: v for k, v in source_before.items() if k.startswith("backend/")},
            "benchmark_source_sha256": {k: v for k, v in source_before.items() if k.startswith("evaluation/")},
            "source_unchanged_during_run": source_before == source_after,
            "source_sha256_after": source_after,
            "corpus_version": state["corpus_version"],
            "corpus_schema": state["schema"], "collection": state["collection"],
            "models": {name: os.getenv(name) for name in ("AGENT_BASE_MODEL", "EMBEDDING_MODEL", "RERANK_MODEL")},
            "top_k": args.top_k, "rerank_enabled": not args.no_rerank, "memory_enabled": False,
            "graph_enabled": graph_enabled, "evidence_enabled": evidence_enabled,
            "answer_accuracy": None, "unsupported_claim_rate": None, "abstain_accuracy": None,
            **summarize(rows)}, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        await database.pool.close()
        await manager.client.close()
    return any(row["status"] == "failed" for row in rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=Path("evaluation/datasets/rag_smoke_v1.jsonl"))
    parser.add_argument("--state", type=Path, default=Path(".runtime/rag_accuracy/corpus.json"))
    parser.add_argument("--output", type=Path, default=Path(".runtime/rag_accuracy/runs/qa.jsonl"))
    parser.add_argument("--mode", choices=MODES, default="vector_only")
    parser.add_argument("--top-k", type=int, choices=range(1, 21), default=5)
    parser.add_argument("--max-cases", type=int, default=20)
    parser.add_argument("--no-rerank", action="store_true")
    parser.add_argument("--judge", action="store_true", help="Also call existing RAGAS judge; use .venv-eval")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.max_cases <= 100:
        parser.error("max-cases must be between 1 and 100")
    cases = load_cases(args.dataset)[:args.max_cases]
    print(f"Validated {len(cases)} actual-Agent QA cases, judge={args.judge}", flush=True)
    if not args.execute:
        print("Dry run. --execute calls the actual Agent/tool and models; --judge adds RAGAS calls.")
        return
    if asyncio.run(run(args, cases)):
        raise SystemExit(1)


if __name__ == "__main__":
    main()

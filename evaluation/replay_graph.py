"""Verify final graph code with recorded vector hits/rerank scores and live PG.

This is a deterministic pipeline replay, not another cloud-model measurement.
It is useful after adding source freshness checks, without resending 100 queries.
"""
import argparse
import asyncio
import json
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from evaluation.rag_corpus import connect
from evaluation.retrieval import load_cases
from evaluation.rag_metrics import evidence_metrics


async def replay(args):
    from rag.retrieval_service import RetrievalService
    from rag.types import ChildHit, RetrievalOptions, RetrievalScope
    recorded = [json.loads(line) for line in args.recorded.read_text(encoding="utf-8").splitlines() if line.strip()]
    manifest = json.loads(args.recorded.with_suffix(".manifest.json").read_text(encoding="utf-8"))
    if manifest["top_k"] != 5 or manifest["retrieval_mode"] != "hybrid" or not manifest["rerank_enabled"]:
        raise ValueError("This replay verifies hybrid/Reranker Top5 recordings only")
    if manifest["dataset_sha256"] != sha256(args.dataset.read_bytes()).hexdigest():
        raise ValueError("Replay dataset fingerprint differs")
    if any(row["status"] != "completed" for row in recorded):
        raise ValueError("Failed cases cannot be replaced with successful replay cases")
    cases = {case["question_id"]: case for case in load_cases(args.dataset)}
    if len(recorded) != len(cases) or {row["question_id"] for row in recorded} != set(cases):
        raise ValueError("Replay requires one complete, identical recorded dataset")
    database, manager = await connect(json.loads(args.state.read_text(encoding="utf-8")))
    results = []
    try:
        for row in recorded:
            case = cases[row["question_id"]]
            hits = sorted([ChildHit(**hit) for candidate in row["candidate_records"] for hit in candidate["child_hits"]], key=lambda hit: hit.rank)
            scored = [c for c in row["candidate_records"] if c["rerank_score"] is not None]
            scored.sort(key=lambda candidate: candidate["rerank_score"], reverse=True)
            expected_inputs = [tuple(key) for key in row["retrieval_trace"]["rerank_input_keys"]]
            async def rerank(query, texts, top_k):
                # Indices refer to the exact input provenance recorded in the run.
                by_key = {key: i for i, key in enumerate(expected_inputs)}
                return {"output": {"results": [
                    {"index": by_key[(c["user_id"], c["knowledge_base_id"], c["parent_id"])], "relevance_score": c["rerank_score"]}
                    for c in scored]}}
            result = await RetrievalService(SimpleNamespace(retrieve_knowledge_hits=AsyncMock(return_value=hits)), database, rerank).retrieve_context(
                case["query"], RetrievalScope(case["user_id"], case["knowledge_base_id"]), 5,
                RetrievalOptions(graph_enabled=True))
            if result.trace.rerank_input_keys != expected_inputs:
                raise ValueError(f"Replay rerank input differs: {case['question_id']}")
            ids = [candidate.parent_id for candidate in result.selected]
            metrics = evidence_metrics(ids, row["gold_parent_ids"], 5, answerable=case["answerable"])
            results.append({"question_id": case["question_id"], "same_evidence_set": set(ids) == set(row["final_parent_ids"]),
                            "same_metrics": metrics == row["metrics"], "fallback_reasons": result.trace.fallback_reasons})
        summary = {"verification_type": "recorded_vector_and_rerank_live_postgres_graph_replay", "cases": len(results),
                   "same_evidence_sets": sum(r["same_evidence_set"] for r in results),
                   "same_metrics": sum(r["same_metrics"] for r in results),
                   "all_passed": all(r["same_evidence_set"] and r["same_metrics"] and not r["fallback_reasons"] for r in results),
                   "model_calls": 0, "records": results}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print({key: value for key, value in summary.items() if key != "records"}, flush=True)
        if not summary["all_passed"]:
            raise SystemExit(1)
    finally:
        await database.pool.close()
        await manager.client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recorded", required=True, type=Path)
    parser.add_argument("--dataset", type=Path, default=Path("evaluation/datasets/rag_v1.jsonl"))
    parser.add_argument("--state", type=Path, default=Path(".runtime/rag_accuracy/corpus.json"))
    parser.add_argument("--output", type=Path, default=Path(".runtime/rag_accuracy/phase2/final_replay.json"))
    asyncio.run(replay(parser.parse_args()))


if __name__ == "__main__":
    main()

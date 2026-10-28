"""JSONL benchmark runner. Dry-run by default; --execute permits model calls."""
import argparse
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
import time
from uuid import uuid4

import requests


def percentile(values, fraction):
    values = sorted(values)
    if not values:
        return None
    position = (len(values) - 1) * fraction
    low = int(position)
    return values[low] + (values[min(low + 1, len(values) - 1)] - values[low]) * (position - low)


def summarize(records):
    metrics = {}
    latencies = [r["run"]["timings"]["server_total_ms"] for r in records if r.get("run")]
    for record in records:
        for name, score in ((record.get("evaluation") or {}).get("result") or {}).get("scores", {}).items():
            name = score.get("metric_id", name)
            stats = metrics.setdefault(name, {"values": [], "failed": 0, "not_applicable": 0})
            if score.get("value") is not None and score["status"] == "completed":
                stats["values"].append(score["value"])
            else:
                key = "not_applicable" if score["status"] == "not_applicable" else "failed"
                stats[key] += 1
    return {"cases": len(records), "failed_cases": sum(bool(r.get("error")) for r in records),
            "partial_failure_cases": sum(any(s.get("status") == "failed" for s in
                ((r.get("evaluation") or {}).get("result") or {}).get("scores", {}).values()) for r in records),
            "routing_failures": sum(bool(((r.get("evaluation") or {}).get("result") or {}).get("routing_failure")) for r in records),
            "latency_ms": {"n": len(latencies), "p50": percentile(latencies, .5), "p95": percentile(latencies, .95)},
            "metrics": {name: {"mean": statistics.mean(s["values"]) if s["values"] else None,
                                "n": len(s["values"]), "failed": s["failed"], "not_applicable": s["not_applicable"]}
                        for name, s in metrics.items()}}


def run_case(case, args):
    base = args.base_url.rstrip("/")
    owner = {"user_id": args.user_id}
    run_id = case.get("run_id")
    if not run_id:
        params = {**owner, "query": case["user_input"], "knowledge_base_id": case["knowledge_base_id"],
                  "session_id": "eval_" + uuid4().hex, "evaluation_mode": True, "memory_enabled": False,
                  "top_k": args.top_k, "rerank_enabled": not args.no_rerank, "retrieval_mode": args.retrieval_mode}
        response = requests.get(base + "/chat_with_agent/stream", params=params, timeout=180)
        response.raise_for_status()
        events = [json.loads(line) for line in response.text.splitlines() if line.strip()]
        errors = [e for e in events if e["type"] == "error"]
        if errors:
            raise RuntimeError(str(errors))
        run_id = next(e["run_id"] for e in events if e["type"] == "run")
    response = requests.get(base + f"/runs/{run_id}", params=owner, timeout=15)
    response.raise_for_status()
    run = response.json()
    payload = {"reference": case.get("reference"), "reference_context_ids": case.get("reference_context_ids"),
               "requires_retrieval": case.get("requires_retrieval")}
    if case.get("metrics"):
        payload["metrics"] = case["metrics"]
    response = requests.post(base + f"/runs/{run_id}/evaluations", params=owner, json=payload, timeout=15)
    response.raise_for_status()
    job = response.json()
    deadline = time.monotonic() + args.timeout
    while job["status"] not in ("completed", "failed"):
        if time.monotonic() > deadline:
            raise TimeoutError(f"Evaluation {job['job_id']} still {job['status']}; ensure worker is running")
        time.sleep(2)
        response = requests.get(base + f"/evaluations/{job['job_id']}", params=owner, timeout=10)
        response.raise_for_status()
        job = response.json()
    record = {"case_id": case["case_id"], "dataset": case, "run": run, "evaluation": job}
    if job["status"] == "failed":
        record["error"] = job.get("error", "Evaluation failed")
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--execute", action="store_true", help="Permit chat and judge API calls")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--user-id", type=int, default=1)
    parser.add_argument("--top-k", type=int, choices=range(1, 21), default=5)
    parser.add_argument("--no-rerank", action="store_true")
    parser.add_argument("--retrieval-mode", choices=("hybrid", "dense"), default="hybrid")
    parser.add_argument("--max-cases", type=int, default=50)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--output", type=Path, default=Path(".runtime/evaluation-reports"))
    args = parser.parse_args()
    cases = [json.loads(line) for line in args.dataset.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    if not cases or len(cases) > args.max_cases or len({c["case_id"] for c in cases}) != len(cases):
        parser.error("Dataset must contain unique case_ids and 1..max-cases rows")
    for case in cases:
        if not case.get("run_id") and (not case.get("user_input") or not case.get("knowledge_base_id")):
            parser.error("Each case needs run_id, or user_input + knowledge_base_id")
    print(f"Cases: {len(cases)}; replay settings: top_k={args.top_k}; rerank={not args.no_rerank}; mode={args.retrieval_mode}. Existing run_ids keep their recorded settings.", flush=True)
    if not args.execute:
        print("Dry run only. --execute calls the chat/evaluation models. Start the evaluation worker first.")
        return
    records = []
    for case in cases:
        try:
            record = run_case(case, args)
        except Exception as error:
            from evaluation.engine import clean_error
            record = {"case_id": case["case_id"], "dataset": case, "error": clean_error(error)}
        records.append(record)
        print(case["case_id"], "FAILED" if record.get("error") else "completed", flush=True)
    args.output.mkdir(parents=True, exist_ok=True)
    stem = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:6]
    path = args.output / stem
    path.with_suffix(".json").write_text(json.dumps({"summary": summarize(records), "cases": records},
                                                   ensure_ascii=False, indent=2), encoding="utf-8")
    with path.with_suffix(".csv").open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.writer(stream)
        writer.writerow(["case_id", "run_id", "metric", "value", "status", "reason", "server_total_ms"])
        for record in records:
            run = record.get("run", {})
            scores = ((record.get("evaluation") or {}).get("result") or {}).get("scores", {})
            for name, score in scores.items():
                writer.writerow([record["case_id"], run.get("run_id"), name, score.get("value"), score["status"],
                                 score.get("reason") or score.get("error"), run.get("timings", {}).get("server_total_ms")])
            if record.get("error"):
                writer.writerow([record["case_id"], run.get("run_id"), "", "", "failed", record["error"], ""])
    print("Reports:", path.with_suffix(".json").resolve(), path.with_suffix(".csv").resolve())


if __name__ == "__main__":
    main()

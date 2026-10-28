"""Deterministic evidence-ID metrics; no judge or model dependency."""
import statistics

from evaluation.batch import percentile


METRICS = ("recall_at_k", "hit_at_k", "complete_evidence_at_k", "mrr_at_k")


def evidence_metrics(returned, gold, k=5, *, answerable=True):
    if k < 1:
        raise ValueError("k must be positive")
    # No-answer questions do not have a meaningful retrieval recall denominator.
    if not answerable or not gold:
        return dict.fromkeys(METRICS)
    ordered = list(dict.fromkeys(returned))[:k]
    relevant = set(gold)
    matched = relevant.intersection(ordered)
    first = next((rank for rank, item in enumerate(ordered, 1) if item in relevant), None)
    return {"recall_at_k": len(matched) / len(relevant), "hit_at_k": float(bool(matched)),
            "complete_evidence_at_k": float(matched == relevant),
            "mrr_at_k": 1 / first if first else 0.0}


def summarize(records):
    def group(rows):
        values = {}
        for name in METRICS:
            scored = [r["metrics"][name] for r in rows if r.get("metrics", {}).get(name) is not None]
            values[name] = {"mean": statistics.mean(scored) if scored else None, "n": len(scored)}
        stages = sorted({name for row in rows for name in row.get("steps_ms", {})})
        return {"cases": len(rows), "failed": sum(r.get("status") == "failed" for r in rows),
                "fallback_cases": sum(bool(r.get("fallback_reasons")) for r in rows),
                "metrics": values,
                "latency_ms": {name: {"n": len(samples), "p50": percentile(samples, .5),
                                      "p95": percentile(samples, .95)}
                               for name in stages
                               if (samples := [r["steps_ms"][name] for r in rows if name in r.get("steps_ms", {})])}}
    return {"overall": group(records), "by_category": {
        category: group([r for r in records if r["category"] == category])
        for category in sorted({r["category"] for r in records})}}

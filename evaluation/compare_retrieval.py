"""Paired evidence-ID report; refuses incompatible corpus/model/budget comparisons."""
import argparse
import json
from pathlib import Path

from evaluation.rag_metrics import METRICS, summarize


def load_run(path):
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    ids = [row["question_id"] for row in rows]
    if len(set(ids)) != len(ids):
        raise ValueError("Run contains duplicate question_ids")
    manifest = json.loads(path.with_suffix(".manifest.json").read_text(encoding="utf-8"))
    return {row["question_id"]: row for row in rows}, manifest


def compare(baseline_path, candidate_path):
    baseline, a = load_run(baseline_path)
    candidate, b = load_run(candidate_path)
    for key in ("dataset_sha256", "corpus_schema", "collection", "corpus_version", "corpus_digest", "models", "top_k", "rerank_enabled", "retrieval_mode"):
        if a.get(key) != b.get(key):
            raise ValueError(f"Incompatible runs: {key}")
    if set(baseline) != set(candidate):
        raise ValueError("Runs must have identical question IDs; partial runs cannot be compared")
    if any(baseline[q].get("gold_parent_ids") != candidate[q].get("gold_parent_ids") for q in baseline):
        raise ValueError("Resolved Gold differs between runs")
    first, second = summarize(list(baseline.values())), summarize(list(candidate.values()))
    label_a, label_b = a.get("mode", "baseline"), b.get("mode", "candidate")
    lines = ["# EchoMind 检索配对评测", "", "Gold 状态：待人工审核；以下为合成语料暂定指标，不代表生产准确率。", "",
             f"样本数：{len(baseline)}；K={a['top_k']}；对照 {label_a} → {label_b}；证据检查：{a.get('evidence_enabled', False)} → {b.get('evidence_enabled', False)}。", "",
             f"| 指标 | {label_a} | {label_b} | 差值 |", "|---|---:|---:|---:|"]
    for metric in METRICS:
        x = first["overall"]["metrics"][metric]["mean"]
        y = second["overall"]["metrics"][metric]["mean"]
        lines.append(f"| {metric} | {x:.4f} | {y:.4f} | {y-x:+.4f} |" if x is not None and y is not None else f"| {metric} | unavailable | unavailable | unavailable |")
    lines += ["", f"| 题型 | 样本数 | {label_a} CompleteEvidence | {label_b} CompleteEvidence |", "|---|---:|---:|---:|"]
    for category, stats in first["by_category"].items():
        x = stats["metrics"]["complete_evidence_at_k"]["mean"]
        y = second["by_category"][category]["metrics"]["complete_evidence_at_k"]["mean"]
        lines.append(f"| {category} | {stats['cases']} | {x if x is not None else 'unavailable'} | {y if y is not None else 'unavailable'} |")
    lines += ["", "| 运行 | 失败题数 | 降级题数 | P50 ms | P95 ms |", "|---|---:|---:|---:|---:|"]
    for name, report in ((label_a, first), (label_b, second)):
        stats = report["overall"]
        latency = stats["latency_ms"].get("total", {})
        lines.append(f"| {name} | {stats['failed']} | {stats['fallback_cases']} | {latency.get('p50')} | {latency.get('p95')} |")
    lines += ["", "## 逐题变化", "", f"| 题号 | 类型 | {label_a} 状态 | {label_b} 状态 | 证据集合相同 | 最终顺序相同 | CompleteEvidence 差值 |",
              "|---|---|---|---|---|---|---:|"]
    changed = []
    for qid, x in baseline.items():
        y = candidate[qid]
        xv = x.get("metrics", {}).get("complete_evidence_at_k")
        yv = y.get("metrics", {}).get("complete_evidence_at_k")
        same_order = x.get("final_parent_ids") == y.get("final_parent_ids")
        same_set = set(x.get("final_parent_ids", [])) == set(y.get("final_parent_ids", []))
        delta = yv - xv if xv is not None and yv is not None else None
        if not same_order or delta or x["status"] != y["status"]:
            changed.append(qid)
        lines.append(f"| {qid} | {x['category']} | {x['status']} | {y['status']} | {same_set} | {same_order} | {delta} |")
    lines += ["", "变化题目：" + (", ".join(changed) if changed else "无"), "",
              "延迟包含真实外部服务波动，单次串行运行不构成性能提升结论。无答案题的召回指标为 unavailable；本报告不评估拒答或答案正确率。",
              "失败题仍保留在样本数中；已解析 Gold 的运行失败按未命中计，未能解析 Gold 的题目不伪造零分。", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report = compare(args.baseline, args.candidate)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")
    print(args.output.resolve())


if __name__ == "__main__":
    main()

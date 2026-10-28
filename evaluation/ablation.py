"""Live A/B/C/D retriever ablation on an unchanged, isolated indexed corpus.

Each arm uses one serial query stream; concurrency is explicit. No recorded model
responses are reused. Gold is used only by the scorer after retrieval completes.
"""
import argparse
import asyncio
from copy import copy
import json
from pathlib import Path
from unittest.mock import patch

from evaluation.compare_retrieval import compare, load_run
from evaluation.rag_metrics import METRICS, summarize
from evaluation.retrieval import load_cases, run

ARMS = {"A": "vector_only", "B": "vector_plus_graph", "C": "vector_plus_check", "D": "vector_plus_graph_plus_check"}


def report(folder):
    runs = {arm: load_run(folder / f"{arm}.jsonl") for arm in ARMS}
    for arm in ("B", "C", "D"):
        # Reuse compatibility guards: data, corpus, Gold, models and K must agree.
        (folder / f"A_vs_{arm}.md").write_text(compare(folder / "A.jsonl", folder / f"{arm}.jsonl"), encoding="utf-8")
    for _, manifest in runs.values():
        if manifest["runtime_source_sha256"] != runs["A"][1]["runtime_source_sha256"]:
            raise ValueError("Source changed during ablation")
        if manifest.get("benchmark_source_sha256") != runs["A"][1].get("benchmark_source_sha256"):
            raise ValueError("Benchmark source changed during ablation")
    results = {arm: summarize(list(rows.values())) for arm, (rows, _) in runs.items()}
    diagnostics = {}
    for arm, (rows, _) in runs.items():
        diagnostics[arm] = {
            "supplemental_searches": sum(row.get("retrieval_trace", {}).get("extra_search_count", 0) for row in rows.values()),
            "max_search_rounds": max((row.get("retrieval_trace", {}).get("extra_search_count", 0) for row in rows.values()), default=0),
            "api_calls": {kind: sum(row.get("call_counts", {}).get(kind, 0) for row in rows.values())
                          for kind in ("embedding", "rerank", "evidence_rerank")},
            "checker_llm_calls": 0,
            "changed_from_A": [], "improved_from_A": [], "regressed_from_A": [],
        }
        for qid, row in rows.items():
            before = runs["A"][0][qid]
            if row.get("final_parent_ids") != before.get("final_parent_ids"):
                diagnostics[arm]["changed_from_A"].append(qid)
            x, y = before.get("metrics", {}).get("complete_evidence_at_k"), row.get("metrics", {}).get("complete_evidence_at_k")
            if x is not None and y is not None and x != y:
                diagnostics[arm]["improved_from_A" if y > x else "regressed_from_A"].append(qid)
    execution = json.loads((folder / "execution.json").read_text(encoding="utf-8"))
    output = {"experiment": "live_four_arm_retrieval", "execution": execution, "gold_review": "pending_human_review",
              "formal_gate_passed": False, "answer_accuracy": None, "unsupported_claim_rate": None,
              "abstain_accuracy": None, "results": results, "diagnostics": diagnostics,
              "manifests": {arm: manifest for arm, (_, manifest) in runs.items()}}
    (folder / "phase3_ablation_results.json").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 第三阶段 A/B/C/D 检索消融", "", f"同一数据、Gold、语料、模型及 K；实验组并发数 {execution['concurrency']}，均为实时 API 调用，未复用录制响应。",
             "Gold 待人工审核，检索指标不能代替答案准确率。", "", "| 指标 | A | B | C | D |", "|---|---:|---:|---:|---:|"]
    for metric in METRICS:
        values = [results[a]['overall']['metrics'][metric]['mean'] for a in ARMS]
        lines.append("| " + metric + " | " + " | ".join(f"{value:.4f}" if value is not None else "unavailable" for value in values) + " |")
    for name, getter in (("失败题数", lambda a: results[a]["overall"]["failed"]),
                         ("降级题数", lambda a: results[a]["overall"]["fallback_cases"]),
                         ("补检次数", lambda a: diagnostics[a]["supplemental_searches"]),
                         ("P95 总耗时 ms", lambda a: round(results[a]["overall"]["latency_ms"]["total"]["p95"], 2))):
        lines.append("| " + name + " | " + " | ".join(str(getter(a)) for a in ARMS) + " |")
    lines += ["", "| 分类 | 题数 | A 完整证据 | B | C | D |", "|---|---:|---:|---:|---:|---:|"]
    for category, stats in results["A"]["by_category"].items():
        values = [results[a]["by_category"][category]["metrics"]["complete_evidence_at_k"]["mean"] for a in ARMS]
        lines.append(f"| {category} | {stats['cases']} | " + " | ".join(f"{value:.4f}" if value is not None else "unavailable" for value in values) + " |")
    lines += ["", "## 逐题改善与退化", ""]
    for arm in ("B", "C", "D"):
        lines += [f"- {arm} 相对 A 改善：" + (", ".join(diagnostics[arm]["improved_from_A"]) or "无"),
                  f"- {arm} 相对 A 退化：" + (", ".join(diagnostics[arm]["regressed_from_A"]) or "无")]
    lines += ["", "全部逐题输出与分阶段耗时保留在各组 JSONL；失败样本不删除。",
              "并发运行仍受服务负载影响，P95 为本次实测，不是严格 SLA 或性能改善证明。",
              "检查器不调用 LLM；embedding 与重排使用实际配置服务。未提供完整 Token 计费信息，费用保持 unavailable。",
              "Answer Accuracy、UnsupportedClaimRate、Abstain Accuracy 均 unavailable，需人工答案审阅。", ""]
    (folder / "phase3_ablation_report.md").write_text("\n".join(lines), encoding="utf-8")
    return output


async def execute(args, cases):
    args.output_dir.mkdir(parents=True, exist_ok=False)
    (args.output_dir / "execution.json").write_text(json.dumps({"concurrency": args.concurrency}, indent=2), encoding="utf-8")
    semaphore = asyncio.Semaphore(args.concurrency)
    async def arm_run(arm, mode):
        selected = copy(args)
        selected.mode, selected.output = mode, args.output_dir / f"{arm}.jsonl"
        async with semaphore:
            return await run(selected, cases)
    import knowledeg_base_manager
    # Each arm also suppresses status output. Keep an outer no-op alive until
    # the last arm completes so overlapping patch contexts cannot restore the
    # real LangGraph stream writer while another arm is still running.
    with patch.object(knowledeg_base_manager, "get_stream_writer", return_value=lambda _: None):
        failures = await asyncio.gather(*(arm_run(arm, mode) for arm, mode in ARMS.items()))
    report(args.output_dir)
    return any(failures)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=Path("evaluation/datasets/rag_v1.jsonl"))
    parser.add_argument("--state", type=Path, default=Path(".runtime/rag_accuracy/corpus.json"))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--top-k", type=int, choices=range(1, 21), default=5)
    parser.add_argument("--retrieval-mode", choices=("hybrid", "dense"), default="hybrid")
    parser.add_argument("--no-rerank", action="store_true")
    parser.add_argument("--concurrency", type=int, choices=range(1, 5), default=1)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    cases = load_cases(args.dataset)
    print(f"Validated {len(cases)} cases × 4 arms; human Gold review pending.", flush=True)
    if args.execute:
        raise SystemExit(bool(asyncio.run(execute(args, cases))))
    print("Dry run; --execute makes real embedding/reranker API calls.")


if __name__ == "__main__":
    main()

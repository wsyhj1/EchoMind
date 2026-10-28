"""Audit reviewed Gold using saved final evidence, without retrieval or model calls.

This is an offline annotation-impact audit, never a new live ablation experiment.
The two edited queries require fresh retrieval and receive no reviewed score.
"""
import argparse
from hashlib import sha256
import json
from pathlib import Path

from evaluation.rag_metrics import METRICS, evidence_metrics, summarize


ARMS = {"A": "vector_only", "B": "vector_plus_graph", "C": "vector_plus_check", "D": "vector_plus_graph_plus_check"}
QUERY_CHANGES = frozenset({"Q082", "Q093"})
METHOD = "offline_rescore_no_model_calls"


def digest(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def _read_rows(path):
    rows = [json.loads(line) for line in Path(path).read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    result = {row["question_id"]: row for row in rows}
    if not result or len(result) != len(rows):
        raise ValueError(f"Empty or duplicate question IDs: {path}")
    return result


def _normalized(value):
    return "".join(value.split())


def _summary(rows):
    # No latency is measured or attributed to new retrieval by an offline audit.
    result = summarize(rows)
    for group in [result["overall"], *result["by_category"].values()]:
        group.pop("latency_ms", None)
    return result


def _resolved_gold(case, source_map, source_quotes):
    result = []
    for ref in case["gold_sources"]:
        doc_id = ref["doc_id"]
        source = source_map[doc_id]
        if source["user_id"] != case["user_id"] or (case["knowledge_base_id"] != "默认知识库" and source["knowledge_base_id"] != case["knowledge_base_id"]):
            raise ValueError(f"Out-of-scope Gold: {case['question_id']} / {doc_id}")
        if ref["quote"] != source_quotes[doc_id] or sha256(ref["quote"].encode()).hexdigest() != source["text_sha256"]:
            raise ValueError(f"Gold quote/corpus fingerprint changed: {doc_id}")
        result.append(source["parent_id"])
    if bool(result) != case["answerable"]:
        raise ValueError(f"Answerability and sources disagree: {case['question_id']}")
    return list(dict.fromkeys(result))


def audit(original_dataset, reviewed_dataset, state_path, runs_dir, *, expected_query_changes=QUERY_CHANGES):
    original_dataset, reviewed_dataset, state_path, runs_dir = map(Path, (original_dataset, reviewed_dataset, state_path, runs_dir))
    original, reviewed = _read_rows(original_dataset), _read_rows(reviewed_dataset)
    if set(original) != set(reviewed):
        raise ValueError("Reviewed dataset must retain every original question ID")
    changed = set()
    for qid, case in original.items():
        new = reviewed[qid]
        for field in ("user_id", "knowledge_base_id", "answerable", "category", "corpus_version"):
            if case[field] != new[field]:
                raise ValueError(f"Question binding changed: {qid} / {field}")
        if new.get("gold_review") != "agent_reviewed" or new.get("human_reviewed") is not False:
            raise ValueError(f"Explicit agent review provenance required: {qid}")
        if not new.get("as_of") or not new.get("dataset_version") or not new.get("review_version"):
            raise ValueError(f"Missing reviewed dataset version/as_of: {qid}")
        if case["query"] != new["query"]:
            changed.add(qid)
    if changed != set(expected_query_changes):
        raise ValueError(f"Unexpected changed query set: {sorted(changed)}")
    for field in ("dataset_version", "review_version", "as_of"):
        if len({row[field] for row in reviewed.values()}) != 1:
            raise ValueError(f"Mixed reviewed dataset metadata: {field}")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("status") != "ready":
        raise ValueError("Original corpus state must be ready")
    source_map = state["source_map"]
    source_quotes = {}
    for case in original.values():
        for ref in case["gold_sources"]:
            prior = source_quotes.setdefault(ref["doc_id"], ref["quote"])
            if prior != ref["quote"]:
                raise ValueError("Original source quotes disagree")
    if set(source_map) != set(source_quotes):
        raise ValueError("Every corpus parent needs its original quoted source for offline verification")
    by_parent = {source["parent_id"]: (doc_id, source) for doc_id, source in source_map.items()}
    if len(by_parent) != len(source_map):
        raise ValueError("Ambiguous parent ID across scopes; composite-ID migration required")
    old_gold = {qid: _resolved_gold(case, source_map, source_quotes) for qid, case in original.items()}
    new_gold = {qid: _resolved_gold(case, source_map, source_quotes) for qid, case in reviewed.items()}
    corpus_digest = sha256(json.dumps(source_map, sort_keys=True).encode()).hexdigest()
    provenance = {"original_dataset": {"path": str(original_dataset), "sha256": digest(original_dataset)},
                  "reviewed_dataset": {"path": str(reviewed_dataset), "sha256": digest(reviewed_dataset)},
                  "corpus_state": {"path": str(state_path), "sha256": digest(state_path), "source_map_digest": corpus_digest},
                  "scorer_source": {"path": str(Path(__file__).resolve()), "sha256": digest(__file__)}, "runs": {}}
    arm_results = {}
    anchor = None
    protected = {original_dataset.resolve(), reviewed_dataset.resolve(), state_path.resolve()}
    for arm, mode in ARMS.items():
        run_path = runs_dir / f"{arm}.jsonl"
        manifest_path = run_path.with_suffix(".manifest.json")
        protected.update((run_path.resolve(), manifest_path.resolve()))
        records = _read_rows(run_path)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if set(records) != set(original):
            raise ValueError(f"Incomplete or foreign original run: {arm}")
        for key, expected in {"mode": mode, "dataset_sha256": digest(original_dataset), "corpus_schema": state["schema"],
                              "collection": state["collection"], "corpus_version": state["corpus_version"], "corpus_digest": corpus_digest}.items():
            if manifest.get(key) != expected:
                raise ValueError(f"Original run provenance mismatch: {arm} / {key}")
        compatibility = {key: manifest.get(key) for key in ("models", "top_k", "rerank_enabled", "retrieval_mode", "runtime_source_sha256", "benchmark_source_sha256")}
        if anchor is None:
            anchor = compatibility
        elif compatibility != anchor:
            raise ValueError(f"Original arms differ in source/model/budget: {arm}")
        k = manifest["top_k"]
        if type(k) is not int or not 1 <= k <= 20:
            raise ValueError("Invalid original top K")
        provenance["runs"][arm] = {"path": str(run_path), "sha256": digest(run_path),
            "manifest_path": str(manifest_path), "manifest_sha256": digest(manifest_path),
            "mode": mode, "top_k": k, "original_created_at": manifest.get("created_at")}
        entries, old_subset, new_subset = [], [], []
        for qid, case in original.items():
            raw = records[qid]
            if raw["category"] != case["category"] or raw["mode"] != mode:
                raise ValueError(f"Original record binding mismatch: {arm}/{qid}")
            if raw.get("status") not in ("completed", "failed"):
                raise ValueError(f"Unknown original run status: {arm}/{qid}")
            entry = {"question_id": qid, "category": case["category"], "answerable": case["answerable"],
                     "original_status": raw["status"], "original_metrics": raw.get("metrics", {}),
                     "original_final_parent_ids": raw.get("final_parent_ids", []),
                     "original_query": case["query"], "reviewed_query": reviewed[qid]["query"],
                     "reviewed_metrics": None}
            if raw["status"] == "completed":
                final = [context for context in raw.get("contexts", []) if context.get("stage") == "final"]
                if [context["parent_id"] for context in final] != raw.get("final_parent_ids"):
                    raise ValueError(f"Final evidence trace differs from saved selection: {arm}/{qid}")
                if len(final) > k:
                    raise ValueError(f"Saved selection exceeds original K: {arm}/{qid}")
                for context in final:
                    if context.get("query") != case["query"] or context.get("user_id") != case["user_id"]:
                        raise ValueError(f"Original final context query/user binding mismatch: {arm}/{qid}")
                    if case["knowledge_base_id"] != "默认知识库" and context.get("knowledge_base_id") != case["knowledge_base_id"]:
                        raise ValueError(f"Original final context escaped KB scope: {arm}/{qid}")
                    doc_id, source = by_parent[context["parent_id"]]
                    if any(context.get(field) != source[field] for field in ("user_id", "knowledge_base_id", "file_hash")):
                        raise ValueError(f"Final source identity mismatch: {arm}/{qid}")
                    if _normalized(context["text"]) != _normalized(source_quotes[doc_id]):
                        raise ValueError(f"Saved final source text differs from frozen corpus: {arm}/{qid}")
                old = evidence_metrics(raw["final_parent_ids"], old_gold[qid], k, answerable=case["answerable"])
                if raw.get("gold_parent_ids") != old_gold[qid] or raw.get("metrics") != old:
                    raise ValueError(f"Original Gold/score cannot be reproduced: {arm}/{qid}")
            if qid in changed:
                entry.update(status="requires_rerun", reason="query_changed", reviewed_gold_parent_ids=None)
            else:
                entry["reviewed_gold_parent_ids"] = new_gold[qid]
                metrics = evidence_metrics(raw.get("final_parent_ids", []), new_gold[qid], k, answerable=case["answerable"]) if raw["status"] == "completed" else dict.fromkeys(METRICS)
                entry.update(status="rescored" if raw["status"] == "completed" else "original_run_failed_unscorable",
                             reviewed_metrics=metrics)
                old_subset.append({**raw, "metrics": raw.get("metrics", {})})
                new_subset.append({"question_id": qid, "category": case["category"], "status": raw["status"],
                    "metrics": metrics, "fallback_reasons": raw.get("fallback_reasons", [])})
            entries.append(entry)
        arm_results[arm] = {"mode": mode, "original_full_run_summary": _summary(list(records.values())),
            "same_query_original_gold_summary": _summary(old_subset), "reviewed_gold_same_query_summary": _summary(new_subset),
            "questions": entries,
            "annotation_change_spotchecks": [row for row in entries if row["question_id"] in ("Q079", "Q083")]}
    return {"method": METHOD, "new_model_calls": 0, "new_retrieval_calls": 0,
        "new_live_experiment": False, "human_reviewed": False, "human_gate_passed": False,
        "answer_accuracy": None, "abstain_accuracy": None, "unsupported_claim_rate": None,
        "dataset_version": next(iter(reviewed.values()))["dataset_version"],
        "review_version": next(iter(reviewed.values()))["review_version"], "as_of": next(iter(reviewed.values()))["as_of"],
        "original_case_count": len(original), "same_query_case_count": len(original)-len(changed),
        "same_query_answerable_count": sum(case["answerable"] for qid, case in original.items() if qid not in changed),
        "requires_rerun_ids": sorted(changed), "provenance": provenance, "arms": arm_results,
        "limitations": ["Saved final evidence only; no reranking, retrieval, graph extraction or generation is repeated.",
            "Q082/Q093 have edited queries and no reviewed score until fresh retrieval.",
            "The as_of value fixes the 2026 synthetic document interpretation; it is not fresh system-state verification.",
            "Backup and relationship-chain answer rubrics are not evaluated by evidence-ID metrics.",
            "Agent-reviewed synthetic Gold is not a human-reviewed production or answer-accuracy validation."],
        "_protected_paths": sorted(map(str, protected))}


def render_markdown(result):
    lines = ["# 新版Gold离线证据复算", "", f"方法：`{METHOD}`。模型调用0，检索调用0。",
        "这份报告复算第三阶段保存的最终证据，不是新的实时400题实验。旧运行、旧分数和旧报告未改写。", "",
        f"原100题中只有{result['same_query_case_count']}题查询、用户、知识库和answerable保持相同，其中{result['same_query_answerable_count']}题有答案并进入召回指标分母。",
        "Q082、Q093已修改查询，标记requires_rerun，v2指标为null，必须重新检索。",
        "不能把旧100题的88题分母与本次相同查询子集的87题分母直接比较来归因Gold修订。", "",
        "## 相同查询子集：原Gold与审核Gold", "", "| 组 | 指标 | 原Gold | 审核Gold | 分母 |", "|---|---|---:|---:|---:|"]
    for arm, data in result["arms"].items():
        for metric in METRICS:
            before = data["same_query_original_gold_summary"]["overall"]["metrics"][metric]
            after = data["reviewed_gold_same_query_summary"]["overall"]["metrics"][metric]
            lines.append(f"| {arm} | {metric} | {before['mean']} | {after['mean']} | {after['n']} |")
    lines += ["", "## 审核Gold分题型", "", "| 题型 | 子集题数 | 有效分母 | A完整证据 | B | C | D |", "|---|---:|---:|---:|---:|---:|---:|"]
    categories = result["arms"]["A"]["reviewed_gold_same_query_summary"]["by_category"]
    for category, group in categories.items():
        scores = [result["arms"][arm]["reviewed_gold_same_query_summary"]["by_category"][category]["metrics"]["complete_evidence_at_k"] for arm in ARMS]
        lines.append(f"| {category} | {group['cases']} | {scores[0]['n']} | " + " | ".join(str(score["mean"]) if score["mean"] is not None else "unavailable" for score in scores) + " |")
    lines += ["", "## Q079与Q083的证据标注修订", "",
        "Q079必需源从architecture＋decoy变为architecture；Q083从decoy＋ownership变为decoy。只删去非必需源，不改变保存的最终证据。", "",
        "| 组 | 题号 | 原Recall | 新Recall | 原完整证据 | 新完整证据 |", "|---|---|---:|---:|---:|---:|"]
    for arm, data in result["arms"].items():
        for row in data["annotation_change_spotchecks"]:
            before, after = row["original_metrics"], row["reviewed_metrics"]
            lines.append(f"| {arm} | {row['question_id']} | {before.get('recall_at_k')} | {after.get('recall_at_k')} | {before.get('complete_evidence_at_k')} | {after.get('complete_evidence_at_k')} |")
    lines += ["", "## 绑定检查与限制", "",
        "逐题验证v1与v2的query/user_id/knowledge_base_id/answerable及题型，验证原manifest绑定v1数据和同一source_map、模型、K与源代码快照。原最终证据的顺序、query、复合权限范围、文件哈希与正文均与保存的来源核对；原Gold及原分数重新算出后必须完全一致。任何不符直接拒绝复算。",
        "报告保留原100题分数摘要、98题原Gold摘要、98题审核Gold摘要和四组全部逐题原分数；新指标不会覆盖原指标。原失败题保留，若不能确认实际最终证据则新指标保持不可用。",
        f"as_of={result['as_of']}固定2026合成文档语义；它不是最新系统状态核验。12道操作前置备份和4道关系链的答案rubric变化，需要另审生成答案，不能靠来源ID命中判通过。",
        "本次不评价答案准确率、拒答质量、引用质量、生产效果或新的延迟；无答案题的召回指标保持unavailable。代理审核不伪装真人审核，原真人gate保持未通过。", "",
        "## 输入指纹", "", "| 输入 | SHA256 |", "|---|---|"]
    for name in ("original_dataset", "reviewed_dataset", "corpus_state", "scorer_source"):
        lines.append(f"| {name} | `{result['provenance'][name]['sha256']}` |")
    for arm, run in result["provenance"]["runs"].items():
        lines.append(f"| {arm}原JSONL | `{run['sha256']}` |")
        lines.append(f"| {arm}原manifest | `{run['manifest_sha256']}` |")
    lines += ["", "完整逐题记录、题型指标、输入路径和指纹见同名JSON。", ""]
    return "\n".join(lines)


def write_report(result, output):
    output = Path(output)
    json_path, markdown_path = output.with_suffix(".json"), output.with_suffix(".md")
    protected = set(result.pop("_protected_paths", []))
    if str(json_path.resolve()) in protected or str(markdown_path.resolve()) in protected:
        raise ValueError("Report output must not overwrite original inputs")
    output.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    markdown_path.write_text(render_markdown(result), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original-dataset", type=Path, default=Path("evaluation/datasets/rag_v1.jsonl"))
    parser.add_argument("--reviewed-dataset", type=Path, default=Path("evaluation/datasets/rag_v2_agent_reviewed.jsonl"))
    parser.add_argument("--state", type=Path, default=Path(".runtime/rag_accuracy/corpus.json"))
    parser.add_argument("--runs-dir", type=Path, default=Path(".runtime/rag_accuracy/phase3/final"))
    parser.add_argument("--output", type=Path, default=Path("docs/rag_accuracy/04_reviewed_retrieval_rescore"))
    args = parser.parse_args()
    result = audit(args.original_dataset, args.reviewed_dataset, args.state, args.runs_dir)
    write_report(result, args.output)
    print(json.dumps({"method": METHOD, "same_query_cases": result["same_query_case_count"],
        "answerable_denominator": result["same_query_answerable_count"], "requires_rerun": result["requires_rerun_ids"],
        "new_model_calls": 0, "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()

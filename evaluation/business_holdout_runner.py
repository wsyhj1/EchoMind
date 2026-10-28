"""Offline preflight for the reviewed report holdout; no retrieval/model clients.

Exact source offsets and actual message history are mandatory capabilities.
Gold and review-only evidence are consumed solely by offline validation/scoring;
runtime requests are rebuilt from an explicit input allowlist.
"""
import argparse
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "evaluation/datasets/business_holdout_v2_agent_reviewed.jsonl"
DEFAULT_MANIFEST = DEFAULT_DATASET.with_suffix(".manifest.json")
DEFAULT_SOURCES = ROOT / "evaluation/datasets/business_holdout_v1.sources.json"
CAPABILITIES = {
    "span_history": frozenset({"tenant_and_kb_scope", "source_span_mapping", "actual_message_history"}),
    "span_single_query": frozenset({"tenant_and_kb_scope", "source_span_mapping"}),
    "document_only": frozenset({"tenant_and_kb_scope"}),
}


def _sha(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def _scope_allows(case, source):
    return source["user_id"] == case["user_id"] and (case["knowledge_base_id"] == "默认知识库" or source["knowledge_base_id"] == case["knowledge_base_id"])


def _unique(rows, key, name):
    result = {row[key]: row for row in rows}
    if not result or len(result) != len(rows):
        raise ValueError(f"{name} needs unique nonempty IDs")
    return result


def canonical_messages(case):
    """Preserve real user/assistant turns verbatim; never rewrite from Gold."""
    history = case.get("history", [])
    if not isinstance(history, list) or len(history) > 64:
        raise ValueError("history must be a bounded message list")
    if bool(history) != case.get("requires_history", False):
        raise ValueError(f"History requirement mismatch: {case['question_id']}")
    messages = []
    for index, message in enumerate(history):
        expected_role = "user" if index % 2 == 0 else "assistant"
        if not isinstance(message, dict) or message.get("role") != expected_role or not isinstance(message.get("content"), str) or not message["content"].strip():
            raise ValueError(f"History must alternate actual user/assistant turns: {case['question_id']}")
        messages.append({"role": message["role"], "content": message["content"]})
    if len(messages) % 2:
        raise ValueError("History must finish with assistant before the current user turn")
    if not isinstance(case.get("query"), str) or not case["query"].strip():
        raise ValueError("Current query must be nonempty")
    messages.append({"role": "user", "content": case["query"]})
    return messages


def runtime_request(case):
    """Only this allowlisted object may be handed to a future runtime adapter."""
    return {"question_id": case["question_id"], "user_id": case["user_id"],
            "knowledge_base_id": case["knowledge_base_id"], "as_of": case["as_of"],
            "messages": canonical_messages(case)}


def require_capabilities(cases, mode):
    if mode not in CAPABILITIES:
        raise ValueError(f"Unknown runner mode: {mode}")
    capabilities = CAPABILITIES[mode]
    failures = []
    for case in cases:
        needed = set(case.get("required_runner_capabilities", [])) | {"tenant_and_kb_scope", "source_span_mapping"}
        if case.get("requires_history") or case.get("history"):
            needed.add("actual_message_history")
        missing = needed - capabilities
        if missing:
            failures.append(f"{case['question_id']}:{','.join(sorted(missing))}")
    if failures:
        raise ValueError("Runner capability refusal: " + "; ".join(failures))


def validate_span(ref, case, sources):
    source = sources.get(ref.get("doc_id"))
    if source is None or not _scope_allows(case, source):
        raise ValueError(f"Unknown/out-of-scope source span: {case['question_id']}")
    start, end = ref.get("start_char"), ref.get("end_char")
    text = source["text"]
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text) or text[start:end] != ref.get("quote"):
        raise ValueError(f"Source offset/quote mismatch: {case['question_id']}/{source['doc_id']}")
    identity = sha256(f"{source['doc_id']}:{start}:{end}".encode()).hexdigest()[:24]
    if ref.get("source_span_id") != identity or ref.get("source_text_sha256") != source["text_sha256"]:
        raise ValueError(f"Source span identity/hash mismatch: {case['question_id']}")


def load_bundle(dataset_path=DEFAULT_DATASET, manifest_path=DEFAULT_MANIFEST, sources_path=DEFAULT_SOURCES, *, review_path=None, draft_path=None):
    dataset_path, manifest_path, sources_path = map(Path, (dataset_path, manifest_path, sources_path))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("gold_review") != "agent_reviewed" or manifest.get("human_reviewed") is not False:
        raise ValueError("Only the independently agent-reviewed dataset may run; draft is preserved")
    if manifest.get("scoring_unit") != "exact_source_span" or manifest.get("runtime_tuning_from_gold_allowed") is not False:
        raise ValueError("Exact spans and no runtime Gold tuning must be explicit")
    for path, key in ((dataset_path, "dataset_sha256"), (sources_path, "sources_sha256")):
        if _sha(path) != manifest.get(key):
            raise ValueError(f"Manifest fingerprint mismatch: {key}")
    if manifest.get("dataset_file") != dataset_path.name or manifest.get("sources_file") != sources_path.name:
        raise ValueError("Manifest file bindings differ")
    review_path = Path(review_path) if review_path else ROOT / manifest["review_file"]
    draft_path = Path(draft_path) if draft_path else dataset_path.parent / "business_holdout_v1.jsonl"
    if _sha(review_path) != manifest.get("review_sha256") or _sha(draft_path) != manifest.get("input_draft_sha256"):
        raise ValueError("Independent review or preserved draft fingerprint mismatch")
    review = json.loads(review_path.read_text(encoding="utf-8"))
    if (review.get("review_type") != "independent_subagent_gold_source_review" or review.get("human_reviewed") is not False
            or review.get("output_sha256") != _sha(dataset_path)
            or review.get("input_sha256", {}).get("sources") != _sha(sources_path)
            or review.get("input_sha256", {}).get("draft") != _sha(draft_path)
            or review.get("reviewer") != manifest.get("reviewer")):
        raise ValueError("Independent source review does not bind these exact inputs/outputs")
    cases = [json.loads(line) for line in dataset_path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    source_rows = json.loads(sources_path.read_text(encoding="utf-8"))
    _unique(cases, "question_id", "dataset")
    sources = _unique(source_rows, "doc_id", "sources")
    if len(cases) != manifest.get("questions") or len(sources) != manifest.get("documents"):
        raise ValueError("Manifest counts differ")
    if {row.get("question_id") for row in review.get("reviewed_rows", [])} != {case["question_id"] for case in cases}:
        raise ValueError("Independent review must cover every question")
    for source in sources.values():
        if type(source.get("user_id")) is not int or source["user_id"] < 1 or not isinstance(source.get("knowledge_base_id"), str) or not source["knowledge_base_id"]:
            raise ValueError("Invalid source scope")
        if not isinstance(source.get("text"), str) or not source["text"] or sha256(source["text"].encode()).hexdigest() != source.get("text_sha256"):
            raise ValueError(f"Source body fingerprint mismatch: {source['doc_id']}")
    for case in cases:
        if case.get("gold_review") != "agent_reviewed" or case.get("human_reviewed") is not False or case.get("dataset_version") != manifest["dataset_version"] or case.get("as_of") != manifest["as_of"]:
            raise ValueError("Mixed or unreviewed dataset provenance")
        if type(case.get("user_id")) is not int or case["user_id"] < 1 or not isinstance(case.get("knowledge_base_id"), str) or not case["knowledge_base_id"]:
            raise ValueError("Invalid question scope")
        mode = case.get("answer_mode")
        if mode not in ("answer", "conflict", "clarify", "abstain") or type(case.get("answerable")) is not bool:
            raise ValueError("Invalid answer mode")
        if case["answerable"] != (mode in ("answer", "conflict")) or bool(case.get("gold_sources")) != case["answerable"]:
            raise ValueError("Scoring Gold/answerability mismatch")
        for collection in ("gold_sources", "review_evidence"):
            refs = case.get(collection, [])
            if not isinstance(refs, list) or len({ref["source_span_id"] for ref in refs}) != len(refs):
                raise ValueError("Repeated/malformed source spans")
            for ref in refs:
                validate_span(ref, case, sources)
        canonical_messages(case)
    return {"cases": cases, "sources": sources, "manifest": manifest,
            "fingerprints": {"dataset": _sha(dataset_path), "manifest": _sha(manifest_path), "sources": _sha(sources_path),
                             "independent_review": _sha(review_path), "preserved_draft": _sha(draft_path)}}


@dataclass(frozen=True)
class Parent:
    parent_id: str
    doc_id: str
    user_id: int
    knowledge_base_id: str
    source_text_sha256: str
    start_char: int
    end_char: int
    text: str


def split_sources(sources, max_chars=1000, overlap=200):
    """Generic fixed character windows, using only raw source text and identity."""
    if type(max_chars) is not int or type(overlap) is not int or max_chars < 1 or not 0 <= overlap < max_chars:
        raise ValueError("Require max_chars>0 and 0<=overlap<max_chars")
    parents = {}
    for doc_id, source in sorted(sources.items()):
        text = source["text"]
        start = 0
        while start < len(text):
            end = min(start + max_chars, len(text))
            identity = [doc_id, source["user_id"], source["knowledge_base_id"], source["text_sha256"], start, end]
            parent_id = sha256(json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()[:32]
            parents[parent_id] = Parent(parent_id, doc_id, source["user_id"], source["knowledge_base_id"], source["text_sha256"], start, end, text[start:end])
            if end == len(text):
                break
            start = end - overlap
    return parents


def _covered_chars(start, end, intervals):
    clipped = sorted((max(start, left), min(end, right)) for left, right in intervals if left < end and right > start)
    count, current_end = 0, start
    for left, right in clipped:
        left = max(left, current_end)
        if right > left:
            count += right-left
            current_end = right
    return count


def score_selected_spans(case, selected_parent_ids, parents):
    """Offline-only scorer: union offsets within exact authorized source identities."""
    selected = []
    for parent_id in dict.fromkeys(selected_parent_ids):
        if parent_id not in parents:
            raise ValueError("Selected parent is not in the frozen offset catalog")
        parent = parents[parent_id]
        if not _scope_allows(case, asdict(parent)):
            raise ValueError("Selected parent escaped the question's user/KB scope")
        selected.append(parent)
    refs = case["gold_sources"]
    if not refs:
        return {"source_span_recall": None, "source_character_recall": None, "complete_evidence": None, "gold_span_count": 0}
    fully_covered, covered_total, length_total = 0, 0, 0
    details = []
    for ref in refs:
        intervals = [(parent.start_char, parent.end_char) for parent in selected
                     if parent.doc_id == ref["doc_id"] and parent.source_text_sha256 == ref["source_text_sha256"]]
        covered = _covered_chars(ref["start_char"], ref["end_char"], intervals)
        length = ref["end_char"]-ref["start_char"]
        complete = covered == length
        fully_covered += complete
        covered_total += covered
        length_total += length
        details.append({"source_span_id": ref["source_span_id"], "covered_chars": covered, "total_chars": length, "complete": complete})
    return {"source_span_recall": fully_covered/len(refs), "source_character_recall": covered_total/length_total,
            "complete_evidence": float(fully_covered == len(refs)), "gold_span_count": len(refs), "spans": details}


def dry_run_plan(bundle, *, mode="span_history", max_chars=1000, overlap=200):
    cases, sources = bundle["cases"], bundle["sources"]
    require_capabilities(cases, mode)
    parents = split_sources(sources, max_chars, overlap)
    case_plans = []
    for case in cases:
        authorized = [parent.parent_id for parent in parents.values() if _scope_allows(case, asdict(parent))]
        # This is reachability over the full authorized catalog, not a retrieval score.
        mapped = score_selected_spans(case, authorized, parents)
        if mapped["complete_evidence"] == 0:
            raise ValueError(f"Frozen source spans cannot be mapped: {case['question_id']}")
        case_plans.append({"question_id": case["question_id"], "runtime_request": runtime_request(case),
            "offline_mapping_validation": {"gold_span_count": len(case["gold_sources"]),
                "review_only_span_count": len(case.get("review_evidence", [])),
                "all_gold_offsets_reachable": mapped["complete_evidence"] in (None, 1.0)},
            "authorized_parent_count": len(authorized)})
    return {"method": "business_holdout_dry_run_preflight", "status": "validated_no_live_execution",
        "mode": mode, "capabilities": sorted(CAPABILITIES[mode]), "dataset_version": bundle["manifest"]["dataset_version"],
        "gold_review": "agent_reviewed", "human_reviewed": False,
        "new_model_calls": 0, "new_retrieval_calls": 0, "answer_accuracy": None,
        "observed_retrieval_span_recall": None, "observed_complete_evidence": None,
        "question_count": len(cases), "source_document_count": len(sources), "parent_count": len(parents),
        "history_case_count": sum(bool(case["history"]) for case in cases),
        "gold_span_count": sum(len(case["gold_sources"]) for case in cases),
        "offset_unit": "Python Unicode code points; half-open [start_char,end_char)",
        "segmentation": {"algorithm": "deterministic_fixed_character_windows_v1", "max_chars": max_chars, "overlap": overlap},
        "fingerprints": {**bundle["fingerprints"], "runner_source": _sha(__file__)},
        "runtime_fields_allowlist": ["question_id", "user_id", "knowledge_base_id", "as_of", "messages"],
        "case_plans": case_plans,
        "limitations": ["No index ingestion, vector retrieval, graph extraction, reranking or answer generation occurred.",
            "Mapping all Gold offsets in all authorized source text is structural reachability, never measured retrieval accuracy.",
            "Future live adapters must actually deliver these messages and validate the model-visible selected-parent text/offset catalog before scoring.",
            "Gold/review_evidence/expected_claims are offline-only and absent from every runtime request."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--sources", type=Path, default=DEFAULT_SOURCES)
    parser.add_argument("--mode", choices=tuple(CAPABILITIES), default="span_history")
    parser.add_argument("--max-chars", type=int, default=1000)
    parser.add_argument("--overlap", type=int, default=200)
    parser.add_argument("--output", type=Path, default=ROOT / "docs/rag_accuracy/05_business_holdout_dry_run.json")
    parser.add_argument("--execute", action="store_true", help="Reserved; live execution is deliberately unsupported")
    args = parser.parse_args()
    if args.execute:
        parser.error("Live execution is not implemented; no model/retrieval calls were made")
    bundle = load_bundle(args.dataset, args.manifest, args.sources)
    result = dry_run_plan(bundle, mode=args.mode, max_chars=args.max_chars, overlap=args.overlap)
    protected = {args.dataset.resolve(), args.manifest.resolve(), args.sources.resolve(),
                 (ROOT / bundle["manifest"]["review_file"]).resolve(),
                 (args.dataset.parent / "business_holdout_v1.jsonl").resolve()}
    if args.output.resolve() in protected:
        parser.error("Output may not overwrite reviewed inputs or the preserved draft")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "questions": result["question_count"], "parents": result["parent_count"],
        "history_cases": result["history_case_count"], "new_model_calls": 0, "new_retrieval_calls": 0,
        "answer_accuracy": None, "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()

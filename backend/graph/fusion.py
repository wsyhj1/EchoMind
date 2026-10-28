from rag.types import RetrievalCandidate


def fuse(candidates, recall, user_id):
    by_key = {candidate.key: candidate for candidate in candidates}
    active = {(path.knowledge_base_id, pid) for path in recall.paths for pid in path.parent_ids}
    for row in recall.records:
        if row["user_id"] != user_id or (row["knowledge_base_id"], row["parent_id"]) not in active:
            continue
        key = user_id, row["knowledge_base_id"], row["parent_id"]
        if key not in by_key:
            by_key[key] = RetrievalCandidate(**{k: row[k] for k in (
                "parent_id", "user_id", "knowledge_base_id", "file_hash", "file_name", "text")})
        candidate = by_key[key]
        if "graph" not in candidate.sources:
            candidate.sources.append("graph")
        candidate.evidence_relation_ids = list(dict.fromkeys(candidate.evidence_relation_ids + [
            rid for path in recall.paths if path.knowledge_base_id == row["knowledge_base_id"] and row["parent_id"] in path.parent_ids
            for rid in path.relation_ids]))
    return list(by_key.values())


def retain_paths(ranked, all_candidates, paths, top_k, quota):
    """Reserve a complete valid path, never just a dangling half-path."""
    if not quota:
        return ranked[:top_k]
    by_source = {(c.knowledge_base_id, c.parent_id): c for c in all_candidates}
    protected = []
    for path in paths:
        keys = [(path.knowledge_base_id, parent) for parent in path.parent_ids]
        if not all(key in by_source for key in keys):
            continue
        group = list(dict.fromkeys([(c.knowledge_base_id, c.parent_id) for c in protected] + keys))
        if len(group) <= min(top_k, quota):
            protected = [by_source[key] for key in group]
    protected_keys = {c.key for c in protected}
    # Keep protected evidence in reranker order when present; append missing path
    # members before filling the remaining slots by the unified ranking.
    selected = [c for c in ranked if c.key in protected_keys]
    seen = {c.key for c in selected}
    selected += [c for c in protected if c.key not in seen]
    seen = {c.key for c in selected}
    return (selected + [c for c in ranked if c.key not in seen])[:top_k]

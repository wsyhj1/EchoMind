"""Local structural citation checks. No model, guessed source, or semantic verdict."""
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
from uuid import UUID, uuid4

VERSION = "citation-structure-v1"


def _prose(answer):
    """Keep offsets while ignoring fenced/inline code containing example labels."""
    return re.sub(r"```[\s\S]*?```|`[^`\n]*`", lambda m: " " * len(m[0]), answer)


def parse_citations(answer):
    text = _prose(answer)
    found = []
    covered = []
    for match in re.finditer(r"[（(]\s*(?:来源|source)\s*[:：]([^\n）)]+)[）)]", text, re.I):
        body = match[1]
        count_before = len(found)
        for pair in re.finditer(r"(?:^|[;；])\s*(?:来源\s*[:：])?\s*([^,，;；]+?)\s*[,，]\s*(E[1-9]\d*)\b", body):
            found.append({"file": pair[1].strip(), "evidence_id": pair[2],
                          "start": match.start(), "end": match.end()})
        if len(found) == count_before:
            bare = re.fullmatch(r"\s*(E[1-9]\d*)\s*", body)
            found.append({"file": None if bare else body.strip(), "evidence_id": bare[1] if bare else None,
                          "start": match.start(), "end": match.end()})
        covered.append((match.start(), match.end()))
    for match in re.finditer(r"《([^》\n]+)》\s*[（(]\s*(E[1-9]\d*)\s*[）)]", text):
        found.append({"file": match[1], "evidence_id": match[2], "start": match.start(), "end": match.end()})
        covered.append((match.start(), match.end()))
    # Bare E references remain unverified; never infer a missing filename.
    for match in re.finditer(r"(?<![A-Za-z0-9_])E[1-9]\d*(?![A-Za-z0-9_])", text):
        if not any(a <= match.start() < b for a, b in covered):
            found.append({"file": None, "evidence_id": match[0], "start": match.start(), "end": match.end()})
    found.sort(key=lambda item: item["start"])
    for number, item in enumerate(found, 1):
        item["citation_id"] = f"c{number}"
    return found


def visible_evidence(run):
    """Require last actual model prompt plus a matching selected trace source."""
    inputs = run.get("model_inputs") or []
    if not inputs:
        return []
    records = []
    for message in inputs[-1].get("messages", []):
        if message.get("role") != "tool" or not isinstance(message.get("content"), str):
            continue
        # JSON permits U+2028/U+2029/U+0085 inside string values. They are
        # document characters, not separators between tool JSON payloads.
        for line in message["content"].split("\n"):
            try:
                data = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(data, dict) and isinstance(data.get("evidence"), list):
                for evidence in data["evidence"]:
                    if not isinstance(evidence, dict) or not all(isinstance(evidence.get(k), str) for k in ("id", "file", "knowledge_base", "text")):
                        continue
                    contexts = [c for c in run.get("contexts", [])
                                if c.get("stage") == "final" and c.get("source_type") == "knowledge"
                                and c.get("included_in_prompt") and c.get("text") == evidence["text"]
                                and c.get("file_name") == evidence["file"]
                                and c.get("knowledge_base_id") == evidence["knowledge_base"]]
                    for context in contexts:
                        if context.get("parent_id") and context.get("file_hash"):
                            records.append({**evidence, "context": context})
    # Identical repeated tool history does not create a false ambiguity.
    unique = {}
    for item in records:
        c = item["context"]
        key = (item["id"], item["file"], item["knowledge_base"], c["parent_id"], c["file_hash"], item["text"])
        unique[key] = item
    return list(unique.values())


class CitationService:
    def __init__(self, evaluation_store, pool):
        self.store, self.pool = evaluation_store, pool

    async def initialize(self):
        sql = (Path(__file__).parent / "migrations/008_citation_validation.sql").read_text("utf-8")
        await self.pool.execute(sql)

    async def _current_source(self, user_id, run_kb, record):
        c = record["context"]
        kb = record["knowledge_base"]
        if c.get("user_id", user_id) != user_id or (run_kb != "默认知识库" and kb != run_kb):
            return "unverified", None
        row = await self.pool.fetchrow("""SELECT p.text,p.file_name,p.file_hash FROM parent_chunks p
            JOIN file_metadata f ON f.user_id=p.user_id AND f.knowledge_base_id=p.knowledge_base_id AND f.file_hash=p.file_hash
            JOIN knowledge_bases k ON k.user_id=p.user_id AND k.knowledge_base_id=p.knowledge_base_id
            WHERE p.user_id=$1 AND p.knowledge_base_id=$2 AND p.parent_id=$3 AND p.file_hash=$4
              AND f.file_name=p.file_name""", user_id, kb, str(c["parent_id"]), c["file_hash"])
        if not row:
            return "source_deleted", None
        if row["text"] != record["text"] or row["file_name"] != record["file"]:
            return "source_changed", None
        exists = await self.pool.fetchval("SELECT to_regclass('kb_delete_tasks') IS NOT NULL")
        if exists and await self.pool.fetchval("""SELECT EXISTS(SELECT 1 FROM kb_delete_tasks
            WHERE user_id=$1 AND knowledge_base_id=$2 AND status<>'DONE'
              AND (file_hash='' OR file_hash=$3))""", user_id, kb, c["file_hash"]):
            return "deletion_pending", None
        return "structure_verified", row["text"]

    async def validate(self, run_id, user_id, *, include_citation=None):
        run = await self.store.get_run(run_id, user_id)
        if not run or run.get("user_id") != user_id:
            raise LookupError("Run not found")
        records = visible_evidence(run)
        declared_ids = {r["id"] for r in records}
        entries = []
        selected_text = None
        checked_sources = {}
        for reference in parse_citations(run.get("answer", "")):
            entry = {**reference, "semantic_status": "unverified", "semantic_note": "仅核对引用结构与来源状态，未验证结论是否由原文蕴含。"}
            matches = [r for r in records if r["id"] == reference["evidence_id"] and r["file"] == reference["file"]]
            if not reference["file"]:
                entry["status"] = "unverified"
            elif not matches:
                entry["status"] = "pair_mismatch" if reference["evidence_id"] in declared_ids else "unverified"
            elif len(matches) != 1:
                entry["status"] = "ambiguous"
            else:
                record = matches[0]
                c = record["context"]
                source_key = (c.get("user_id", user_id), record["knowledge_base"], c["parent_id"],
                              c["file_hash"], record["file"], record["text"])
                if source_key not in checked_sources:
                    checked_sources[source_key] = await self._current_source(user_id, run["knowledge_base_id"], record)
                status, source_text = checked_sources[source_key]
                entry["status"] = status
                if status == "structure_verified":
                    entry["knowledge_base_id"] = record["knowledge_base"]
                    entry["source_text_sha256"] = sha256(source_text.encode()).hexdigest()
                    if reference["citation_id"] == include_citation:
                        selected_text = source_text
            entries.append(entry)
        snapshot = {"validation_id": str(uuid4()), "algorithm_version": VERSION,
                    "run_id": str(run_id), "user_id": user_id,
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                    "run_snapshot_sha256": sha256(json.dumps(run, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest(),
                    "semantic_validation": "not_performed", "citations": entries,
                    "statistics": {"citation_occurrences": len(entries), "actual_visible_evidence": len(records),
                                   "status_counts": dict(Counter(e["status"] for e in entries)),
                                   "semantic_verified": 0}}
        await self.pool.execute("""INSERT INTO citation_validation_snapshots
            (validation_id,run_id,user_id,snapshot) VALUES($1,$2,$3,$4::jsonb)""",
            UUID(snapshot["validation_id"]), UUID(str(run_id)), user_id,
            json.dumps(snapshot, ensure_ascii=False))
        if include_citation:
            entry = next((e for e in entries if e["citation_id"] == include_citation), None)
            if entry is None:
                raise LookupError("Citation not found")
            return {"validation_id": snapshot["validation_id"], "checked_at": snapshot["checked_at"],
                    "citation": entry, "text": selected_text}
        return snapshot

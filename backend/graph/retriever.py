"""Query-first entity linking and bounded, directed, evidence-backed templates."""
import re
import json
from datetime import datetime, timezone

from graph.schemas import GraphPath, GraphRecall, normalize, REPORT_GENERIC_NAMES
from graph.semantic_guard import quote_supports_relation

DESCRIPTOR_GENERIC = {'系统', '业务系统', '默认系统', '本系统', 'system', '服务', '平台', '默认平台'}


def descriptor_key(name, entity_type):
    """A query key only: original canonical names and aliases stay unchanged."""
    name = normalize(name)
    if entity_type == 'System':
        stem = re.sub(r'^(当前|目前)', '', name)
        if stem != name and len(stem) >= 4 and stem not in DESCRIPTOR_GENERIC:
            return stem
    return name


def seed_groups(seeds, query):
    grouped = {}
    body = normalize(query)
    for seed in seeds:
        canonical = seed['normalized_name']
        stem = descriptor_key(canonical, seed['entity_type'])
        if stem != canonical and any(prefix+name in body for prefix in ('旧', '历史', '非', '不是', '原有', '原来的') for name in (stem, canonical)):
            continue  # Never reinterpret an old/negated system as the current one.
        aliases = seed.get('aliases') or []
        if isinstance(aliases, str):
            aliases = json.loads(aliases)
        aliases = aliases if isinstance(aliases, list) else []
        labels = [canonical, stem, *(normalize(a) for a in aliases[:8] if isinstance(a, str))]
        matches = [label for label in labels if label and label in body and label not in REPORT_GENERIC_NAMES]
        label = max(matches, key=len) if matches else canonical
        key = stem if label == canonical else label
        grouped.setdefault((seed['knowledge_base_id'], key, seed['entity_type']), []).append(seed)
    return grouped


def relation_templates(query):
    if re.search(r"历史|过去|以前|明年|去年|计划|冲突|比较|差异|对比.*(?:结果|成绩|数值)|版本|\bv\d|historical|previous|next year", query, re.I):
        return []
    if any(int(year) != datetime.now(timezone.utc).year for year in re.findall(r"20\d{2}", query)):
        return []  # the online graph intentionally contains current facts only
    # Names and source links only; metric values and comparisons remain text tasks.
    if re.search(r"多少|数值|得分|置信区间|百分点|how much|score value", query, re.I):
        return []
    if re.search(r"实现|implemented", query, re.I):
        return [("HAS_FEATURE", "IMPLEMENTED_BY"), ("IMPLEMENTED_BY",)]
    if re.search(r"评估|评测|evaluated|evaluation", query, re.I):
        return [("HAS_MODULE", "EVALUATED_ON"), ("HAS_EXPERIMENT", "EVALUATED_ON"), ("EVALUATED_ON",)]
    if re.search(r"指标|metrics?", query, re.I):
        return [("HAS_EXPERIMENT", "REPORTS_METRIC"), ("REPORTS_METRIC",)]
    if re.search(r"功能|features?", query, re.I):
        return [("HAS_FEATURE",)]
    if re.search(r"模块|modules?", query, re.I) and not re.search(r"使用|依赖|属于", query):
        return [("HAS_MODULE",)]
    if re.search(r"实验|experiments?", query, re.I):
        return [("HAS_EXPERIMENT",)]
    if re.search(r"维护|负责|谁管|哪个团队|谁来管|归.*管|谁.*开发|maintain|responsible|owner", query, re.I):
        if "开发" in query:
            return [("DEVELOPED_BY",)]
        return [("USES", "MAINTAINED_BY"), ("DEPENDS_ON", "MAINTAINED_BY"), ("MAINTAINED_BY",)]
    if re.search(r"怎么处理|排障|如何处理|故障|troubleshoot|resolve", query, re.I):
        return [("HAS_ISSUE", "RESOLVED_BY"), ("RESOLVED_BY",), ("HAS_ISSUE",)]
    if re.search(r"使用|依赖|属于|uses|depend|part of", query, re.I):
        return [("USES",), ("DEPENDS_ON",), ("PART_OF",)]
    return []


def active_ready_job_sql():
    """One highest-priority READY version per immutable source file.

    Every caller binds the same ordered readable-version array at $5. A READY
    v3 file shadows its own v2 evidence, including relations removed by v3.
    Failed/pending v3 does not hide a usable, still-source-verified v2 file.
    """
    return """j.status='READY' AND j.extraction_version=ANY($5::text[])
        AND NOT EXISTS(SELECT 1 FROM kg_index_jobs preferred
            WHERE preferred.user_id=j.user_id AND preferred.knowledge_base_id=j.knowledge_base_id
            AND preferred.file_hash=j.file_hash AND preferred.status='READY'
            AND preferred.extraction_version=ANY($5::text[])
            AND array_position($5::text[],preferred.extraction_version)
                <array_position($5::text[],j.extraction_version))"""


class GraphRetriever:
    def __init__(self, repository, settings):
        self.repository, self.settings = repository, settings

    async def recall(self, query, scope, parent_candidates):
        templates = [p for p in relation_templates(query) if len(p) <= self.settings.max_hops]
        if not templates:
            return GraphRecall()
        user, kb = scope.user_id, scope.knowledge_base_id
        all_kbs = kb == "默认知识库"
        versions = list(self.settings.readable_versions)
        active_job = active_ready_job_sql()
        pool = self.repository.pool
        # SQL performs the user/KB filter, even for the entity seed lookup.
        seeds = await pool.fetch(f"""SELECT n.* FROM kg_entities n WHERE n.user_id=$1
            AND ($2::boolean OR n.knowledge_base_id=$3)
            AND (position(n.normalized_name in $4)>0 OR (
                n.entity_type='System' AND n.normalized_name ~ '^(当前|目前)'
                AND length(regexp_replace(n.normalized_name,'^(当前|目前)',''))>=4
                AND regexp_replace(n.normalized_name,'^(当前|目前)','') NOT IN ('业务系统','默认系统','system','默认平台')
                AND position(regexp_replace(n.normalized_name,'^(当前|目前)','') in $4)>0) OR EXISTS(
                SELECT 1 FROM jsonb_array_elements_text(n.aliases) a WHERE length(a)>1
                AND lower(a) NOT IN ('redis','mysql','数据库','缓存','系统','实例','集群','功能','模块','组件','模型','数据集','样本集','指标','实验','报告',
                    'feature','module','model','dataset','metric','experiment','report') AND position(lower(a) in $4)>0))
            AND EXISTS(SELECT 1 FROM kg_entity_mentions m JOIN kg_index_jobs j ON j.job_id=m.job_id
                WHERE m.entity_id=n.entity_id AND m.extraction_version=ANY($5::text[]) AND {active_job})
            ORDER BY length(n.normalized_name) DESC,n.entity_id LIMIT 13""", user, all_kbs, kb, normalize(query), versions)
        if len(seeds)>12:
            # Never decide uniqueness from a truncated candidate list.
            return GraphRecall(seed_method='candidate_limit')
        seed_method = "query"
        # Same name/type with different qualifiers in one KB is ambiguous. Only
        # select it when exactly one candidate is also mentioned by vector evidence.
        grouped = seed_groups(seeds, query)
        safe = [items[0] for items in grouped.values() if len(items) == 1]
        if not safe and parent_candidates:
            seeds = await pool.fetch(f"""SELECT DISTINCT n.* FROM kg_entities n
                JOIN kg_entity_mentions m ON m.entity_id=n.entity_id AND m.user_id=n.user_id AND m.knowledge_base_id=n.knowledge_base_id
                JOIN kg_index_jobs j ON j.job_id=m.job_id
                WHERE n.user_id=$1 AND ($2::boolean OR n.knowledge_base_id=$3) AND m.parent_id=ANY($4::text[])
                AND m.extraction_version=ANY($5::text[]) AND {active_job}
                ORDER BY n.normalized_name,n.entity_id LIMIT 13""",
                user, all_kbs, kb, [p.parent_id for p in parent_candidates], versions)
            if len(seeds)>12:
                return GraphRecall(seed_method='candidate_limit')
            grouped = seed_groups(seeds, query)
            safe = [items[0] for items in grouped.values() if len(items) == 1]
            seed_method = "vector_mentions"
        safe = safe[:self.settings.max_seeds]
        paths, cache, seen_relations, seen_paths = [], {}, set(), set()
        rejected_relations = set()

        async def edges(entity, entity_kb, predicate):
            key = entity, entity_kb, predicate
            if key in cache:
                return cache[key]
            remaining = self.settings.max_relations - len(seen_relations)
            if remaining <= 0:
                return []
            rows = await pool.fetch(f"""SELECT r.relation_id,r.object_id,e.parent_id,e.quote,
                s.canonical_name AS subject_name,s.aliases AS subject_aliases,s.entity_type AS subject_type,
                t.canonical_name AS target_name,t.aliases AS target_aliases,t.entity_type AS target_type
                FROM kg_relations r
                JOIN kg_entities s ON s.entity_id=r.subject_id AND s.user_id=r.user_id AND s.knowledge_base_id=r.knowledge_base_id
                JOIN kg_entities t ON t.entity_id=r.object_id AND t.user_id=r.user_id AND t.knowledge_base_id=r.knowledge_base_id
                JOIN LATERAL (SELECT ev.parent_id,ev.quote FROM kg_relation_evidence ev
                    JOIN parent_chunks p ON (p.parent_id,p.knowledge_base_id,p.user_id,p.file_hash)=
                        (ev.parent_id,ev.knowledge_base_id,ev.user_id,ev.file_hash)
                    JOIN kg_index_jobs j ON j.job_id=ev.job_id
                    WHERE ev.relation_id=r.relation_id AND ev.user_id=$1 AND ev.knowledge_base_id=$2
                    AND ev.extraction_version=r.extraction_version AND {active_job}
                    AND strpos(lower(regexp_replace(p.text,'[[:space:]]','','g')),
                               lower(regexp_replace(ev.quote,'[[:space:]]','','g')))>0
                    ORDER BY ev.parent_id LIMIT 1) e ON true
                WHERE r.user_id=$1 AND r.knowledge_base_id=$2 AND r.subject_id=$3
                AND r.predicate=$4 AND r.extraction_version=ANY($5::text[]) ORDER BY r.relation_id LIMIT $6""",
                user, entity_kb, entity, predicate, versions, remaining)
            seen_relations.update(r["relation_id"] for r in rows)
            accepted = []
            for row in rows:
                if quote_supports_relation(row["subject_name"], predicate, row["target_name"], row["quote"],
                    subject_aliases=row["subject_aliases"], target_aliases=row["target_aliases"],
                    subject_type=row["subject_type"], target_type=row["target_type"]):
                    accepted.append(row)
                else:
                    rejected_relations.add(row["relation_id"])
            cache[key] = accepted
            return accepted

        for seed in safe:
            for template in templates:
                frontier = [(seed["entity_id"], [], [])]
                for predicate in template:
                    expanded = []
                    for entity, relations, parents in frontier:
                        for edge in await edges(entity, seed["knowledge_base_id"], predicate):
                            if str(edge["relation_id"]) in relations:
                                continue
                            expanded.append((edge["object_id"], relations + [str(edge["relation_id"])],
                                             list(dict.fromkeys(parents + [edge["parent_id"]]))))
                    frontier = expanded[:self.settings.max_relations]
                for _, relations, parents in frontier:
                    signature = tuple(relations)
                    if signature not in seen_paths and len(parents) <= self.settings.max_parents:
                        existing = {(p.knowledge_base_id, parent) for p in paths for parent in p.parent_ids}
                        additional = {(seed["knowledge_base_id"], parent) for parent in parents}
                        if len(existing | additional) <= self.settings.max_parents:
                            seen_paths.add(signature)
                            paths.append(GraphPath(seed["knowledge_base_id"], relations, parents, list(template)))
        ids = list(dict.fromkeys(parent for path in paths for parent in path.parent_ids))
        records = []
        if ids:
            records = await pool.fetch("""SELECT parent_id,user_id,knowledge_base_id,file_hash,file_name,text FROM parent_chunks
                WHERE user_id=$1 AND ($2::boolean OR knowledge_base_id=$3) AND parent_id=ANY($4::text[])""", user, all_kbs, kb, ids)
        legal = {(r["knowledge_base_id"], r["parent_id"]) for r in records}
        paths = [p for p in paths if all((p.knowledge_base_id, parent) in legal for parent in p.parent_ids)]
        return GraphRecall(paths, [dict(r) for r in records], len(safe), seed_method, len(rejected_relations))

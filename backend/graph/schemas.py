from dataclasses import dataclass, field
from typing import Literal
import re
import unicodedata
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field

EntityType = Literal["System", "Service", "Database", "CacheCluster", "Team", "Issue", "Procedure", "Product", "Organization",
                     "Feature", "Module", "Dataset", "Metric", "Experiment", "Report", "Model"]
Predicate = Literal["USES", "DEPENDS_ON", "MAINTAINED_BY", "DEVELOPED_BY", "HAS_ISSUE", "RESOLVED_BY", "PART_OF",
                    "HAS_FEATURE", "HAS_MODULE", "IMPLEMENTED_BY", "EVALUATED_ON", "REPORTS_METRIC", "HAS_EXPERIMENT"]
PREDICATES = ("USES", "DEPENDS_ON", "MAINTAINED_BY", "DEVELOPED_BY", "HAS_ISSUE", "RESOLVED_BY", "PART_OF",
              "HAS_FEATURE", "HAS_MODULE", "IMPLEMENTED_BY", "EVALUATED_ON", "REPORTS_METRIC", "HAS_EXPERIMENT")
DOMAINS = {
    "USES": ({"System", "Service", "Product", "Module"}, {"CacheCluster", "Database", "Service", "Module", "Model"}),
    "DEPENDS_ON": ({"System", "Service", "Product", "Module"}, {"System", "Service", "Database", "CacheCluster", "Module", "Model"}),
    "MAINTAINED_BY": ({"System", "Service", "Product", "CacheCluster", "Database"}, {"Team", "Organization"}),
    "DEVELOPED_BY": ({"System", "Service", "Product"}, {"Team", "Organization"}),
    "HAS_ISSUE": ({"System", "Service", "CacheCluster", "Database"}, {"Issue"}),
    "RESOLVED_BY": ({"Issue"}, {"Procedure"}),
    "PART_OF": ({"Service", "CacheCluster", "Database", "Module"}, {"System", "Product", "Module"}),
    "HAS_FEATURE": ({"System", "Service", "Product"}, {"Feature"}),
    "HAS_MODULE": ({"System", "Service", "Product"}, {"Module"}),
    "IMPLEMENTED_BY": ({"Feature"}, {"Module", "Model", "Service"}),
    "EVALUATED_ON": ({"System", "Service", "Product", "Module", "Model", "Feature", "Experiment"}, {"Dataset"}),
    "REPORTS_METRIC": ({"Report", "Experiment", "System", "Product", "Module", "Model"}, {"Metric"}),
    "HAS_EXPERIMENT": ({"System", "Product", "Module", "Report"}, {"Experiment"}),
}
REPORT_GENERIC_NAMES = {"功能", "模块", "组件", "模型", "数据集", "样本集", "指标", "实验", "报告",
                        "feature", "module", "model", "dataset", "metric", "experiment", "report"}


class Entity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    local_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=160)
    type: EntityType
    aliases: list[str] = Field(default_factory=list, max_length=8)
    qualifier: str = Field(default="", max_length=160)


class Relation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject_local_id: str = Field(min_length=1, max_length=64)
    predicate: Predicate
    object_local_id: str = Field(min_length=1, max_length=64)
    evidence_quote: str = Field(min_length=1, max_length=1000)
    factuality: Literal["asserted", "historical", "planned", "negated", "uncertain"]


class Extraction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entities: list[Entity] = Field(max_length=48)
    relations: list[Relation] = Field(max_length=64)


def normalize(value):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).casefold()


def validated(extraction, text):
    """Conservative textual grounding, not a claim that LLM semantics are infallible."""
    raw = Extraction.model_validate(extraction)
    if len({e.local_id for e in raw.entities}) != len(raw.entities):
        raise ValueError("Duplicate entity local IDs")
    body = normalize(text)
    entities = {}
    for entity in raw.entities:
        if not normalize(entity.name) or normalize(entity.name) not in body or (entity.qualifier and normalize(entity.qualifier) not in body):
            continue
        if entity.type in ("CacheCluster", "Database", "System") and normalize(entity.name) in ("redis", "mysql", "数据库", "系统", "缓存"):
            continue  # generic concepts must not become instance seeds
        if entity.type == "Metric" and re.fullmatch(r"[+-]?\d+(?:[.,]\d+)?(?:%|分|秒|ms|指标|值)?", normalize(entity.name)):
            continue  # values stay in quoted text, never stand in for metric identity
        if entity.type in {"Feature", "Module", "Model", "Dataset", "Metric", "Experiment", "Report"} and normalize(entity.name) in REPORT_GENERIC_NAMES:
            continue
        entity.aliases = [a for a in entity.aliases if 1 < len(a) <= 160 and normalize(a) in body
                          and normalize(a) not in {"redis", "mysql", "数据库", "缓存", "系统", "实例", "集群", *REPORT_GENERIC_NAMES}]
        qualifier = normalize(entity.qualifier)
        # A generic description is not an identity discriminator. Otherwise one
        # file's "independent instance" and another's "instance" split one cache.
        redundant = {"实例", "独立实例", "集群", "独立集群", "instance", "independentinstance"}
        stem = re.sub(r"实例$|集群$|instance$|cluster$", "", qualifier)
        if qualifier in redundant or (stem and stem != qualifier and normalize(entity.name).startswith(stem)):
            entity.qualifier = ""
        entities[entity.local_id] = entity
    accepted = []
    for relation in raw.relations:
        quote = normalize(relation.evidence_quote)
        if relation.factuality != "asserted" or quote not in body:
            continue
        if re.search(r"不维护|不使用|尚未|计划|已终止|曾经|并非|\bnot\b|planned|formerly", relation.evidence_quote, re.I):
            continue
        if any(int(year) != datetime.now(timezone.utc).year for year in re.findall(r"(20\d{2})\s*年", relation.evidence_quote)):
            continue  # conservative: historical dates are not online current edges
        subject, target = entities.get(relation.subject_local_id), entities.get(relation.object_local_id)
        if not subject or not target or subject.local_id == target.local_id:
            continue
        domains, ranges = DOMAINS[relation.predicate]
        if subject.type not in domains or target.type not in ranges:
            continue
        if not all(any(normalize(n) in quote for n in [e.name, *e.aliases]) for e in (subject, target)):
            continue
        if relation.predicate in {"HAS_FEATURE", "HAS_MODULE", "IMPLEMENTED_BY", "EVALUATED_ON", "REPORTS_METRIC", "HAS_EXPERIMENT"}:
            from graph.semantic_guard import quote_supports_relation
            if not quote_supports_relation(subject.name, relation.predicate, target.name, relation.evidence_quote,
                                           subject_aliases=subject.aliases, target_aliases=target.aliases,
                                           subject_type=subject.type, target_type=target.type):
                continue
        accepted.append(relation)
    return Extraction(entities=list(entities.values()), relations=accepted), len(raw.relations) - len(accepted)


@dataclass
class GraphPath:
    knowledge_base_id: str
    relation_ids: list[str]
    parent_ids: list[str]
    predicates: list[str]


@dataclass
class GraphRecall:
    paths: list[GraphPath] = field(default_factory=list)
    records: list[dict] = field(default_factory=list)
    seed_count: int = 0
    seed_method: str = "none"
    rejected_relation_count: int = 0

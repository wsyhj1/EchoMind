"""Scores keep their original spaces; identity always includes tenant and KB."""
from dataclasses import dataclass, field
from typing import Literal
from uuid import uuid4


@dataclass(frozen=True)
class RetrievalScope:
    user_id: int
    knowledge_base_id: str
    allowed_scope_ids: tuple[str, ...] | None = None

    def __post_init__(self):
        if type(self.user_id) is not int or self.user_id < 1:
            raise ValueError("user_id must be a positive integer")
        if not isinstance(self.knowledge_base_id, str) or not self.knowledge_base_id or len(self.knowledge_base_id) > 128:
            raise ValueError("knowledge_base_id must contain 1..128 characters")


@dataclass(frozen=True)
class RetrievalOptions:
    retrieval_mode: Literal["hybrid", "dense"] = "hybrid"
    rerank_enabled: bool = True
    graph_enabled: bool | None = None
    evidence_enabled: bool | None = None

    def __post_init__(self):
        if self.retrieval_mode not in ("hybrid", "dense"):
            raise ValueError("Unsupported retrieval mode")


@dataclass(frozen=True)
class ChildHit:
    parent_id: str
    knowledge_base_id: str
    child_id: str
    rank: int
    score: float | None
    score_type: str
    source: str
    file_hash: str = ""
    fallback: bool = False


@dataclass
class RetrievalCandidate:
    parent_id: str
    user_id: int
    knowledge_base_id: str
    file_hash: str
    file_name: str
    text: str
    sources: list[str] = field(default_factory=list)
    child_hits: list[ChildHit] = field(default_factory=list)
    rerank_score: float | None = None
    evidence_relation_ids: list[str] = field(default_factory=list)

    @property
    def key(self):
        return self.user_id, self.knowledge_base_id, self.parent_id


@dataclass
class RetrievalTrace:
    request_id: str = field(default_factory=lambda: str(uuid4()))
    retrieval_mode: str = "hybrid"
    steps_ms: dict[str, float] = field(default_factory=dict)
    candidate_keys: list[tuple[int, str, str]] = field(default_factory=list)
    final_keys: list[tuple[int, str, str]] = field(default_factory=list)
    missing_parent_keys: list[tuple[int, str, str]] = field(default_factory=list)
    fallback_reasons: list[str] = field(default_factory=list)
    graph_used: bool = False
    graph_seed_count: int = 0
    graph_seed_method: str = "none"
    graph_rejected_relation_count: int = 0
    graph_semantic_policy: str = ""
    graph_paths: list[dict] = field(default_factory=list)
    rerank_input_keys: list[tuple[int, str, str]] = field(default_factory=list)
    evidence_check_enabled: bool = False
    evidence_before: dict = field(default_factory=dict)
    evidence_coverage: dict = field(default_factory=dict)
    extra_search_count: int = 0
    extra_candidate_count: int = 0
    extra_selected_count: int = 0


@dataclass
class RetrievalResult:
    candidates: list[RetrievalCandidate]
    selected: list[RetrievalCandidate]
    trace: RetrievalTrace

    def context_text(self):
        if self.trace.evidence_check_enabled:
            from rag.targeted_retriever import checked_context
            return checked_context(self)
        if not self.selected:
            return "未检索到相关文档"
        from rag.context import source_context
        return source_context(self.selected)

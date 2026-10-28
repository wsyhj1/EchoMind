"""Structured Milvus → scoped PostgreSQL → index-mapped reranking pipeline."""
import asyncio
import logging
import math
from dataclasses import asdict
from time import perf_counter

from observability import current_trace, measure, record_context
from rag.settings import env_bool
from rag.types import RetrievalCandidate, RetrievalOptions, RetrievalResult, RetrievalScope, RetrievalTrace

logger = logging.getLogger(__name__)


class RetrievalService:
    def __init__(self, milvus, database, reranker, graph_retriever=None, evidence_checker=None):
        self.milvus = milvus
        self.database = database
        self.reranker = reranker
        self.graph_retriever = graph_retriever
        self.evidence_checker = evidence_checker

    async def retrieve_context(self, query: str, scope: RetrievalScope, top_k=5,
                               options: RetrievalOptions | None = None) -> RetrievalResult:
        options = options or RetrievalOptions()
        start = perf_counter()
        result = await self._retrieve_once(query, scope, top_k, options)
        enabled = options.evidence_enabled if options.evidence_enabled is not None else env_bool("EVIDENCE_CHECK_ENABLED")
        if enabled:
            check_start = perf_counter()
            extension = None
            with measure("evidence", "knowledge") as call:
                try:
                    from rag.evidence_settings import EvidenceSettings
                    from rag.targeted_retriever import TargetedRetriever
                    settings = EvidenceSettings.from_env()
                    extension = TargetedRetriever(self._retrieve_once, self.reranker, settings, self.evidence_checker)
                    active = current_trace()
                    # The Agent may rewrite its tool query; the user's original
                    # goals/time/version constraints remain the check's authority.
                    check_query = active.query if active is not None and isinstance(active.query, str) and active.query.strip() else query
                    result = await asyncio.wait_for(extension.complete(check_query, scope, top_k, options, result),
                                                    settings.total_timeout_ms / 1000)
                    call["attributes"].update(coverage=result.trace.evidence_coverage,
                                              extra_search_count=result.trace.extra_search_count)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    result.trace.fallback_reasons.append("evidence_" + type(error).__name__)
                    result.trace.extra_search_count = extension.extra_search_count if extension else 0
                    call["status"] = "failed"
                    call["attributes"]["fallback"] = True
            result.trace.steps_ms["evidence"] = (perf_counter() - check_start) * 1000
        result.trace.final_keys = [candidate.key for candidate in result.selected]
        result.trace.steps_ms["total"] = (perf_counter() - start) * 1000
        for rank, candidate in enumerate(result.selected, 1):
            self._record("final", candidate, rank, query)
        if env_bool("RAG_TRACE_ENABLED"):
            logger.info("rag_retrieval request=%s mode=%s candidates=%d final=%d extra_search=%d fallback=%s duration_ms=%.2f",
                        result.trace.request_id, result.trace.retrieval_mode, len(result.candidates), len(result.selected),
                        result.trace.extra_search_count, ",".join(result.trace.fallback_reasons), result.trace.steps_ms["total"])
        return result

    async def _retrieve_once(self, query: str, scope: RetrievalScope, top_k=5,
                             options: RetrievalOptions | None = None) -> RetrievalResult:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must not be empty")
        if type(top_k) is not int or not 1 <= top_k <= 20:
            raise ValueError("top_k must be between 1 and 20")
        options = options or RetrievalOptions()
        trace = RetrievalTrace(retrieval_mode=options.retrieval_mode)
        total = perf_counter()
        start = perf_counter()
        hits = await self.milvus.retrieve_knowledge_hits(query=query, knowledge_base_id=scope.knowledge_base_id,
            user_id=scope.user_id, top_k=top_k, retrieval_mode=options.retrieval_mode)
        if scope.allowed_scope_ids is not None:
            hits = [hit for hit in hits if hit.knowledge_base_id in scope.allowed_scope_ids]
        trace.steps_ms["vector"] = (perf_counter() - start) * 1000
        if any(hit.fallback for hit in hits):
            trace.fallback_reasons.append("hybrid_to_dense")
        start = perf_counter()
        with measure("parent_fetch", "knowledge"):
            records = await self.database.get_parents_with_metadata(
                [hit.parent_id for hit in hits], scope.knowledge_base_id, scope.user_id) if hits else []
        trace.steps_ms["postgres"] = (perf_counter() - start) * 1000
        # SQL enforces authorization. Composite matching additionally prevents a
        # default-KB query from merging equal IDs in two authorized KBs.
        by_key = {(r["user_id"], r["knowledge_base_id"], r["parent_id"]): r for r in records}
        candidates = {}
        for hit in hits:
            key = scope.user_id, hit.knowledge_base_id, hit.parent_id
            record = by_key.get(key)
            if record is None or (hit.file_hash and record["file_hash"] != hit.file_hash):
                if key not in trace.missing_parent_keys:
                    trace.missing_parent_keys.append(key)
                continue
            if key not in candidates:
                candidates[key] = RetrievalCandidate(**{name: record[name] for name in (
                    "parent_id", "user_id", "knowledge_base_id", "file_hash", "file_name", "text")})
            candidate = candidates[key]
            candidate.child_hits.append(hit)
            if hit.source not in candidate.sources:
                candidate.sources.append(hit.source)
        ordered = list(candidates.values())
        paths = []
        graph_settings = None
        use_graph = options.graph_enabled if options.graph_enabled is not None else env_bool("GRAPH_RAG_ENABLED")
        if use_graph:
            start = perf_counter()
            with measure("graph_retrieval", "knowledge") as call:
                try:
                    from graph.settings import GraphSettings
                    from graph.repository import GraphRepository
                    from graph.retriever import GraphRetriever
                    from graph.fusion import fuse
                    from graph.semantic_guard import POLICY_VERSION
                    graph_settings = GraphSettings.from_env()
                    retriever = self.graph_retriever or GraphRetriever(GraphRepository(self.database.pool), graph_settings)
                    recall = await asyncio.wait_for(retriever.recall(query, scope, ordered), graph_settings.query_timeout_ms / 1000)
                    if scope.allowed_scope_ids is not None:
                        recall.records = [p for p in recall.records if p['knowledge_base_id'] in scope.allowed_scope_ids]
                        recall.paths = [p for p in recall.paths if p.knowledge_base_id in scope.allowed_scope_ids]
                    ordered = fuse(ordered, recall, scope.user_id)
                    paths = recall.paths
                    trace.graph_used = bool(paths)
                    trace.graph_seed_count, trace.graph_seed_method = recall.seed_count, recall.seed_method
                    trace.graph_rejected_relation_count = recall.rejected_relation_count
                    trace.graph_semantic_policy = POLICY_VERSION
                    trace.graph_paths = [asdict(path) for path in paths]
                    call["attributes"].update(seed_count=recall.seed_count, seed_method=recall.seed_method,
                                              rejected_relation_count=recall.rejected_relation_count, semantic_policy=POLICY_VERSION,
                                              path_count=len(paths), paths=trace.graph_paths)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    trace.fallback_reasons.append("graph_" + type(error).__name__)
                    call["status"] = "failed"
                    call["attributes"]["fallback"] = True
            trace.steps_ms["graph"] = (perf_counter() - start) * 1000
        for rank, candidate in enumerate(ordered, 1):
            self._record("parent", candidate, rank, query)
        trace.candidate_keys = [c.key for c in ordered]
        selected = ordered[:top_k]
        if ordered and options.rerank_enabled:
            start = perf_counter()
            # The existing API helper sends at most min(3K,30) documents. Keep
            # that same input list here so an API index cannot map beyond it.
            submitted = ordered[:min(top_k * 3, 30)]
            if paths:
                from graph.fusion import retain_paths
                submitted = retain_paths(submitted, ordered, paths, min(top_k * 3, 30), graph_settings.max_parents)
            trace.rerank_input_keys = [candidate.key for candidate in submitted]
            with measure("rerank", "knowledge") as call:
                try:
                    response = await self.reranker(query, [c.text for c in submitted], top_k)
                    selected = self._rerank_selection(response, submitted, top_k)
                    if not selected:
                        raise ValueError("empty or malformed rerank result")
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    selected = ordered[:top_k]
                    trace.fallback_reasons.append("rerank_" + type(error).__name__)
                    call["status"] = "failed"
                    call["attributes"]["fallback"] = True
            trace.steps_ms["rerank"] = (perf_counter() - start) * 1000
        if paths:
            from graph.fusion import retain_paths
            selected = retain_paths(selected, ordered, paths, top_k, graph_settings.bridge_quota)
        trace.final_keys = [c.key for c in selected]
        trace.steps_ms["total"] = (perf_counter() - total) * 1000
        return RetrievalResult(ordered, selected, trace)

    @staticmethod
    def _rerank_selection(response, submitted, top_k):
        if not isinstance(response, dict):
            raise ValueError("invalid rerank response")
        output = response.get("output")
        if not isinstance(output, dict) or not isinstance(output.get("results"), list):
            raise ValueError("invalid rerank results")
        selected, seen, scores = [], set(), []
        for item in output["results"]:
            if not isinstance(item, dict):
                raise ValueError("invalid rerank item")
            index = item.get("index")
            if type(index) is not int or not 0 <= index < len(submitted) or index in seen:
                raise ValueError("invalid or duplicate rerank index")
            score = item.get("relevance_score")
            if score is not None and (type(score) not in (int, float) or not math.isfinite(score)):
                raise ValueError("invalid rerank score")
            seen.add(index)
            selected.append(submitted[index])
            scores.append(score)
        # Commit scores only after every item is valid. A malformed later item
        # must not leave partial rerank scores on the fallback candidates.
        for candidate, score in zip(selected, scores):
            candidate.rerank_score = score
        return selected[:top_k]

    @staticmethod
    def _record(stage, candidate, rank, query):
        first = candidate.child_hits[0] if candidate.child_hits else None
        record_context(stage, "knowledge", candidate.text, source_id=candidate.parent_id,
                       parent_id=candidate.parent_id, user_id=candidate.user_id,
                       knowledge_base_id=candidate.knowledge_base_id, file_hash=candidate.file_hash,
                       file_name=candidate.file_name, rank=rank, query=query,
                       sources=candidate.sources, included_in_prompt=stage == "final",
                       evidence_relation_ids=candidate.evidence_relation_ids,
                       score=candidate.rerank_score if candidate.rerank_score is not None else (first.score if first else None),
                       score_type="rerank" if candidate.rerank_score is not None else (first.score_type if first else None))

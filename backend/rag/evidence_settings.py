from dataclasses import dataclass

from graph.settings import bounded


@dataclass(frozen=True)
class EvidenceSettings:
    max_rounds: int = 1
    extra_parent_limit: int = 3
    check_timeout_ms: int = 800
    retrieval_timeout_ms: int = 10000
    total_timeout_ms: int = 12000
    max_context_chars: int = 12000

    @classmethod
    def from_env(cls):
        return cls(bounded("EVIDENCE_MAX_RETRIEVAL_ROUNDS", 1, 0, 1),
                   bounded("EVIDENCE_EXTRA_PARENT_LIMIT", 3, 1, 3),
                   bounded("EVIDENCE_CHECK_TIMEOUT_MS", 800, 1, 5000),
                   bounded("EVIDENCE_RETRIEVAL_TIMEOUT_MS", 10000, 1, 30000),
                   bounded("EVIDENCE_TOTAL_TIMEOUT_MS", 12000, 1, 60000),
                   bounded("EVIDENCE_MAX_CONTEXT_CHARS", 12000, 2000, 40000))

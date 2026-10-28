from dataclasses import dataclass
import os

def bounded(name, default, low, high):
    value = int(os.getenv(name, str(default)))
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return value


@dataclass(frozen=True)
class GraphSettings:
    version: str = "v3"
    max_hops: int = 2
    max_seeds: int = 3
    max_relations: int = 20
    max_parents: int = 5
    query_timeout_ms: int = 400
    bridge_quota: int = 2
    batch_size: int = 4
    extraction_timeout_seconds: int = 30

    @property
    def readable_versions(self):
        # Compatibility is explicit and one-way: pinning v2 preserves old reads.
        return ("v3", "v2") if self.version == "v3" else (self.version,)

    @classmethod
    def from_env(cls):
        version = os.getenv("GRAPH_EXTRACTION_VERSION", "v3")
        if not version or len(version) > 32:
            raise ValueError("Invalid extraction version")
        return cls(version, bounded("GRAPH_MAX_HOPS", 2, 1, 2), bounded("GRAPH_MAX_SEEDS", 3, 1, 3),
            bounded("GRAPH_MAX_RELATIONS", 20, 1, 100), bounded("GRAPH_MAX_EVIDENCE_PARENTS", 5, 1, 10),
            bounded("GRAPH_QUERY_TIMEOUT_MS", 400, 1, 5000), bounded("GRAPH_BRIDGE_QUOTA", 2, 0, 3),
            bounded("GRAPH_EXTRACTION_BATCH_SIZE", 4, 1, 10), bounded("GRAPH_EXTRACTION_TIMEOUT_SECONDS", 30, 1, 120))

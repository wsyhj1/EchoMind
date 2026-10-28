"""Freeze local pre-change modules and non-secret settings; never overwrites a baseline."""
import argparse
import ast
from datetime import datetime, timezone
from hashlib import sha256
import importlib.metadata
import json
from pathlib import Path
import platform
import subprocess

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
MODULES = ("tools", "knowledeg_base_manager", "milvus_client", "postgresql_client", "documents_process",
           "agent", "config", "schemas", "observability")


def freeze(manifest_path, directory):
    if manifest_path.exists() or directory.exists():
        raise FileExistsError("A baseline already exists; choose new paths rather than replacing it")
    env = dotenv_values(ROOT / ".env")
    safe_keys = ("AGENT_BASE_MODEL", "SUMMARIZATION_MODEL", "EMBEDDING_MODEL", "dense_dimension", "RERANK_MODEL",
                 "knowledge_base_collection", "memory_collection")
    sources = {f"backend/{name}.py": (ROOT / "backend" / f"{name}.py").read_bytes() for name in MODULES}
    chunking = {}
    for node in ast.walk(ast.parse(sources["backend/config.py"].decode("utf-8-sig"))):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in (
                        "parent_chunk_size", "parent_chunk_overlap", "child_chunk_size", "child_chunk_overlap", "separators"):
                    chunking[target.id] = ast.literal_eval(node.value)
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "git_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "dirty_worktree": subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).splitlines(),
        "python": platform.python_version(), "models_and_collections": {key: env.get(key) for key in safe_keys},
        "source_sha256": {name: sha256(content).hexdigest() for name, content in sources.items()},
        "snapshot_directory": str(directory.resolve()),
        "chunking": chunking,
        "packages": {name: importlib.metadata.version(name) for name in (
            "pymilvus", "asyncpg", "redis", "langchain", "langchain-core", "langgraph", "httpx", "dashscope")},
        "baseline_status": "frozen_not_measured",
        "baseline_definition": "Actual local worktree, including pre-existing uncommitted changes; not upstream HEAD",
    }
    directory.mkdir(parents=True)
    for name, content in sources.items():
        (directory / Path(name).name).write_bytes(content)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=ROOT / "docs/rag_accuracy/baseline_manifest.json")
    parser.add_argument("--directory", type=Path, default=ROOT / ".runtime/rag_accuracy/legacy_source")
    args = parser.parse_args()
    manifest = freeze(args.manifest, args.directory)
    print("Frozen", len(manifest["source_sha256"]), "modules at", manifest["git_sha"])


if __name__ == "__main__":
    main()

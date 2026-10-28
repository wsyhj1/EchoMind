"""Isolated, resumable PDF/DOCX ingestion benchmark. Never touches the public corpus.

Each import owns a PostgreSQL schema and a separate Milvus collection. Reuse the
state file for both runs so generated parent UUIDs and embeddings stay identical.
"""
import argparse
import asyncio
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import sys
from unittest.mock import AsyncMock, patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

# Only the tables used by DocumentProcessor, in a private schema. The production
# parent key and foreign keys are reproduced exactly; no memory/profile tables.
DDL = """
CREATE TABLE IF NOT EXISTS users(user_id SERIAL PRIMARY KEY, created_at TIMESTAMP DEFAULT now());
ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT now();
CREATE TABLE IF NOT EXISTS knowledge_bases(
 knowledge_base_id VARCHAR(128) NOT NULL, user_id INT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
 created_at TIMESTAMP DEFAULT now(), PRIMARY KEY(knowledge_base_id,user_id));
ALTER TABLE knowledge_bases ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT now();
CREATE TABLE IF NOT EXISTS file_metadata(
 file_hash VARCHAR(64), file_name VARCHAR(255), knowledge_base_id VARCHAR(128), user_id INT,
 uploaded_at TIMESTAMP DEFAULT now(), PRIMARY KEY(file_hash,knowledge_base_id,user_id),
 FOREIGN KEY(knowledge_base_id,user_id) REFERENCES knowledge_bases ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS parent_chunks(
 parent_id VARCHAR(128), knowledge_base_id VARCHAR(128), user_id INT, text TEXT NOT NULL,
 file_name VARCHAR(255),file_hash VARCHAR(64),created_at TIMESTAMP DEFAULT now(),updated_at TIMESTAMP DEFAULT now(),
 PRIMARY KEY(parent_id,knowledge_base_id,user_id),
 FOREIGN KEY(file_hash,knowledge_base_id,user_id) REFERENCES file_metadata ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS chunk_hashes(
 chunk_hash VARCHAR(64),file_hash VARCHAR(64),knowledge_base_id VARCHAR(128),user_id INT,
 PRIMARY KEY(chunk_hash,file_hash,knowledge_base_id,user_id),
 FOREIGN KEY(file_hash,knowledge_base_id,user_id) REFERENCES file_metadata ON DELETE CASCADE);
"""


def save_state(path, state):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def validate_state(state):
    for key in ("schema", "collection"):
        if not re.fullmatch(r"rag_accuracy_[0-9a-f]{12}", state[key]):
            raise ValueError("Refusing access to a resource outside the benchmark namespace")


async def connect(state):
    import asyncpg
    from dotenv import load_dotenv
    from langchain_community.embeddings import DashScopeEmbeddings
    from pymilvus import AsyncMilvusClient
    from knowledeg_base_manager import KnowledgeBaseManager
    from postgresql_client import PostgreSQLParentClient
    load_dotenv(ROOT / ".env")
    validate_state(state)
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=3,
                                     command_timeout=20, server_settings={"search_path": state["schema"]})
    database = object.__new__(PostgreSQLParentClient)
    database.pool = pool
    client = AsyncMilvusClient(uri=os.environ["Milvus_url"], token=os.getenv("Token", ""))
    embeddings = DashScopeEmbeddings(model=os.environ["EMBEDDING_MODEL"],
                                     dashscope_api_key=os.environ["DASHSCOPE_API_KEY"])
    manager = KnowledgeBaseManager(client, embeddings, state["collection"], int(os.getenv("dense_dimension", "1024")))
    return database, manager


def normalize(text):
    return re.sub(r"\s+", "", text)


async def ingest(args):
    import asyncpg
    from dotenv import load_dotenv
    from evaluation.rag_fixtures import CORPUS_VERSION, build_files
    from documents_process import DocumentProcessor
    from hash_storage import HashStorage
    load_dotenv(ROOT / ".env")
    args.state.parent.mkdir(parents=True, exist_ok=True)
    if args.state.exists():
        state = json.loads(args.state.read_text(encoding="utf-8"))
        if state.get("corpus_version") != CORPUS_VERSION:
            raise ValueError("Corpus version differs; use a new state path")
    if not args.state.exists() or state.get("status") == "cleaned":
        name = "rag_accuracy_" + uuid4().hex[:12]
        state = {"schema": name, "collection": name, "corpus_version": CORPUS_VERSION,
                 "status": "indexing", "documents": [], "source_map": {}}
        save_state(args.state, state)  # recoverable resource names before creation
    validate_state(state)
    conn = await asyncpg.connect(os.environ["DATABASE_URL"], timeout=5)
    try:
        await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{state["schema"]}"')
    finally:
        await conn.close()
    database, manager = await connect(state)
    try:
        await database.pool.execute(DDL)
        await manager.init_collection()
        files = build_files(args.state.parent / "fixtures")
        storage = HashStorage()
        storage._pg_client = database
        processor = DocumentProcessor(storage)
        processor._milvus_client = manager
        for doc in files:
            await database.pool.execute("INSERT INTO users(user_id) VALUES($1) ON CONFLICT DO NOTHING", doc["user_id"])
            await database.pool.execute("INSERT INTO knowledge_bases(knowledge_base_id,user_id) VALUES($1,$2) ON CONFLICT DO NOTHING",
                                         doc["knowledge_base_id"], doc["user_id"])
            old = next((d for d in state["documents"] if d["doc_id"] == doc["doc_id"]), None)
            if old and old["file_hash"] != doc["file_hash"]:
                raise ValueError("Fixture bytes changed; use a new state after cleanup")
            if not old:
                # Retry deletes only this fixture's partial writes. PG/Milvus are
                # not a shared transaction; state is READY only after both succeed.
                await manager.delete_file_chunks(doc["knowledge_base_id"], doc["file_hash"], doc["user_id"])
                await database.delete_file(doc["file_hash"], doc["knowledge_base_id"], doc["user_id"])
                temporary = args.state.parent / ("upload_" + Path(doc["path"]).name)
                shutil.copyfile(doc["path"], temporary)
                with patch("documents_process.get_postgresql_client", AsyncMock(return_value=database)):
                    await processor.process_document(temporary, Path(doc["path"]).name, doc["file_hash"],
                                                     doc["knowledge_base_id"], doc["user_id"])
                state["documents"].append({k: doc[k] for k in ("doc_id", "file_hash", "user_id", "knowledge_base_id")})
                save_state(args.state, state)
            rows = await database.pool.fetch(
                "SELECT parent_id,text FROM parent_chunks WHERE user_id=$1 AND knowledge_base_id=$2 AND file_hash=$3 ORDER BY parent_id",
                doc["user_id"], doc["knowledge_base_id"], doc["file_hash"])
            matching = [r["parent_id"] for r in rows if normalize(doc["text"]) in normalize(r["text"])]
            if len(matching) != 1:
                raise ValueError(f"Gold source {doc['doc_id']} must resolve to exactly one intact parent, got {len(matching)}")
            state["source_map"][doc["doc_id"]] = {"parent_id": matching[0], "user_id": doc["user_id"],
                "knowledge_base_id": doc["knowledge_base_id"], "file_hash": doc["file_hash"],
                "text_sha256": sha256(doc["text"].encode()).hexdigest()}
            save_state(args.state, state)
            print("Indexed", doc["doc_id"], flush=True)
        await manager.client.flush(collection_name=state["collection"])
        state["status"] = "ready"
        save_state(args.state, state)
    finally:
        await database.pool.close()
        await manager.client.close()


async def cleanup(args):
    import asyncpg
    from dotenv import load_dotenv
    from pymilvus import AsyncMilvusClient
    load_dotenv(ROOT / ".env")
    state = json.loads(args.state.read_text(encoding="utf-8"))
    validate_state(state)
    client = AsyncMilvusClient(uri=os.environ["Milvus_url"], token=os.getenv("Token", ""))
    try:
        if await client.has_collection(state["collection"]):
            await client.drop_collection(state["collection"])
    finally:
        await client.close()
    conn = await asyncpg.connect(os.environ["DATABASE_URL"], timeout=5)
    try:
        await conn.execute(f'DROP SCHEMA IF EXISTS "{state["schema"]}" CASCADE')
    finally:
        await conn.close()
    state["status"] = "cleaned"
    save_state(args.state, state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("import", "cleanup", "status"))
    parser.add_argument("--state", type=Path, default=Path(".runtime/rag_accuracy/corpus.json"))
    parser.add_argument("--execute", action="store_true", help="Import calls embedding APIs; cleanup deletes owned benchmark resources")
    args = parser.parse_args()
    if args.action == "status":
        print(args.state.read_text(encoding="utf-8") if args.state.exists() else "not imported")
    elif not args.execute:
        print("Dry run. Supply --execute to modify the isolated benchmark corpus.")
    else:
        asyncio.run(ingest(args) if args.action == "import" else cleanup(args))


if __name__ == "__main__":
    main()

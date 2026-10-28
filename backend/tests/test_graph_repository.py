import asyncio
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from graph.indexer import GraphIndexer
from graph.repository import GraphRepository
from graph.retriever import GraphRetriever
from graph.settings import GraphSettings
from rag.types import RetrievalCandidate, RetrievalScope
from test_graph import extraction


@unittest.skipUnless(os.getenv("ECHOMIND_TEST_DATABASE_URL"), "Set ECHOMIND_TEST_DATABASE_URL for isolated PostgreSQL graph checks")
class GraphRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import asyncpg
        from evaluation.rag_corpus import DDL
        from postgresql_client import PostgreSQLParentClient
        self.name = "graph_test_" + uuid4().hex
        self.admin = await asyncpg.connect(os.environ["ECHOMIND_TEST_DATABASE_URL"])
        await self.admin.execute(f'CREATE SCHEMA "{self.name}"')
        self.pool = await asyncpg.create_pool(os.environ["ECHOMIND_TEST_DATABASE_URL"], min_size=1, max_size=4,
                                             server_settings={"search_path": self.name})
        await self.pool.execute(DDL)
        self.repository = GraphRepository(self.pool)
        await self.repository.initialize()
        self.database = object.__new__(PostgreSQLParentClient)
        self.database.pool = self.pool
        self.settings = GraphSettings()

    async def asyncTearDown(self):
        await self.pool.close()
        await self.admin.execute(f'DROP SCHEMA "{self.name}" CASCADE')
        await self.admin.close()

    async def parent(self, text, *, kb="kb", user=1, filename=None):
        filename = filename or uuid4().hex
        file_hash = hashlib.sha256(filename.encode()).hexdigest()
        await self.pool.execute("INSERT INTO users(user_id) VALUES($1) ON CONFLICT DO NOTHING", user)
        await self.pool.execute("INSERT INTO knowledge_bases(knowledge_base_id,user_id) VALUES($1,$2) ON CONFLICT DO NOTHING", kb, user)
        await self.pool.execute("INSERT INTO file_metadata(file_hash,file_name,knowledge_base_id,user_id) VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING", file_hash, filename, kb, user)
        pid = uuid4().hex
        await self.pool.execute("INSERT INTO parent_chunks(parent_id,knowledge_base_id,user_id,text,file_name,file_hash) VALUES($1,$2,$3,$4,$5,$6)", pid, kb, user, text, filename, file_hash)
        return dict(parent_id=pid, file_hash=file_hash, user_id=user, knowledge_base_id=kb, text=text, file_name=filename)

    async def index(self, parent, raw, *, force=False, version="v2"):
        await self.repository.enqueue(parent["user_id"], parent["knowledge_base_id"], parent["file_hash"], version, force=force)
        job = await self.repository.claim(parent["user_id"], parent["knowledge_base_id"], version)
        if job:
            indexer = GraphIndexer(self.repository, replace(self.settings, version=version), SimpleNamespace(extract=AsyncMock(return_value=(raw, 0))))
            return await indexer.run_job(job)

    async def two_hop(self, user=1, kb="kb"):
        arch = await self.parent("订单系统使用赤霄缓存集群。", user=user, kb=kb)
        owner = await self.parent("赤霄缓存集群由基础设施甲组维护。", user=user, kb=kb)
        await self.index(arch, extraction())
        await self.index(owner, extraction("赤霄缓存集群", "基础设施甲组", "MAINTAINED_BY", owner["text"], "CacheCluster", "Team"))
        return arch, owner

    async def test_migration_is_idempotent_and_concurrent(self):
        await asyncio.gather(self.repository.initialize(), self.repository.initialize())
        self.assertEqual(await self.pool.fetchval("SELECT count(*) FROM kg_schema_migrations WHERE version='003'"), 1)

    async def test_query_seed_two_hops_direction_and_budget(self):
        arch, owner = await self.two_hop()
        result = await GraphRetriever(self.repository, self.settings).recall("订单系统由谁维护？", RetrievalScope(1, "kb"), [])
        self.assertEqual(result.seed_method, "query")
        self.assertEqual(set(result.paths[0].parent_ids), {arch["parent_id"], owner["parent_id"]})
        short = await GraphRetriever(self.repository, replace(self.settings, max_hops=1)).recall("订单系统由谁维护？", RetrievalScope(1, "kb"), [])
        self.assertEqual(short.paths, [])
        inverse = await GraphRetriever(self.repository, self.settings).recall("基础设施甲组使用什么？", RetrievalScope(1, "kb"), [])
        self.assertEqual(inverse.paths, [])

    async def test_tenant_default_scope_and_foreign_keys(self):
        await self.two_hop()
        await self.two_hop(user=2)
        await self.two_hop(kb="other")
        result = await GraphRetriever(self.repository, self.settings).recall("订单系统谁维护？", RetrievalScope(1, "默认知识库"), [])
        self.assertTrue(all(r["user_id"] == 1 for r in result.records))
        self.assertEqual({r["knowledge_base_id"] for r in result.records}, {"kb", "other"})
        import asyncpg
        foreign = await self.pool.fetchval("SELECT entity_id FROM kg_entities WHERE user_id=2 LIMIT 1")
        local = await self.pool.fetchval("SELECT entity_id FROM kg_entities WHERE user_id=1 AND knowledge_base_id='kb' LIMIT 1")
        with self.assertRaises(asyncpg.ForeignKeyViolationError):
            await self.pool.execute("INSERT INTO kg_relations VALUES($1,1,'kb',$2,'USES',$3,'v1')", uuid4(), foreign, local)

    async def test_shared_fact_survives_one_file_deletion_then_prunes(self):
        arch, owner = await self.two_hop()
        duplicate = await self.parent(owner["text"])
        await self.index(duplicate, extraction("赤霄缓存集群", "基础设施甲组", "MAINTAINED_BY", owner["text"], "CacheCluster", "Team"))
        self.assertEqual(await self.pool.fetchval("SELECT count(*) FROM kg_relations"), 2)
        await self.database.delete_file(owner["file_hash"], "kb", 1)
        result = await GraphRetriever(self.repository, self.settings).recall("订单系统谁维护？", RetrievalScope(1, "kb"), [])
        self.assertIn(duplicate["parent_id"], result.paths[0].parent_ids)
        self.assertNotIn(owner["parent_id"], result.paths[0].parent_ids)
        await self.database.delete_file(duplicate["file_hash"], "kb", 1)
        result = await GraphRetriever(self.repository, self.settings).recall("订单系统谁维护？", RetrievalScope(1, "kb"), [])
        self.assertEqual(result.paths, [])
        self.assertEqual(await self.pool.fetchval("SELECT count(*) FROM kg_relations"), 1)

    async def test_failed_extraction_retry_and_stale_lease(self):
        parent = await self.parent("订单系统使用赤霄缓存集群。")
        await self.repository.enqueue(1, "kb", parent["file_hash"], "v2")
        job = await self.repository.claim(1, "kb", "v2")
        bad = GraphIndexer(self.repository, self.settings, SimpleNamespace(extract=AsyncMock(side_effect=ValueError("invalid output"))))
        self.assertIsNone(await bad.run_job(job))
        self.assertEqual((await self.repository.status(1, "kb"))[0]["status"], "FAILED")
        self.assertEqual(await self.pool.fetchval("SELECT count(*) FROM kg_relations"), 0)
        await self.index(parent, extraction(), force=True)
        self.assertEqual((await self.repository.status(1, "kb"))[0]["status"], "READY")
        await self.repository.enqueue(1, "kb", parent["file_hash"], "v2", force=True)
        stale = await self.repository.claim(1, "kb", "v2")
        await self.pool.execute("UPDATE kg_index_jobs SET locked_until=now()-interval '1 second'")
        recovered = await self.repository.claim(1, "kb", "v2")
        self.assertNotEqual(stale["lease_id"], recovered["lease_id"])
        with self.assertRaises(RuntimeError):
            await self.repository.publish(stale, await self.repository.parents(stale), [(extraction(), 0)])

    async def test_vector_mention_fallback_and_unready_sources(self):
        arch, _ = await self.two_hop()
        candidate = RetrievalCandidate(**arch)
        result = await GraphRetriever(self.repository, self.settings).recall("这个平台谁维护？", RetrievalScope(1, "kb"), [candidate])
        self.assertEqual(result.seed_method, "vector_mentions")
        self.assertTrue(result.paths)
        await self.pool.execute("UPDATE kg_index_jobs SET status='STALE'")
        result = await GraphRetriever(self.repository, self.settings).recall("订单系统谁维护？", RetrievalScope(1, "kb"), [candidate])
        self.assertFalse(result.paths)

    async def test_delete_outbox_retries_and_blocks_recreation(self):
        arch, _ = await self.two_hop()
        await self.database.delete_knowledge_base("kb", 1)
        self.assertFalse((await self.database.create_knowledge_base("kb", 1))["success"])
        fake = SimpleNamespace(delete_knowledge_file_chunks=AsyncMock(side_effect=RuntimeError("offline")))
        self.assertEqual(await self.repository.drain_delete(fake), 0)
        self.assertEqual(await self.pool.fetchval("SELECT attempts FROM kb_delete_tasks"), 1)
        fake.delete_knowledge_file_chunks = AsyncMock(return_value=3)
        self.assertEqual(await self.repository.drain_delete(fake), 1)
        self.assertTrue((await self.database.create_knowledge_base("kb", 1))["success"])

    async def test_source_text_change_invalidates_online_path_even_before_reindex(self):
        arch, _ = await self.two_hop()
        await self.pool.execute("UPDATE parent_chunks SET text='原始关系已撤回' WHERE parent_id=$1", arch["parent_id"])
        result = await GraphRetriever(self.repository, self.settings).recall("订单系统谁维护？", RetrievalScope(1, "kb"), [])
        self.assertEqual(result.paths, [])

    async def test_backfill_resumes_after_ready_files_and_force_is_explicit(self):
        first = await self.parent("订单系统使用赤霄缓存集群。")
        await self.index(first, extraction())
        second = await self.parent("尚未索引的第二份文件。")
        remaining = await self.repository.backfill_files(1, "kb", "v2", 1)
        self.assertEqual([r["file_hash"] for r in remaining], [second["file_hash"]])
        all_files = await self.repository.backfill_files(1, "kb", "v2", 10, force=True)
        self.assertEqual({r["file_hash"] for r in all_files}, {first["file_hash"], second["file_hash"]})


if __name__ == "__main__":
    unittest.main()

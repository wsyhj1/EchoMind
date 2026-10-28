"""Real PostgreSQL/Milvus isolation and PDF/DOCX lifecycle, no cloud model calls."""
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class FixedEmbeddings:
    """Constant test vectors; assertions concern scope and lifecycle, not quality."""
    async def aembed_query(self, query):
        return [1.0] + [0.0] * 7

    async def aembed_documents(self, texts):
        return [[1.0] + [0.0] * 7 for _ in texts]


@unittest.skipUnless(os.getenv("ECHOMIND_TEST_DATABASE_URL") and os.getenv("ECHOMIND_TEST_MILVUS_URL"),
                     "Set ECHOMIND_TEST_DATABASE_URL and ECHOMIND_TEST_MILVUS_URL for isolated integration")
class RAGIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import asyncpg
        from pymilvus import AsyncMilvusClient
        from knowledeg_base_manager import KnowledgeBaseManager
        from postgresql_client import PostgreSQLParentClient
        from evaluation.rag_corpus import DDL
        self.name = "rag_accuracy_" + uuid4().hex[:12]
        self.admin = await asyncpg.connect(os.environ["ECHOMIND_TEST_DATABASE_URL"])
        await self.admin.execute(f'CREATE SCHEMA "{self.name}"')
        self.pool = await asyncpg.create_pool(os.environ["ECHOMIND_TEST_DATABASE_URL"], min_size=1, max_size=3,
                                             server_settings={"search_path": self.name})
        await self.pool.execute(DDL)
        self.database = object.__new__(PostgreSQLParentClient)
        self.database.pool = self.pool
        self.client = AsyncMilvusClient(uri=os.environ["ECHOMIND_TEST_MILVUS_URL"])
        self.manager = KnowledgeBaseManager(self.client, FixedEmbeddings(), self.name, 8)
        await self.manager.init_collection()
        self.temp = tempfile.TemporaryDirectory()

    async def asyncTearDown(self):
        self.temp.cleanup()
        await self.client.drop_collection(self.name)
        await self.client.close()
        await self.pool.close()
        await self.admin.execute(f'DROP SCHEMA "{self.name}" CASCADE')
        await self.admin.close()

    async def upload(self, filename, text, kb, user):
        from documents_process import DocumentProcessor
        from hash_storage import HashStorage
        from evaluation.rag_fixtures import write_docx
        import fitz
        path = Path(self.temp.name) / filename
        if path.suffix == ".docx":
            write_docx(path, text)
        else:
            pdf = fitz.open()
            pdf.new_page().insert_text((50, 50), text)
            pdf.save(path)
            pdf.close()
        file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        storage = HashStorage()
        storage._pg_client = self.database
        processor = DocumentProcessor(storage)
        processor._milvus_client = self.manager
        with patch("documents_process.get_postgresql_client", AsyncMock(return_value=self.database)):
            await processor.process_document(path, filename, file_hash, kb, user)
        return file_hash

    async def test_real_loaders_retrieve_delete_and_user_kb_scopes(self):
        from rag.retrieval_service import RetrievalService
        from rag.types import RetrievalOptions, RetrievalScope
        await self.upload("architecture.docx", "Alpha uses RedCache in production.", "primary", 1101)
        pdf_hash = await self.upload("ownership.pdf", "RedCache is maintained by the Platform team.", "primary", 1101)
        await self.upload("private.docx", "Alpha confidential FOREIGN_TENANT token.", "primary", 1102)
        await self.upload("other.docx", "Alpha separate OTHER_KB token.", "secondary", 1101)
        await self.client.flush(self.name)
        rerank = AsyncMock(return_value=None)
        service = RetrievalService(self.manager, self.database, rerank)
        options = RetrievalOptions(rerank_enabled=False)
        first = await service.retrieve_context("Alpha RedCache", RetrievalScope(1101, "primary"), 5, options)
        self.assertEqual(len(first.selected), 2)
        self.assertTrue(all(c.user_id == 1101 and c.knowledge_base_id == "primary" for c in first.selected))
        self.assertTrue(any(c.file_name.endswith(".pdf") for c in first.selected))
        self.assertTrue(any(c.file_name.endswith(".docx") for c in first.selected))
        across = await service.retrieve_context("Alpha", RetrievalScope(1101, "默认知识库"), 5, options)
        self.assertEqual({c.knowledge_base_id for c in across.selected}, {"primary", "secondary"})
        self.assertTrue(all(c.user_id == 1101 for c in across.selected))
        deleted_parent = next(c.parent_id for c in first.selected if c.file_hash == pdf_hash)
        await self.database.delete_file(pdf_hash, "primary", 1101)
        # Even if Milvus deletion lags, the missing SQL parent cannot enter context.
        stale = await service.retrieve_context("RedCache", RetrievalScope(1101, "primary"), 5, options)
        self.assertNotIn(deleted_parent, [c.parent_id for c in stale.selected])
        self.assertIn((1101, "primary", deleted_parent), stale.trace.missing_parent_keys)
        await self.manager.delete_file_chunks("primary", pdf_hash, 1101)
        await self.client.flush(self.name)
        after = await service.retrieve_context("RedCache", RetrievalScope(1101, "primary"), 5, options)
        self.assertEqual(len(after.selected), 1)
        await self.database.delete_knowledge_base("primary", 1101)
        await self.manager.delete_knowledge_file_chunks("primary", 1101)
        await self.client.flush(self.name)
        empty = await service.retrieve_context("Alpha", RetrievalScope(1101, "primary"), 5, options)
        self.assertEqual(empty.selected, [])
        self.assertEqual(await self.pool.fetchval("SELECT count(*) FROM parent_chunks WHERE user_id=1102"), 1)
        rerank.assert_not_awaited()

    async def test_sql_metadata_fetch_keeps_equal_parent_ids_in_two_kbs(self):
        for kb, user in (("first", 1101), ("second", 1101), ("first", 1102)):
            await self.upload(f"{kb}_{user}.docx", f"Scoped content {kb} {user}.", kb, user)
            await self.pool.execute("UPDATE parent_chunks SET parent_id='same-id' WHERE user_id=$1 AND knowledge_base_id=$2", user, kb)
        rows = await self.database.get_parents_with_metadata(["same-id"], "默认知识库", 1101)
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["knowledge_base_id"] for r in rows}, {"first", "second"})
        rows = await self.database.get_parents_with_metadata(["same-id"], "first", 1101)
        self.assertEqual([(r["knowledge_base_id"], r["user_id"]) for r in rows], [("first", 1101)])

    async def test_unicode_knowledge_base_scope_matches_live_milvus(self):
        from rag.retrieval_service import RetrievalService
        from rag.types import RetrievalOptions, RetrievalScope
        await self.upload("unicode.docx", "Alpha uses the dedicated cache.", "中文知识库", 1101)
        await self.client.flush(self.name)
        result = await RetrievalService(self.manager, self.database, AsyncMock()).retrieve_context(
            "Alpha", RetrievalScope(1101, "中文知识库"), 5, RetrievalOptions(rerank_enabled=False))
        self.assertEqual(len(result.selected), 1)
        self.assertEqual(result.selected[0].knowledge_base_id, "中文知识库")

    async def test_duplicate_content_keeps_both_sources_after_one_file_is_deleted(self):
        from rag.retrieval_service import RetrievalService
        from rag.types import RetrievalOptions, RetrievalScope
        text = "Alpha uses the same shared cache."
        first_hash = await self.upload("first.docx", text, "shared", 1101)
        second_hash = await self.upload("second.pdf", text, "shared", 1101)
        await self.client.flush(self.name)
        service = RetrievalService(self.manager, self.database, AsyncMock())
        options = RetrievalOptions(rerank_enabled=False, graph_enabled=False)
        before = await service.retrieve_context("Alpha", RetrievalScope(1101, "shared"), 5, options)
        self.assertEqual({c.file_hash for c in before.selected}, {first_hash, second_hash})
        await self.database.delete_file(first_hash, "shared", 1101)
        await self.manager.delete_file_chunks("shared", first_hash, 1101)
        await self.client.flush(self.name)
        after = await service.retrieve_context("Alpha", RetrievalScope(1101, "shared"), 5, options)
        self.assertEqual({c.file_hash for c in after.selected}, {second_hash})

    async def test_successful_upload_queues_graph_then_publishes_and_cleans_up(self):
        from graph.repository import GraphRepository
        from graph.indexer import GraphIndexer
        from graph.settings import GraphSettings
        from rag.retrieval_service import RetrievalService
        from rag.types import RetrievalOptions, RetrievalScope
        from test_graph import extraction
        from types import SimpleNamespace
        with patch.dict(os.environ, {"GRAPH_EXTRACTION_ENABLED": "true"}):
            file_hash = await self.upload("graph.docx", "Alpha uses RedCache.", "graph", 1101)
        await self.client.flush(self.name)
        repository = GraphRepository(self.pool)
        self.assertEqual((await repository.status(1101, "graph"))[0]["status"], "PENDING")
        raw = extraction("Alpha", "RedCache", quote="Alpha uses RedCache.")
        await GraphIndexer(repository, GraphSettings.from_env(), SimpleNamespace(extract=AsyncMock(return_value=(raw, 0)))).run_pending()
        service = RetrievalService(self.manager, self.database, AsyncMock())
        result = await service.retrieve_context("Alpha uses what?", RetrievalScope(1101, "graph"), 5,
                                               RetrievalOptions(rerank_enabled=False, graph_enabled=True))
        self.assertTrue(result.trace.graph_used, result.trace)
        self.assertEqual(result.selected[0].sources, ["rrf", "graph"])
        await self.database.delete_file(file_hash, "graph", 1101)
        self.assertEqual(await self.pool.fetchval("SELECT count(*) FROM kg_relation_evidence"), 0)
        self.assertEqual(await repository.drain_delete(self.manager), 1)
        self.assertEqual(await self.pool.fetchval("SELECT status FROM kb_delete_tasks"), "DONE")


if __name__ == "__main__":
    unittest.main()

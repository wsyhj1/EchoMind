"""Integration check against a disposable Milvus collection (no model API calls)."""
import os
from pathlib import Path
import sys
import unittest
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pymilvus import AsyncMilvusClient
from knowledeg_base_manager import KnowledgeBaseManager
from memory_manager import MemoryManager


@unittest.skipUnless(os.getenv("ECHOMIND_TEST_MILVUS_URL"), "Set ECHOMIND_TEST_MILVUS_URL to run this integration check")
class MilvusSchemaTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_collection_schemas_initialize_and_load(self):
        client = AsyncMilvusClient(uri=os.environ["ECHOMIND_TEST_MILVUS_URL"])
        names = []
        try:
            for manager_type in (KnowledgeBaseManager, MemoryManager):
                name = "echomind_test_" + uuid4().hex
                names.append(name)
                manager = manager_type(client, None, name, 1024)
                await manager.init_collection()
                self.assertTrue(await client.has_collection(name))
                self.assertEqual(int((await client.get_load_state(name))["state"]), 3)
        finally:
            for name in names:
                if await client.has_collection(name):
                    await client.drop_collection(name)
            await client.close()

"""User defaults survive new sessions and reach the retrieval tool; no model calls."""
import asyncio
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pydantic import ValidationError
from user_settings import SettingsValues, SettingsConflict, UserSettingsStore, public_runtime_settings


class SettingsValidationTests(unittest.TestCase):
    def test_bounded_values_and_no_arbitrary_server_settings(self):
        for invalid in ({"top_k": 0}, {"top_k": 21}, {"top_k": True}, {"top_k": 2.5},
                        {"retrieval_mode": "sql"}, {"rerank_enabled": "false"},
                        {"memory_enabled": None}, {"graph_enabled": 1}, {"api_key": "secret"}):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                SettingsValues.model_validate(invalid)
        self.assertIsNone(SettingsValues().graph_enabled)
        self.assertFalse(SettingsValues(graph_enabled=False).graph_enabled)

    def test_runtime_response_only_contains_public_allowlist(self):
        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "secret", "DATABASE_URL": "private",
                                     "BASE_URL": "private-endpoint", "GRAPH_RAG_ENABLED": "false"}):
            runtime = public_runtime_settings()
        self.assertEqual(set(runtime), {"agent_model", "embedding_model", "rerank_model",
                                       "graph_default_enabled", "graph_extraction_enabled", "evidence_default_enabled"})
        self.assertFalse(runtime["graph_default_enabled"])


class SettingsPropagationTests(unittest.IsolatedAsyncioTestCase):
    async def test_graph_option_reaches_retrieval_service(self):
        import tools
        from schemas import ContextSchema
        result = MagicMock()
        result.context_text.return_value = "evidence"
        service = MagicMock()
        service.retrieve_context = AsyncMock(return_value=result)
        context = ContextSchema(1, "kb", top_k=9, retrieval_mode="dense", rerank_enabled=False, graph_enabled=True, evidence_enabled=True)
        with patch.object(tools, "get_milvus_client", AsyncMock()), \
                patch.object(tools, "get_postgresql_client", AsyncMock()), \
                patch.object(tools, "get_stream_writer", return_value=MagicMock()), \
                patch.object(tools, "RetrievalService", return_value=service):
            self.assertEqual(await tools.search_knowledge_base.coroutine("q", SimpleNamespace(context=context)), "evidence")
        args = service.retrieve_context.call_args.args
        self.assertEqual(args[2], 9)
        self.assertEqual(args[3].retrieval_mode, "dense")
        self.assertFalse(args[3].rerank_enabled)
        self.assertTrue(args[3].graph_enabled)
        self.assertTrue(args[3].evidence_enabled)


@unittest.skipUnless(os.getenv("ECHOMIND_TEST_DATABASE_URL"), "Set ECHOMIND_TEST_DATABASE_URL for isolated PostgreSQL checks")
class UserSettingsStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import asyncpg
        self.schema = "settings_test_" + uuid4().hex
        self.admin = await asyncpg.connect(os.environ["ECHOMIND_TEST_DATABASE_URL"])
        await self.admin.execute(f'CREATE SCHEMA "{self.schema}"')
        self.pool = await asyncpg.create_pool(os.environ["ECHOMIND_TEST_DATABASE_URL"], min_size=1, max_size=3,
                                             server_settings={"search_path": self.schema})
        await self.pool.execute("""CREATE TABLE users(user_id INTEGER PRIMARY KEY,user_profile TEXT);
            INSERT INTO users VALUES(1,'已有画像'),(2,'另一用户');""")
        self.store = UserSettingsStore(self.pool)
        await self.store.initialize()

    async def asyncTearDown(self):
        await self.pool.close()
        await self.admin.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        await self.admin.close()

    async def test_migration_defaults_persistence_and_user_isolation(self):
        await self.store.initialize()
        original = await self.store.get(1)
        self.assertEqual(original["values"], SettingsValues().model_dump())
        self.assertIsNone(original["updated_at"])
        values = SettingsValues(top_k=8, retrieval_mode="dense", graph_enabled=False, memory_enabled=False)
        saved = await self.store.save(1, values, 1)
        self.assertEqual(saved["revision"], 2)
        self.assertIsNotNone(saved["updated_at"])
        self.assertEqual((await UserSettingsStore(self.pool).get(1))["values"], values.model_dump())
        self.assertEqual((await self.store.get(2))["values"], original["values"])
        self.assertEqual(await self.pool.fetchval("SELECT user_profile FROM users WHERE user_id=1"), "已有画像")

    async def test_concurrent_writes_reject_stale_revision(self):
        results = await asyncio.gather(self.store.save(1, SettingsValues(top_k=8), 1),
                                       self.store.save(1, SettingsValues(top_k=9), 1), return_exceptions=True)
        self.assertEqual(sum(isinstance(result, SettingsConflict) for result in results), 1)
        self.assertEqual((await self.store.get(1))["revision"], 2)

    async def test_api_validation_revision_conflict_and_missing_user(self):
        import httpx
        from fastapi import FastAPI
        import api
        app = FastAPI()
        app.include_router(api.router)
        app.state.user_settings_store = self.store
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/user-settings?user_id=1")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["defaults"], SettingsValues().model_dump())
            self.assertEqual((await client.get("/user-settings?user_id=0")).status_code, 422)
            self.assertEqual((await client.get("/user-settings?user_id=3")).status_code, 404)
            payload = {"values": SettingsValues(top_k=7).model_dump(), "expected_revision": 1}
            self.assertEqual((await client.put("/user-settings?user_id=1", json=payload)).status_code, 200)
            self.assertEqual((await client.put("/user-settings?user_id=1", json=payload)).status_code, 409)
            self.assertEqual((await client.put("/user-settings?user_id=3", json=payload)).status_code, 404)
            for invalid in ({"top_k": 50}, {"retrieval_mode": "unknown"}, {"api_key": "secret"}):
                payload["values"] = invalid
                self.assertEqual((await client.put("/user-settings?user_id=1", json=payload)).status_code, 422)

    async def test_chat_uses_saved_defaults_explicit_overrides_and_isolated_evaluation(self):
        import httpx
        from fastapi import FastAPI
        import api
        app = FastAPI()
        app.include_router(api.router)
        app.state.user_settings_store = self.store
        app.state.evaluation_store = SimpleNamespace()
        captured = []
        async def fake_stream(**kwargs):
            captured.append(kwargs)
            yield "done\n"
        await self.store.save(1, SettingsValues(top_k=8, rerank_enabled=False, graph_enabled=True, evidence_enabled=True,
                                               retrieval_mode="dense", memory_enabled=False), 1)
        params = {"user_id": 1, "query": "q", "knowledge_base_id": "kb"}
        with patch.object(api, "stream_agent_response", fake_stream):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                self.assertEqual((await client.get("/chat_with_agent/stream", params=params)).status_code, 200)
                self.assertEqual(captured[-1]["top_k"], 8)
                self.assertEqual(captured[-1]["retrieval_mode"], "dense")
                self.assertFalse(captured[-1]["rerank_enabled"])
                self.assertFalse(captured[-1]["memory_enabled"])
                self.assertTrue(captured[-1]["graph_enabled"])
                self.assertTrue(captured[-1]["evidence_enabled"])
                self.assertEqual(captured[-1]["trace"].config["settings_revision"], 2)
                await client.get("/chat_with_agent/stream", params={**params, "top_k": 2,
                    "rerank_enabled": True, "memory_enabled": True, "graph_enabled": False, "evidence_enabled": False})
                self.assertEqual(captured[-1]["top_k"], 2)
                self.assertTrue(captured[-1]["rerank_enabled"])
                self.assertTrue(captured[-1]["memory_enabled"])
                self.assertFalse(captured[-1]["graph_enabled"])
                self.assertFalse(captured[-1]["evidence_enabled"])
                await client.get("/chat_with_agent/stream", params={**params, "evaluation_mode": True})
                self.assertEqual(captured[-1]["top_k"], 5)
                self.assertEqual(captured[-1]["retrieval_mode"], "hybrid")
                self.assertTrue(captured[-1]["rerank_enabled"])
                self.assertFalse(captured[-1]["memory_enabled"])
                self.assertIsNone(captured[-1]["graph_enabled"])
                self.assertIsNone(captured[-1]["evidence_enabled"])
                self.assertIsNone(captured[-1]["trace"].config["settings_revision"])
                self.assertEqual((await client.get("/chat_with_agent/stream", params={**params, "user_id": 3})).status_code, 404)
                self.assertEqual((await client.get("/chat_with_agent/stream", params={**params, "top_k": 21})).status_code, 422)


if __name__ == "__main__":
    unittest.main()

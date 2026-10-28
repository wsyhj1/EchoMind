import asyncio
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from profile_store import ProfileStore, ProfileConflict, describe_personalization, effective_profile
from schemas import ContextSchema
from observability import RunTrace


class ProfilePromptTests(unittest.TestCase):
    def state(self):
        return {"fields": {"preferences": "先给结论"}, "automatic_summary": "喜欢长篇解释",
                "personalization_enabled": True, "revision": 4, "source": "manual"}

    def test_manual_preferences_replace_automatic_inferences(self):
        self.assertEqual(effective_profile(self.state()), "回答偏好：先给结论")

    def test_disabled_does_not_leak_text_into_prompt(self):
        from agent import build_system_prompt
        profile = self.state()
        profile["personalization_enabled"] = False
        snapshot = describe_personalization(profile)
        context = ContextSchema(1, "默认知识库", profile_snapshot=snapshot)
        trace = RunTrace(1, "unit", "q", "kb")
        with trace.activate():
            prompt = build_system_prompt(context)
        self.assertNotIn("先给结论", prompt)
        self.assertNotIn("喜欢长篇解释", prompt)
        self.assertFalse(snapshot["injected"])
        self.assertFalse(trace.contexts)

    def test_one_turn_uses_fixed_snapshot_and_records_injection_once(self):
        from agent import build_system_prompt
        snapshot = describe_personalization(self.state())
        context = ContextSchema(1, "默认知识库", profile_snapshot=snapshot)
        trace = RunTrace(1, "unit", "q", "kb")
        with trace.activate():
            first = build_system_prompt(context)
            second = build_system_prompt(context)
        self.assertEqual(first, second)
        self.assertEqual(len(trace.contexts), 1)
        self.assertEqual(trace.personalization["revision"], 4)
        self.assertTrue(trace.personalization["injected"])


class AutomaticProfileTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, auto_update_enabled):
        import auto_store_memory_from_psql as memory
        pg = SimpleNamespace(pool=object(), update_messages_with_summary_id=AsyncMock(return_value=True))
        milvus = SimpleNamespace(resolve_conflicts=AsyncMock(return_value={}), add_memories_batch=AsyncMock(return_value=True))
        store = SimpleNamespace(get=AsyncMock(return_value={"automatic_summary": "old", "revision": 1,
                                                            "auto_update_enabled": auto_update_enabled}),
                                save_automatic=AsyncMock(return_value=None), restore_automatic=AsyncMock())
        model = SimpleNamespace(ainvoke=AsyncMock(return_value=SimpleNamespace(content="new")))
        with patch.object(memory, "get_postgresql_client", AsyncMock(return_value=pg)), \
                patch.object(memory, "get_milvus_client", AsyncMock(return_value=milvus)), \
                patch.object(memory, "ProfileStore", return_value=store):
            result = await memory._store_memories([{"id": "1"}], {"user_profile": "new"}, 1, "unit", 1001, [], model)
        self.assertTrue(result["success"])
        milvus.add_memories_batch.assert_awaited_once()
        store.restore_automatic.assert_not_awaited()
        return store, model

    async def test_pausing_profile_updates_keeps_other_memory_processing(self):
        store, model = await self.exercise(False)
        model.ainvoke.assert_not_awaited()
        store.save_automatic.assert_not_awaited()

    async def test_disabling_while_model_is_running_skips_late_profile_without_failing_memory(self):
        store, model = await self.exercise(True)
        model.ainvoke.assert_awaited_once()
        store.save_automatic.assert_awaited_once_with(1, "new", 1)


@unittest.skipUnless(os.getenv("ECHOMIND_TEST_DATABASE_URL"), "Set ECHOMIND_TEST_DATABASE_URL for isolated database checks")
class ProfileStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import asyncpg
        self.schema = "profile_test_" + uuid4().hex
        self.admin = await asyncpg.connect(os.environ["ECHOMIND_TEST_DATABASE_URL"])
        await self.admin.execute(f'CREATE SCHEMA "{self.schema}"')
        self.pool = await asyncpg.create_pool(os.environ["ECHOMIND_TEST_DATABASE_URL"], min_size=1, max_size=3,
                                             server_settings={"search_path": self.schema})
        await self.pool.execute("""CREATE TABLE users(user_id INTEGER PRIMARY KEY,user_profile TEXT);
                                  INSERT INTO users VALUES(1,'旧版自动画像'),(2,'另一位用户');""")
        self.store = ProfileStore(self.pool)
        await self.store.initialize()
        self.invalidation = patch.object(ProfileStore, "_invalidate", new_callable=AsyncMock)
        self.invalidate = self.invalidation.start()

    async def asyncTearDown(self):
        self.invalidation.stop()
        await self.pool.close()
        await self.admin.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        await self.admin.close()

    async def test_migration_preserves_legacy_and_is_idempotent(self):
        await self.store.initialize()
        profile = await self.store.get(1)
        self.assertEqual(profile["automatic_summary"], "旧版自动画像")
        self.assertEqual(profile["source"], "automatic")
        self.assertIsNone(profile["updated_at"])

    async def test_manual_changes_are_not_overwritten_and_settings_are_independent(self):
        profile = await self.store.save_fields(1, {"preferences": "  简洁  "}, 1)
        self.assertEqual(profile["fields"]["preferences"], "简洁")
        new_revision = await self.store.save_automatic(1, "长篇解释", profile["revision"])
        current = await self.store.get(1)
        self.assertEqual(effective_profile(current), "回答偏好：简洁")
        disabled = await self.store.settings(1, new_revision, personalization_enabled=False)
        self.assertTrue(disabled["auto_update_enabled"])
        self.assertEqual(disabled["fields"]["preferences"], "简洁")
        self.assertEqual(describe_personalization(disabled)["profile_text"], "")

    async def test_concurrent_edits_allow_exactly_one_writer(self):
        results = await asyncio.gather(self.store.save_fields(1, {"goals": "a"}, 1),
                                       self.store.save_fields(1, {"goals": "b"}, 1), return_exceptions=True)
        self.assertEqual(sum(isinstance(r, ProfileConflict) for r in results), 1)

    async def test_clear_pauses_updates_and_rejects_inflight_results(self):
        cleared = await self.store.clear(1, 1)
        self.assertEqual(cleared["source"], "empty")
        self.assertFalse(cleared["auto_update_enabled"])
        self.assertIsNone(await self.store.save_automatic(1, "迟到结果", 1))
        self.assertIsNone(await self.store.save_automatic(1, "禁用期间", cleared["revision"]))
        self.invalidate.assert_awaited_once_with(1)
        self.assertEqual((await self.store.get(2))["automatic_summary"], "另一位用户")

    async def test_rollback_cannot_restore_over_a_newer_user_edit(self):
        revision = await self.store.save_automatic(1, "新摘要", 1)
        await self.store.save_fields(1, {"goals": "手动目标"}, revision)
        self.assertIsNone(await self.store.restore_automatic(1, "旧摘要", revision))
        self.assertEqual((await self.store.get(1))["fields"]["goals"], "手动目标")

    async def test_api_validation_user_scope_and_revision_conflicts(self):
        import httpx
        from fastapi import FastAPI
        import api
        app = FastAPI()
        app.include_router(api.router)
        app.state.profile_store = self.store
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            self.assertEqual((await client.get("/user-profile?user_id=3")).status_code, 404)
            self.assertEqual((await client.get("/user-profile?user_id=0")).status_code, 422)
            edited = await client.put("/user-profile?user_id=1", json={"fields": {"goals": "目标"}, "expected_revision": 1})
            self.assertEqual(edited.status_code, 200)
            self.assertEqual((await client.put("/user-profile?user_id=1", json={"fields": {}, "expected_revision": 1})).status_code, 409)
            self.assertEqual((await client.put("/user-profile?user_id=1", json={"fields": {"unknown": "x"}, "expected_revision": 2})).status_code, 422)
            self.assertEqual((await client.patch("/user-profile/settings?user_id=1", json={"expected_revision": 2})).status_code, 422)
            self.assertEqual((await client.get("/user-profile?user_id=2")).json()["automatic_summary"], "另一位用户")

    async def test_stream_returns_and_persists_exact_profile_snapshot(self):
        import agent
        class Checkpointer:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def asetup(self): pass
        class FakeAgent:
            async def astream(self, *args, context, **kwargs):
                agent.build_system_prompt(context)
                yield "messages", (SimpleNamespace(content="模拟回答"), {"langgraph_node": "model"})
        saved = SimpleNamespace(save_run=AsyncMock())
        trace = RunTrace(1, "test", "q", "默认知识库")
        with patch.object(agent.AsyncRedisSaver, "from_conn_string", return_value=Checkpointer()), \
                patch.object(agent, "get_postgresql_client", AsyncMock(return_value=SimpleNamespace(pool=self.pool))), \
                patch.object(agent, "get_or_create_agent", AsyncMock(return_value=FakeAgent())):
            events = [json.loads(line) async for line in agent.stream_agent_response("q", "默认知识库", 1, trace=trace, store=saved)]
        self.assertTrue(events[-2]["personalization"]["injected"])
        self.assertEqual(saved.save_run.call_args.args[0]["personalization"]["profile_text"], "旧版自动画像")
        await self.store.save_fields(1, {"preferences": "新的偏好"}, 1)
        self.assertEqual(trace.snapshot()["personalization"]["profile_text"], "旧版自动画像")


if __name__ == "__main__":
    unittest.main()

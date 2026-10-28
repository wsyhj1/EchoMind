"""Regression checks for bounded plans and resumable graph backfill."""
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import graph_cli
import test_graph_repository as fixtures


class GraphCLIBackfillTests(unittest.IsolatedAsyncioTestCase):
    async def test_claims_only_selected_files_and_stops_on_failure(self):
        repository = SimpleNamespace(enqueue=AsyncMock())
        indexer = SimpleNamespace(run_pending=AsyncMock(side_effect=[[{"parents": 1}], [None]]))
        args = SimpleNamespace(user_id=7, knowledge_base_id="chosen", force=False)
        settings = SimpleNamespace(version="v2")
        with patch("graph.indexer.GraphIndexer", return_value=indexer):
            results = await graph_cli.execute_backfill(repository, settings, args,
                [{"file_hash": "selected-a"}, {"file_hash": "selected-b"}, {"file_hash": "never-started"}])
        self.assertEqual(results, [{"parents": 1}, None])
        self.assertEqual(indexer.run_pending.await_args_list[0].args, (7, "chosen"))
        self.assertEqual([call.kwargs for call in indexer.run_pending.await_args_list], [
            {"limit": 1, "file_hash": "selected-a"}, {"limit": 1, "file_hash": "selected-b"}])
        self.assertEqual(repository.enqueue.await_count, 2)
        self.assertTrue(all(not call.kwargs["force"] for call in repository.enqueue.await_args_list))

    async def test_state_includes_unindexed_failed_and_other_pending_files(self):
        pool = SimpleNamespace(fetch=AsyncMock(return_value=[
            {"status": "READY", "count": 3}, {"status": "FAILED", "count": 1},
            {"status": "STALE", "count": 1}, {"status": "UNINDEXED", "count": 4},
            {"status": "RUNNING", "count": 1}]))
        args = SimpleNamespace(user_id=7, knowledge_base_id="chosen", file_hash="only-this-file")
        state = await graph_cli.backfill_state(pool, args, "v2")
        self.assertEqual(state["files"], 10)
        self.assertEqual(state["remaining"], 7)
        self.assertEqual(state["requires_force"], 2)
        self.assertFalse(state["complete"])
        self.assertEqual(pool.fetch.await_args.args[1:], (7, "chosen", "v2", "only-this-file"))

    async def test_missing_requested_file_is_not_reported_complete(self):
        args = SimpleNamespace(user_id=7, knowledge_base_id="chosen", file_hash="missing")
        state = await graph_cli.backfill_state(SimpleNamespace(fetch=AsyncMock(return_value=[])), args, "v2")
        self.assertTrue(state["requested_file_missing"])
        self.assertFalse(state["complete"])


@unittest.skipUnless(os.getenv("ECHOMIND_TEST_DATABASE_URL"), "Set ECHOMIND_TEST_DATABASE_URL for isolated graph checks")
class GraphBackfillRepositoryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.GraphRepositoryTests.asyncSetUp
    asyncTearDown = fixtures.GraphRepositoryTests.asyncTearDown
    parent = fixtures.GraphRepositoryTests.parent

    async def test_failed_and_stale_jobs_do_not_starve_later_unindexed_files(self):
        parents = sorted([await self.parent("plain text") for _ in range(3)], key=lambda p: p["file_hash"])
        for parent, status in zip(parents[:2], ["FAILED", "STALE"]):
            await self.repository.enqueue(1, "kb", parent["file_hash"], "v2")
            await self.pool.execute("UPDATE kg_index_jobs SET status=$2 WHERE file_hash=$1", parent["file_hash"], status)
        ordinary = await self.repository.backfill_files(1, "kb", "v2", 1)
        self.assertEqual([r["file_hash"] for r in ordinary], [parents[2]["file_hash"]])
        forced = await self.repository.backfill_files(1, "kb", "v2", 10, force=True)
        self.assertEqual({r["file_hash"] for r in forced}, {p["file_hash"] for p in parents})
        self.assertEqual([r["status"] for r in await self.repository.status(1, "kb")], ["FAILED", "STALE"])

    async def test_active_and_exhausted_leases_do_not_block_resumable_jobs(self):
        parents = sorted([await self.parent("plain text") for _ in range(3)], key=lambda p: p["file_hash"])
        for parent in parents:
            await self.repository.enqueue(1, "kb", parent["file_hash"], "v2")
            await self.repository.claim(1, "kb", "v2", parent["file_hash"])
        await self.pool.execute("UPDATE kg_index_jobs SET locked_until=now()-interval '1 second' WHERE file_hash=ANY($1::text[])",
                                [parents[1]["file_hash"], parents[2]["file_hash"]])
        await self.pool.execute("UPDATE kg_index_jobs SET retry_count=3 WHERE file_hash=$1", parents[1]["file_hash"])
        for force in (False, True):
            eligible = await self.repository.backfill_files(1, "kb", "v2", 1, force=force)
            self.assertEqual([r["file_hash"] for r in eligible], [parents[1]["file_hash"]])
        self.assertIsNone(await self.repository.claim(1, "kb", "v2", parents[1]["file_hash"]))
        eligible = await self.repository.backfill_files(1, "kb", "v2", 1)
        self.assertEqual([r["file_hash"] for r in eligible], [parents[2]["file_hash"]])

    async def test_force_recovers_exhausted_lease_without_touching_active_job(self):
        first, second = [await self.parent("plain text") for _ in range(2)]
        for parent in (first, second):
            await self.repository.enqueue(1, "kb", parent["file_hash"], "v2")
            await self.repository.claim(1, "kb", "v2", parent["file_hash"])
        await self.pool.execute("UPDATE kg_index_jobs SET retry_count=3,locked_until=now()-interval '1 second' WHERE file_hash=$1", first["file_hash"])
        indexer = SimpleNamespace(run_pending=AsyncMock(return_value=[]))
        args = SimpleNamespace(user_id=1, knowledge_base_id="kb", force=True)
        with patch("graph.indexer.GraphIndexer", return_value=indexer):
            await graph_cli.execute_backfill(self.repository, self.settings, args, [{"file_hash": first["file_hash"]}])
        states = {r["file_hash"]: r for r in await self.repository.status(1, "kb")}
        self.assertEqual(states[first["file_hash"]]["status"], "PENDING")
        self.assertEqual(states[first["file_hash"]]["retry_count"], 0)
        self.assertEqual(states[second["file_hash"]]["status"], "RUNNING")


if __name__ == "__main__":
    unittest.main()

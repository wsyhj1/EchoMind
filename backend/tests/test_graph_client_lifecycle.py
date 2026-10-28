"""Real indexer/extractor seam: blocking SDK setup must not trip cancellation."""
import asyncio
from dataclasses import replace
from pathlib import Path
import sys
from time import perf_counter
from time import sleep
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from threading import Event

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from graph.extractor import GraphExtractor
from graph.checkpoints import GraphJobCancelled
from graph.indexer import GraphIndexer
from graph.settings import GraphSettings


class GraphClientLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_construction_cannot_block_running_cancellation_check(self):
        watching = asyncio.Event()
        clients = []
        saved = []
        delays = []

        async def responsiveness():
            previous = perf_counter()
            while True:
                await asyncio.sleep(.01)
                now = perf_counter()
                delays.append(now - previous)
                previous = now

        class Store:
            async def active(self, job):
                if saved:
                    watching.set()
                    await asyncio.sleep(.02)

            async def load(self, job, parent):
                if parent['parent_id'] == 'second':
                    await watching.wait()
                return None

            async def reserve_call(self, job):
                pass

            async def save(self, job, parent, result):
                saved.append(parent['parent_id'])

        def factory(**kwargs):
            if clients:
                sleep(2.1)  # Windows certificate/SDK setup while watcher is active.
            client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
                create=AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(
                    message=SimpleNamespace(content='{"entities":[],"relations":[]}'))])))), close=AsyncMock())
            clients.append(client)
            return client

        repository = SimpleNamespace(pool=SimpleNamespace(execute=AsyncMock()),
            parents=AsyncMock(return_value=[{'parent_id': name, 'text': 'fixture'} for name in ('first', 'second')]),
            fail=AsyncMock(), publish=AsyncMock(return_value='published'), heartbeat=AsyncMock())
        settings = replace(GraphSettings(), batch_size=1)
        job = dict(generation=1, extraction_version=settings.version, parent_limit=200,
                   job_id='fixture', lease_id='fixture')
        ticker = asyncio.create_task(responsiveness())
        try:
            with patch('openai.AsyncOpenAI', side_effect=factory), patch('graph.indexer.CheckpointStore', return_value=Store()):
                result = await GraphIndexer(repository, settings, GraphExtractor(settings)).run_job(job)
        finally:
            ticker.cancel()
            await asyncio.gather(ticker, return_exceptions=True)
        self.assertLess(max(delays), .5, 'SDK initialization blocked cancellation and lease checks')
        self.assertEqual(result, 'published', repository.fail.call_args)
        self.assertEqual(saved, ['first', 'second'])
        for client in clients:
            client.close.assert_awaited_once()

    async def test_cancel_during_sdk_setup_closes_client_without_request_or_budget(self):
        entered, release = Event(), Event()
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock())), close=AsyncMock())
        def factory(**kwargs):
            entered.set()
            release.wait(3)
            return client
        hook = AsyncMock()
        with patch('openai.AsyncOpenAI', side_effect=factory):
            work = asyncio.create_task(GraphExtractor(GraphSettings(), before_request=hook).extract({'text': 'fixture'}))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                work.cancel()
                await asyncio.sleep(.01)
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await work
        client.close.assert_awaited_once()
        client.chat.completions.create.assert_not_awaited()
        hook.assert_not_awaited()

    async def test_indexer_cancel_keeps_sdk_cleanup_alive_until_client_is_closed(self):
        entered, release = Event(), Event()
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock())), close=AsyncMock())
        store = SimpleNamespace(load=AsyncMock(return_value=None), reserve_call=AsyncMock(),
                                save=AsyncMock(), finish_cancel=AsyncMock())
        checks = 0
        async def active(job):
            nonlocal checks
            checks += 1
            if checks > 1:
                self.assertTrue(entered.is_set())
                raise GraphJobCancelled()
        store.active = active
        def factory(**kwargs):
            entered.set()
            release.wait(3)
            return client
        async def release_constructor():
            await asyncio.to_thread(entered.wait, 2)
            await asyncio.sleep(.4)
            release.set()
        repository = SimpleNamespace(pool=SimpleNamespace(execute=AsyncMock()),
            parents=AsyncMock(return_value=[{'text': 'fixture'}]),
            fail=AsyncMock(), publish=AsyncMock(), heartbeat=AsyncMock())
        settings = GraphSettings()
        job = dict(generation=1, extraction_version=settings.version, parent_limit=200,
                   job_id='fixture', lease_id='fixture')
        releaser = asyncio.create_task(release_constructor())
        try:
            with patch('openai.AsyncOpenAI', side_effect=factory), patch('graph.indexer.CheckpointStore', return_value=store):
                result = await GraphIndexer(repository, settings, GraphExtractor(settings)).run_job(job)
            self.assertIsNone(result)
            client.close.assert_awaited_once()
            client.chat.completions.create.assert_not_awaited()
            store.reserve_call.assert_not_awaited()
            store.finish_cancel.assert_awaited_once()
        finally:
            release.set()
            await releaser


if __name__ == '__main__':
    unittest.main()

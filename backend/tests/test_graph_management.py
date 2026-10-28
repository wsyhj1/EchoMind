"""Management consent, durable resume, budgets and cancellation; model calls are mocks."""
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from graph.checkpoints import CheckpointStore, GraphLeaseLost, GraphJobCancelled, GraphSourceChanged, text_hash, service_config, management_exists
from graph.extractor import GraphExtractor
from graph.indexer import GraphIndexer
from graph.management import GraphManagement, GraphManagementError
from test_graph import extraction
import test_graph_repository as fixtures


def model_response(raw=None):
    raw = raw if raw is not None else extraction().model_dump()
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(raw)))])


def extractor_with(create, settings):
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return GraphExtractor(settings, client=client)


@unittest.skipUnless(os.getenv('ECHOMIND_TEST_DATABASE_URL'), 'Set ECHOMIND_TEST_DATABASE_URL for isolated graph checks')
class GraphManagementTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = fixtures.GraphRepositoryTests.asyncTearDown
    parent = fixtures.GraphRepositoryTests.parent

    async def asyncSetUp(self):
        await fixtures.GraphRepositoryTests.asyncSetUp(self)
        self.settings = replace(self.settings, version='v3', batch_size=1)
        self.management = GraphManagement(self.pool, self.settings)
        self.config_patch = patch.dict(os.environ, {'BASE_URL': 'https://models.example.invalid/v1',
            'GRAPH_EXTRACTION_VERSION': 'v3', 'GRAPH_JOB_MAX_PARENTS': '200', 'GRAPH_JOB_MAX_CALLS': '400'})
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)

    async def job(self, file_hash):
        return dict(await self.pool.fetchrow("SELECT * FROM kg_index_jobs WHERE user_id=1 AND knowledge_base_id='kb' AND file_hash=$1 AND extraction_version='v3'", file_hash))

    async def submit(self, user, kb, file_hash, **kwargs):
        kwargs.setdefault('consent_target', service_config()[1])
        return await self.management.submit(user, kb, file_hash, **kwargs)

    async def fail_after_first_checkpoint(self):
        await self.parent('订单系统使用赤霄缓存集群。', filename='two-parents')
        parent = await self.parent('订单系统使用赤霄缓存集群。', filename='two-parents')
        await self.submit(1, 'kb', parent['file_hash'], consent=True)
        create = AsyncMock(side_effect=[model_response(), RuntimeError('mock outage'), RuntimeError('mock outage')])
        result = await GraphIndexer(self.repository, self.settings, extractor_with(create, self.settings)).run_pending(1, 'kb', limit=1, file_hash=parent['file_hash'], explicit_only=True)
        self.assertEqual(result, [None])
        self.assertEqual(create.await_count, 3)
        self.assertEqual(await self.pool.fetchval('SELECT count(*) FROM kg_parent_checkpoints'), 1)
        self.assertEqual((await self.job(parent['file_hash']))['model_calls'], 3)
        return parent

    async def test_consent_scope_and_pending_auto_promotion(self):
        parent = await self.parent('订单系统使用赤霄缓存集群。')
        for user, kb, consent, code in [(1, 'kb', False, 'ExternalModelConsentRequired'),
                                         (2, 'kb', True, 'KnowledgeBaseNotFound'),
                                         (1, '默认知识库', True, 'InvalidScope')]:
            with self.assertRaises(GraphManagementError) as error:
                await self.submit(user, kb, parent['file_hash'], consent=consent)
            self.assertEqual(error.exception.code, code)
        self.assertEqual(await self.pool.fetchval('SELECT count(*) FROM kg_index_jobs'), 0)
        await self.repository.enqueue(1, 'kb', parent['file_hash'], 'v3')
        first = await self.submit(1, 'kb', parent['file_hash'], consent=True)
        again = await self.submit(1, 'kb', parent['file_hash'], consent=True)
        self.assertEqual(first['job_id'], again['job_id'])
        self.assertTrue(again['idempotent'])
        self.assertTrue((await self.job(parent['file_hash']))['explicit_requested'])

    async def test_persistent_resume_reuses_only_successful_parent_and_keeps_call_count(self):
        parent = await self.fail_after_first_checkpoint()
        await self.submit(1, 'kb', parent['file_hash'], consent=True)
        self.assertEqual((await self.job(parent['file_hash']))['model_calls'], 3)
        create = AsyncMock(return_value=model_response())
        result = await GraphIndexer(self.repository, self.settings, extractor_with(create, self.settings)).run_pending(1, 'kb', limit=1, file_hash=parent['file_hash'], explicit_only=True)
        self.assertIsNotNone(result[0])
        self.assertEqual(create.await_count, 1)
        job = await self.job(parent['file_hash'])
        self.assertEqual((job['status'], job['parents_completed'], job['model_calls'], job['lifetime_model_calls']), ('READY', 2, 4, 4))
        status = await self.management.status(1, 'kb')
        document = status['documents'][0]
        self.assertEqual((document['raw_relations'], document['usable_relations'], document['parents_completed']), (1, 1, 2))
        self.assertTrue(document['counts_complete'])
        self.assertTrue(document['progress_verified'])

    async def test_changed_source_checkpoint_is_not_reused(self):
        parent = await self.fail_after_first_checkpoint()
        cached_id = await self.pool.fetchval('SELECT parent_id FROM kg_parent_checkpoints')
        new_text = '订单系统使用星云缓存集群。'
        await self.pool.execute('UPDATE parent_chunks SET text=$2 WHERE parent_id=$1', cached_id, new_text)
        await self.submit(1, 'kb', parent['file_hash'], consent=True)
        async def create(**kwargs):
            source = json.loads(kwargs['messages'][1]['content'])['document_text']
            return model_response(extraction(target='星云缓存集群').model_dump() if '星云' in source else None)
        mocked = AsyncMock(side_effect=create)
        await GraphIndexer(self.repository, self.settings, extractor_with(mocked, self.settings)).run_pending(1, 'kb', limit=1, file_hash=parent['file_hash'], explicit_only=True)
        self.assertEqual(mocked.await_count, 2)
        self.assertEqual(await self.pool.fetchval('SELECT source_sha256 FROM kg_parent_checkpoints WHERE parent_id=$1', cached_id), text_hash(new_text))

    async def test_ready_rebuild_requires_force_and_preserves_lifetime_calls(self):
        parent = await self.parent('订单系统使用赤霄缓存集群。')
        await self.submit(1, 'kb', parent['file_hash'], consent=True)
        create = AsyncMock(return_value=model_response())
        await GraphIndexer(self.repository, self.settings, extractor_with(create, self.settings)).run_pending(1, 'kb', limit=1)
        with self.assertRaises(GraphManagementError) as error:
            await self.submit(1, 'kb', parent['file_hash'], consent=True)
        self.assertEqual(error.exception.code, 'ReadyRequiresForce')
        await self.submit(1, 'kb', parent['file_hash'], consent=True, force=True)
        job = await self.job(parent['file_hash'])
        self.assertEqual((job['generation'], job['model_calls'], job['lifetime_model_calls']), (2, 0, 1))
        self.assertEqual(await self.pool.fetchval('SELECT count(*) FROM kg_parent_checkpoints'), 0)

    async def test_model_call_budget_counts_schema_retry_and_cannot_be_reset_by_failed_retry(self):
        parent = await self.parent('订单系统使用赤霄缓存集群。')
        await self.submit(1, 'kb', parent['file_hash'], consent=True)
        await self.pool.execute('UPDATE kg_index_jobs SET call_limit=1')
        create = AsyncMock(return_value=model_response({'entities': [], 'relations': [], '$defs': {}}))
        result = await GraphIndexer(self.repository, self.settings, extractor_with(create, self.settings)).run_pending(1, 'kb', limit=1)
        self.assertEqual(result, [None])
        self.assertEqual(create.await_count, 1)
        self.assertEqual((await self.job(parent['file_hash']))['last_error'], 'GraphCallLimitExceeded')
        for force in (False, True):
            with self.assertRaises(GraphManagementError) as error:
                await self.submit(1, 'kb', parent['file_hash'], consent=True, force=force)
            self.assertEqual(error.exception.code, 'GraphCallLimitExceeded')

    async def test_parent_budget_preflight_and_runtime_block_without_cloud(self):
        await self.parent('订单系统使用赤霄缓存集群。', filename='too-many')
        parent = await self.parent('订单系统使用赤霄缓存集群。', filename='too-many')
        with patch.dict(os.environ, {'GRAPH_JOB_MAX_PARENTS': '1'}):
            with self.assertRaises(GraphManagementError) as error:
                await self.submit(1, 'kb', parent['file_hash'], consent=True)
            self.assertEqual(error.exception.code, 'GraphParentLimitExceeded')
            await self.repository.enqueue(1, 'kb', parent['file_hash'], 'v3')
        create = AsyncMock(return_value=model_response())
        result = await GraphIndexer(self.repository, self.settings, extractor_with(create, self.settings)).run_pending(1, 'kb', limit=1)
        self.assertEqual(result, [None])
        create.assert_not_awaited()
        self.assertEqual((await self.job(parent['file_hash']))['last_error'], 'GraphParentLimitExceeded')

    async def test_cancel_stops_inflight_task_and_does_not_start_next_parent(self):
        await self.parent('订单系统使用赤霄缓存集群。', filename='cancel')
        parent = await self.parent('订单系统使用赤霄缓存集群。', filename='cancel')
        await self.submit(1, 'kb', parent['file_hash'], consent=True)
        entered = asyncio.Event()
        async def create(**kwargs):
            entered.set()
            await asyncio.Event().wait()
        mocked = AsyncMock(side_effect=create)
        task = asyncio.create_task(GraphIndexer(self.repository, self.settings, extractor_with(mocked, self.settings)).run_pending(1, 'kb', limit=1))
        await asyncio.wait_for(entered.wait(), timeout=3)
        cancel = await self.management.cancel(1, 'kb', parent['file_hash'])
        self.assertTrue(cancel['cancel_requested'])
        self.assertEqual(await asyncio.wait_for(task, timeout=3), [None])
        self.assertEqual(mocked.await_count, 1)
        self.assertEqual((await self.job(parent['file_hash']))['status'], 'STALE')

    async def test_expired_lease_cannot_write_or_reserve_and_checkpoint_adopts_new_lease(self):
        parent = await self.parent('订单系统使用赤霄缓存集群。')
        await self.submit(1, 'kb', parent['file_hash'], consent=True)
        old = await self.repository.claim(1, 'kb', 'v3')
        store = CheckpointStore(self.pool)
        await store.save(old, parent, (extraction(), 0))
        await self.pool.execute("UPDATE kg_index_jobs SET locked_until=now()-interval '1 second'")
        current = await self.repository.claim(1, 'kb', 'v3')
        with self.assertRaises(GraphLeaseLost):
            await store.reserve_call(old)
        with self.assertRaises(GraphLeaseLost):
            await store.save(old, parent, (extraction(), 0))
        self.assertIsNotNone(await store.load(current, parent))
        self.assertEqual(await self.pool.fetchval('SELECT lease_id FROM kg_parent_checkpoints'), current['lease_id'])

    async def test_consent_is_bound_to_model_service_configuration(self):
        parent = await self.parent('订单系统使用赤霄缓存集群。')
        await self.submit(1, 'kb', parent['file_hash'], consent=True)
        create = AsyncMock(return_value=model_response())
        with patch.dict(os.environ, {'BASE_URL': 'https://different.example.invalid/v1'}):
            await GraphIndexer(self.repository, self.settings, extractor_with(create, self.settings)).run_pending(1, 'kb', limit=1)
        create.assert_not_awaited()
        self.assertEqual((await self.job(parent['file_hash']))['last_error'], 'ConsentTargetChanged')

    async def test_old_displayed_target_is_rejected_before_enqueue_and_audit_records_target(self):
        parent = await self.parent('订单系统使用赤霄缓存集群。')
        original = service_config()[1]
        with patch.dict(os.environ, {'BASE_URL': 'https://models.example.invalid/changed-path'}):
            with self.assertRaises(GraphManagementError) as error:
                await self.management.submit(1, 'kb', parent['file_hash'], consent=True, consent_target=original)
            self.assertEqual(error.exception.code, 'ConsentTargetChanged')
        with self.assertRaises(GraphManagementError):
            await self.management.submit(1, 'kb', parent['file_hash'], consent=True)
        self.assertEqual(await self.pool.fetchval('SELECT count(*) FROM kg_index_jobs'), 0)
        await self.submit(1, 'kb', parent['file_hash'], consent=True)
        job = await self.job(parent['file_hash'])
        self.assertEqual(job['consent_host'], 'models.example.invalid')
        self.assertEqual(job['consent_model'], service_config()[0]['model'])
        self.assertEqual(job['consent_config_sha256'], original)

    async def test_worker_global_off_processes_only_explicit_queue(self):
        from graph import worker
        auto = await self.parent('订单系统使用赤霄缓存集群。', filename='automatic')
        manual = await self.parent('订单系统使用赤霄缓存集群。', filename='manual')
        await self.repository.enqueue(1, 'kb', auto['file_hash'], 'v3')
        await self.submit(1, 'kb', manual['file_hash'], consent=True)
        completed = asyncio.Event()
        create = AsyncMock(return_value=model_response())
        def factory(repository, settings):
            async def pending(*args, **kwargs):
                result = await GraphIndexer(repository, settings, extractor_with(create, settings)).run_pending(*args, **kwargs)
                if result:
                    completed.set()
                return result
            return SimpleNamespace(run_pending=pending)
        with patch.dict(os.environ, {'GRAPH_EXTRACTION_ENABLED': 'false'}), patch.object(worker, 'GraphIndexer', side_effect=factory):
            task = asyncio.create_task(worker.run_worker(SimpleNamespace(pool=self.pool), SimpleNamespace()))
            try:
                await asyncio.wait_for(completed.wait(), timeout=4)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual((await self.job(manual['file_hash']))['status'], 'READY')
        self.assertEqual((await self.job(auto['file_hash']))['status'], 'PENDING')
        self.assertEqual(create.await_count, 1)

    async def test_checkpoint_cannot_cross_version_scope_or_generation(self):
        parent = await self.parent('订单系统使用赤霄缓存集群。')
        await self.repository.enqueue(1, 'kb', parent['file_hash'], 'v2')
        old = await self.repository.claim(1, 'kb', 'v2')
        store = CheckpointStore(self.pool)
        await store.save(old, parent, (extraction(), 0))
        await self.repository.publish(old, [parent], [(extraction(), 0)])
        await self.submit(1, 'kb', parent['file_hash'], consent=True, force=True)
        current = await self.repository.claim(1, 'kb', 'v3')
        self.assertIsNone(await store.load(current, parent))
        foreign = await self.parent('订单系统使用赤霄缓存集群。', kb='other')
        with self.assertRaises(GraphSourceChanged):
            await store.load(current, foreign)
        with self.assertRaises(GraphLeaseLost):
            await store.reserve_call({**current, 'generation': current['generation'] + 1})
        self.assertEqual((await self.job(parent['file_hash']))['model_calls'], 0)

    async def test_cancelled_pending_job_never_claims_and_requires_new_consent_to_resume(self):
        parent = await self.parent('订单系统使用赤霄缓存集群。')
        await self.submit(1, 'kb', parent['file_hash'], consent=True)
        await self.management.cancel(1, 'kb', parent['file_hash'])
        self.assertIsNone(await self.repository.claim(1, 'kb', 'v3', explicit_only=True))
        with self.assertRaises(GraphManagementError):
            await self.submit(1, 'kb', parent['file_hash'], consent=False)
        await self.submit(1, 'kb', parent['file_hash'], consent=True)
        current = await self.repository.claim(1, 'kb', 'v3', explicit_only=True)
        await self.management.cancel(1, 'kb', parent['file_hash'])
        with self.assertRaises(GraphJobCancelled):
            await CheckpointStore(self.pool).reserve_call(current)
        self.assertEqual((await self.job(parent['file_hash']))['model_calls'], 0)

    async def test_old_003_only_repository_remains_usable_without_management_table(self):
        import asyncpg
        from evaluation.rag_corpus import DDL
        from graph.repository import GraphRepository
        name = self.name + '_legacy'
        await self.admin.execute(f'CREATE SCHEMA "{name}"')
        pool = await asyncpg.create_pool(os.environ['ECHOMIND_TEST_DATABASE_URL'], min_size=1, max_size=2,
                                        server_settings={'search_path': name})
        try:
            await pool.execute(DDL)
            await pool.execute((Path(__file__).resolve().parents[1] / 'migrations/003_knowledge_graph.sql').read_text(encoding='utf-8'))
            repository = GraphRepository(pool)
            parent = await fixtures.GraphRepositoryTests.parent(SimpleNamespace(pool=pool), '订单系统使用赤霄缓存集群。')
            await repository.enqueue(1, 'kb', parent['file_hash'], 'v2')
            self.assertIsNone(await repository.claim(1, 'kb', 'v2', explicit_only=True))
            job = await repository.claim(1, 'kb', 'v2')
            self.assertNotIn('generation', job)
            extractor = SimpleNamespace(extract=AsyncMock(return_value=(extraction(), 0)))
            self.assertIsNotNone(await GraphIndexer(repository, replace(self.settings, version='v2'), extractor).run_job(job))
            self.assertFalse(await management_exists(pool))
            self.assertEqual((await repository.status(1, 'kb'))[0]['status'], 'READY')
        finally:
            await pool.close()
            await self.admin.execute(f'DROP SCHEMA "{name}" CASCADE')


class GraphManagementAPITests(unittest.TestCase):
    def test_service_config_is_an_allowlist_and_hides_url_credentials(self):
        with patch.dict(os.environ, {'BASE_URL': 'https://user:PRIVATE_PASSWORD@models.example.invalid/private?token=PRIVATE_TOKEN'}):
            display, _ = service_config()
        self.assertEqual(set(display), {'model', 'host', 'target_id'})
        self.assertEqual(display['host'], 'models.example.invalid')
        self.assertNotIn('PRIVATE_', json.dumps(display))

    def client(self, *, with_identity=True):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import graph_api
        from auth import Identity
        app = FastAPI()
        app.state.postgresql_client = SimpleNamespace(pool=object())
        if with_identity:
            @app.middleware('http')
            async def trusted_test_identity(request, call_next):
                request.state.identity = Identity(1, 'test', 'local')
                return await call_next(request)
        app.include_router(graph_api.router)
        return TestClient(app)

    def test_api_rejects_missing_identity_forged_body_and_nonboolean_consent(self):
        body = dict(user_id=1, knowledge_base_id='kb', file_hash='a'*64, external_model_consent=True, consent_target='b'*64)
        with self.client(with_identity=False) as client:
            self.assertEqual(client.post('/graph-jobs', json=body).status_code, 401)
        with self.client() as client:
            self.assertEqual(client.post('/graph-jobs', json={**body, 'user_id': 2}).status_code, 403)
            self.assertEqual(client.post('/graph-jobs', json={**body, 'external_model_consent': 'true'}).status_code, 422)


if __name__ == '__main__':
    unittest.main()

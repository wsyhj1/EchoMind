import asyncio
import logging

from graph.extractor import GraphExtractor
from graph.checkpoints import CheckpointStore, GraphJobCancelled, GraphParentLimitExceeded, GraphExtractionVersionMismatch

logger = logging.getLogger(__name__)


class GraphCancellationCheckTimeout(TimeoutError):
    """The database cancellation/lease check timed out, not the model call."""


class GraphIndexer:
    def __init__(self, repository, settings, extractor=None):
        self.repository, self.settings = repository, settings
        self.injected_extractor = extractor is not None
        self.extractor = extractor or GraphExtractor(settings)

    async def run_job(self, job):
        async def heartbeat():
            while True:
                await asyncio.sleep(30)
                await self.repository.heartbeat(job)
        keeper = asyncio.create_task(heartbeat())
        store = CheckpointStore(self.repository.pool) if 'generation' in job else None
        old_hook = getattr(self.extractor, 'before_request', None)
        work = watcher = None
        try:
            parents = await self.repository.parents(job)
            if store:
                if job['extraction_version'] != self.settings.version:
                    raise GraphExtractionVersionMismatch()
                await store.active(job)
                if len(parents) > job['parent_limit']:
                    raise GraphParentLimitExceeded()
                if not self.injected_extractor and not hasattr(self.extractor, 'before_request'):
                    raise RuntimeError('GraphExtractorBudgetHookUnavailable')
                async def before_request():
                    await store.reserve_call(job)
                    if old_hook:
                        await old_hook()
                self.extractor.before_request = before_request
                await self.repository.pool.execute('UPDATE kg_index_jobs SET parents_total=$3 WHERE job_id=$1 AND lease_id=$2',
                                                   job['job_id'], job['lease_id'], len(parents))

            async def extract_parent(parent):
                cached = await store.load(job, parent) if store else None
                if cached is not None:
                    return cached
                result = await self.extractor.extract(parent)
                if store:
                    await store.save(job, parent, result)
                return result

            async def extract_all():
                extracted = []
                for start in range(0, len(parents), self.settings.batch_size):
                    tasks = [asyncio.create_task(extract_parent(p)) for p in parents[start:start+self.settings.batch_size]]
                    try:
                        extracted.extend(await asyncio.gather(*tasks))
                    except BaseException:
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                        raise
                return await self.repository.publish(job, parents, extracted)

            work = asyncio.create_task(extract_all())
            if store:
                async def watch_cancel():
                    while True:
                        await asyncio.sleep(.2)
                        try:
                            await asyncio.wait_for(store.active(job), timeout=2)
                        except asyncio.TimeoutError as error:
                            raise GraphCancellationCheckTimeout() from error
                watcher = asyncio.create_task(watch_cancel())
                done, _ = await asyncio.wait((work, watcher), return_when=asyncio.FIRST_COMPLETED)
                if watcher in done and work not in done:
                    await watcher  # raise its cancel/lease/config failure
            return await work
        except GraphJobCancelled:
            if work:
                work.cancel()
            if store:
                try:
                    await asyncio.wait_for(store.finish_cancel(job), timeout=2)
                except asyncio.TimeoutError:
                    pass  # Persistent cancel flag still prevents new requests.
            return None
        except asyncio.CancelledError:
            # Leave RUNNING with a lease; crash/restart recovery retries it.
            raise
        except Exception as error:
            logger.warning("graph_job_failed job_id=%s error_type=%s", job.get('job_id'), type(error).__name__)
            if work:
                work.cancel()
            try:
                await asyncio.wait_for(self.repository.fail(job, type(error).__name__), timeout=2)
            except asyncio.TimeoutError:
                pass  # The stopped heartbeat leaves bounded lease recovery.
            return None
        finally:
            for task in (work, watcher):
                # Do not interrupt an already-running cancellation cleanup.
                # SDK setup in a thread must finish and close its client.
                if task and not task.done() and not task.cancelling():
                    task.cancel()
            await asyncio.gather(*(task for task in (work, watcher) if task), return_exceptions=True)
            if store:
                self.extractor.before_request = old_hook
            keeper.cancel()
            await asyncio.gather(keeper, return_exceptions=True)

    async def run_pending(self, user_id=None, kb=None, limit=10, file_hash=None, *, explicit_only=False, auto_only=False):
        results = []
        for _ in range(limit):
            kwargs = {'explicit_only': explicit_only, 'auto_only': auto_only} if explicit_only or auto_only else {}
            job = await self.repository.claim(user_id, kb, self.settings.version, file_hash, **kwargs)
            if not job:
                break
            result = await self.run_job(job)
            results.append(result)
            if result is None:
                break  # stop this batch; do not multiply an unavailable model's failures
        return results

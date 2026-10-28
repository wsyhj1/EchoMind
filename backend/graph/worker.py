import asyncio
import logging

from graph.indexer import GraphIndexer
from graph.repository import GraphRepository, graph_exists
from graph.settings import GraphSettings
from rag.settings import env_bool

logger = logging.getLogger(__name__)


async def run_worker(database, milvus):
    repository = GraphRepository(database.pool)
    while True:
        delay = 3
        try:
            if not await graph_exists(database.pool):
                await asyncio.sleep(delay)
                continue
            # Compensation keeps running even after disabling graph extraction.
            await repository.drain_delete(milvus, limit=5)
            results = await GraphIndexer(repository, GraphSettings.from_env()).run_pending(limit=1, explicit_only=True)
            if not results and env_bool("GRAPH_EXTRACTION_ENABLED"):
                results = await GraphIndexer(repository, GraphSettings.from_env()).run_pending(limit=1, auto_only=True)
            if results:
                if any(result is None for result in results):
                    delay = 30
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning("graph_worker_failed error_type=%s", type(error).__name__)
        await asyncio.sleep(delay)

"""Run with .venv-eval: python -m evaluation.worker [--once]."""
import argparse
import asyncio
import logging
import os
from pathlib import Path
import sys

import asyncpg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from evaluation_store import EvaluationStore, decode
from evaluation.engine import RagasEvaluator, clean_error


async def process_job(store, job):
    async def heartbeat():
        while True:
            await asyncio.sleep(30)
            await store.heartbeat(job)

    keeper = asyncio.create_task(heartbeat())
    try:
        run = await store.get_run(job["run_id"], job["user_id"])
        payload = decode(job["payload"])
        evaluator = RagasEvaluator(payload["config"], os.getenv("EVAL_API_KEY") or os.getenv("DASHSCOPE_API_KEY"))
        result = await evaluator.evaluate(run, payload)
        await store.complete(job, result)
        logging.info("Evaluation completed: %s (%s metrics)", job["job_id"], len(result["scores"]))
    except Exception as error:
        await store.fail(job, clean_error(error))
        logging.error("Evaluation task failed: %s", clean_error(error))
    finally:
        keeper.cancel()
        await asyncio.gather(keeper, return_exceptions=True)


async def main(once=False):
    load_dotenv(ROOT / ".env")
    os.environ.setdefault("RAGAS_DO_NOT_TRACK", "true")
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=3)
    store = EvaluationStore(pool)
    try:
        await store.initialize()
        logging.info("Evaluation worker ready; waiting for explicitly queued tasks")
        while True:
            job = await store.claim()
            if job:
                await process_job(store, job)
            elif once:
                logging.info("No queued tasks")
            if once:
                break
            if not job:
                await asyncio.sleep(2)
    finally:
        await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(main(args.once))
    except KeyboardInterrupt:
        pass

import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
from contextlib import asynccontextmanager
import json
import unittest
from unittest.mock import patch, AsyncMock
from uuid import uuid4

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from operations_api import summarize_runs
from observability import RunTrace,ModelTimingCallback
from runtime_limits import ChatLimits


class RuntimeControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_preparation_redis_setup_is_inside_chat_deadline_and_trace_is_saved(self):
        import agent
        checkpointer=SimpleNamespace()
        async def slow_setup():
            await asyncio.sleep(.1)
        checkpointer.asetup=slow_setup
        @asynccontextmanager
        async def connection(**kwargs):
            yield checkpointer
        trace=RunTrace(1,'timeout-prep','q','kb')
        store=SimpleNamespace(save_run=AsyncMock())
        limits=SimpleNamespace(total_seconds=.01,model_calls=4,output_tokens=4096)
        with patch.object(agent.ChatLimits,'from_env',return_value=limits), \
             patch.object(agent.AsyncRedisSaver,'from_conn_string',side_effect=connection), \
             patch.object(agent,'get_or_create_agent',new_callable=AsyncMock) as build:
            events=[json.loads(row) async for row in agent.stream_agent_response('q','kb',1,
                trace=trace,store=store,memory_enabled=False,evaluation_mode=True)]
        build.assert_not_awaited()
        self.assertEqual(trace.status,'failed')
        self.assertTrue(trace.persisted)
        store.save_run.assert_awaited_once()
        self.assertIn('时间上限',next(row['content'] for row in events if row['type']=='error'))
        self.assertEqual(events[-1]['status'],'failed')

    async def test_callback_stops_before_extra_model_start(self):
        trace=RunTrace(1,'test','q','kb',config={'chat_limits':{'model_calls':1}})
        callback=ModelTimingCallback(trace)
        await callback.on_chat_model_start({},[],run_id=uuid4())
        with self.assertRaises(RuntimeError):await callback.on_chat_model_start({},[],run_id=uuid4())
        self.assertEqual(len(trace.model_inputs),1)
        self.assertTrue(callback.raise_error)

    async def test_stream_deadline_does_not_allow_another_slow_generation(self):
        from agent import bounded_agent_stream
        async def stream():
            yield 'first'
            await asyncio.sleep(.1)
            yield 'too-late'
        seen=[]
        with self.assertRaises(TimeoutError):
            async for item in bounded_agent_stream(SimpleNamespace(astream=stream),remaining_seconds=.01):seen.append(item)
        self.assertEqual(seen,['first'])

    async def test_summary_has_counts_alerts_and_no_private_text(self):
        runs=[{'query':'PRIVATE','answer':'SECRET','status':'failed','timings':{'server_total_ms':100},
               'spans':[{'kind':'model','status':'failed','attributes':{'usage':{'total_tokens':15}}}]}]
        result=summarize_runs(runs)
        self.assertEqual(result['p95_ms'],100)
        self.assertEqual(result['reported_total_tokens'],15)
        self.assertEqual(result['model_calls'],1)
        self.assertTrue(result['alerts'])
        self.assertNotIn('PRIVATE',str(result))
        self.assertNotIn('SECRET',str(result))

    async def test_limits_reject_unbounded_server_configuration(self):
        with patch.dict('os.environ',{'CHAT_MAX_MODEL_CALLS':'1000'}):
            with self.assertRaises(ValueError):ChatLimits.from_env()

"""Registered workflows never change scope or turn local commands into model calls."""
from contextlib import asynccontextmanager
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI
from httpx import AsyncClient,ASGITransport
from schemas import ContextSchema


class SkillExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_catalog_endpoint_exposes_metadata_and_version_without_workflows(self):
        import api
        app=FastAPI()
        app.include_router(api.router)
        async with AsyncClient(transport=ASGITransport(app=app),base_url='http://testserver') as client:
            response=await client.get('/skills')
        self.assertEqual(response.status_code,200)
        body=response.json()
        self.assertEqual(len(body['skills']),9)
        self.assertTrue(body['version'].startswith('echomind-skills-v1:'))
        self.assertNotIn('workflow',response.text)
        self.assertNotIn('instructions',response.text)
        self.assertNotIn('policy',response.text)

    async def test_server_parses_alias_and_preserves_raw_command_in_trace(self):
        import api
        app=FastAPI()
        app.include_router(api.router)
        app.state.evaluation_store=object()
        calls=[]
        async def fake_stream(**kwargs):
            calls.append(kwargs)
            yield json.dumps({'type':'done','status':'complete'})+'\n'
        async with AsyncClient(transport=ASGITransport(app=app),base_url='http://testserver') as client:
            with patch.object(api,'stream_agent_response',side_effect=fake_stream):
                response=await client.get('/chat_with_agent/stream',params={'user_id':7,'knowledge_base_id':'私有甲',
                    'query':'&摘要 检索质量','evaluation_mode':'true','skill_id':'review'})
        self.assertEqual(response.status_code,200)
        call=calls[0]
        self.assertEqual(call['skill_id'],'summary')
        self.assertEqual(call['user_message'],'检索质量')
        self.assertEqual(call['trace'].query,'&摘要 检索质量')
        self.assertEqual((call['user_id'],call['knowledge_base_id']),(7,'私有甲'))
        self.assertTrue(call['skill_version'])

    async def test_local_unknown_and_missing_argument_fail_before_model_or_settings(self):
        import api
        app=FastAPI()
        app.include_router(api.router)
        async with AsyncClient(transport=ASGITransport(app=app),base_url='http://testserver') as client:
            with patch.object(api,'stream_agent_response') as stream:
                for command in ('/settings','&new','/not_registered','/compare'):
                    response=await client.get('/chat_with_agent/stream',params={'user_id':1,'knowledge_base_id':'kb','query':command})
                    self.assertEqual(response.status_code,422,command)
                stream.assert_not_called()

    async def test_workflow_is_system_scoped_and_user_content_stays_user_data(self):
        import agent
        base=agent.build_system_prompt(ContextSchema(1,'kb'))
        active=agent.build_system_prompt(ContextSchema(1,'kb',skill_id='evidence'))
        self.assertTrue(active.startswith(base))
        self.assertGreater(len(active),len(base))
        self.assertEqual(agent.build_system_prompt(ContextSchema(1,'kb')),base)
        with self.assertRaises(ValueError):agent.build_system_prompt(ContextSchema(1,'kb',skill_id='unregistered'))
        with self.assertRaises(ValueError):agent.build_system_prompt(ContextSchema(1,'kb',skill_id='settings'))

    async def test_skill_context_reaches_agent_without_changing_scope_or_tools(self):
        import agent
        from observability import RunTrace
        captured={}
        fake=SimpleNamespace(asetup=AsyncMock())
        @asynccontextmanager
        async def connection(**kwargs):yield fake
        async def stream(compiled,*args,**kwargs):
            captured.update(kwargs)
            captured['input']=args[0]
            if False:yield None
        trace=RunTrace(7,'skills','/summary 内容','私有甲')
        from skill_registry import get_skill
        version=get_skill('summary').version
        with patch.object(agent.AsyncRedisSaver,'from_conn_string',side_effect=connection), \
             patch.object(agent,'get_or_create_agent',new_callable=AsyncMock), \
             patch.object(agent,'bounded_agent_stream',side_effect=stream):
            events=[json.loads(row) async for row in agent.stream_agent_response('内容','私有甲',7,
                trace=trace,memory_enabled=False,skill_id='summary',evaluation_mode=True)]
        context=captured['context']
        self.assertEqual((context.user_id,context.knowledge_base_id,context.skill_id),(7,'私有甲','summary'))
        self.assertFalse(context.memory_enabled)
        self.assertEqual(captured['input'],{'messages':[{'role':'user','content':'内容'}]})
        self.assertEqual(events[0]['skill']['id'],'summary')
        self.assertEqual(trace.config['skill']['version'],version)

    async def test_internal_stale_version_is_rejected_before_model_or_checkpoint(self):
        import agent
        with patch.object(agent,'get_or_create_agent',new_callable=AsyncMock) as build:
            with self.assertRaises(ValueError):
                _=[row async for row in agent.stream_agent_response('内容','kb',1,
                    skill_id='summary',skill_version='forged-version')]
        build.assert_not_awaited()


if __name__=='__main__':unittest.main()

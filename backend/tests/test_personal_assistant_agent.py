"""Actual LangGraph tool execution and HTTP project context; offline model + isolated PG."""
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch
from uuid import UUID,uuid4

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parent))
import test_personal_assistant as fixtures
from assistant.models import ACTION
from assistant.service import project_snapshot
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration,ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field
from schemas import ContextSchema
from orchestration.context_manager import conversation_messages


class ProjectModel(BaseChatModel):
    seen:list=Field(default_factory=list)
    bound:list=Field(default_factory=list)
    perform_tools:bool=True

    @property
    def _llm_type(self): return 'offline-project-test'

    def bind_tools(self,tools,**kwargs):
        self.bound.append([tool.name for tool in tools])
        return self

    def _generate(self,messages,stop=None,run_manager=None,**kwargs):
        self.seen.append(list(messages))
        last = messages[-1]
        if not self.perform_tools:
            message = AIMessage(content='离线项目回答')
        elif last.type!='tool':
            message = AIMessage(content='',tool_calls=[{'name':'get_project_workspace','args':{'section':'summary'},'id':'read-project'}])
        elif last.name=='get_project_workspace':
            message = AIMessage(content='',tool_calls=[{'name':'propose_project_action','id':'save-project-plan',
                'args':{'action':{'kind':'add_tasks','tasks':[{'title':'学习SSE','category':'study','estimated_minutes':30},
                    {'title':'实现流式输出','estimated_minutes':60}]}}}])
        else:
            saved = json.loads(last.content)
            message = AIMessage(content='已保存计划候选，正式任务尚未改变。' if saved['status']=='proposal_saved' else '候选没有保存。')
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _agenerate(self,messages,stop=None,run_manager=None,**kwargs):
        return self._generate(messages,stop,run_manager,**kwargs)


@unittest.skipUnless(os.getenv('ECHOMIND_TEST_DATABASE_URL'),'Isolated PostgreSQL required')
class AssistantAgentTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.AssistantStoreTests.asyncSetUp
    asyncTearDown = fixtures.AssistantStoreTests.asyncTearDown
    project = fixtures.AssistantStoreTests.project
    apply = fixtures.AssistantStoreTests.apply
    source = fixtures.AssistantStoreTests.source

    async def test_real_agent_reads_project_and_saves_atomic_plan_candidate(self):
        import agent
        p = await self.project()
        conv,turn,trace = await self.source(p)
        snapshot = project_snapshot(await self.store.workspace(1,p['project_id']))
        model = ProjectModel()
        with patch.object(agent,'model',model),patch('assistant.tools.get_postgresql_client',new_callable=AsyncMock,return_value=SimpleNamespace(pool=self.pool)):
            compiled = await agent.get_or_create_agent(InMemorySaver(),False,contextual=True,project_enabled=True)
            result = await compiled.ainvoke({'messages':conversation_messages([],'规划流式输出的学习和实现',str(turn['turn_id']))},
                {'configurable':{'thread_id':'isolated-project-plan'}},context=ContextSchema(1,'甲',memory_enabled=False,
                    conversation_turn_id=str(turn['turn_id']),project_snapshot=snapshot))
        self.assertIn('正式任务尚未改变',result['messages'][-1].content)
        workspace = await self.store.workspace(1,p['project_id'])
        self.assertEqual(len(workspace['proposals']),1)
        self.assertEqual(workspace['tasks'],[])
        self.assertEqual(workspace['proposals'][0]['action']['kind'],'add_tasks')
        trace.finish(result['messages'][-1].content)
        await self.conversations.save_run(trace.snapshot())
        saved = await self.store.decide(1,p['project_id'],workspace['proposals'][0]['proposal_id'],'apply',uuid4(),1)
        self.assertEqual(len(saved['entity']['tasks']),2)
        fresh = await self.store.workspace(1,p['project_id'])
        self.assertEqual({task['title']:task['category'] for task in fresh['tasks']},
                         {'学习SSE':'study','实现流式输出':'work'})
        self.assertEqual(fresh['project']['revision'],2)
        systems = [message.content for message in model.seen[0] if message.type=='system']
        self.assertIn('项目协作规则',''.join(systems))
        self.assertIn(p['project_id'],''.join(systems))

    async def test_material_skills_do_not_gain_proposal_write_tool(self):
        import agent
        p = await self.project()
        _,turn,_ = await self.source(p)
        model = ProjectModel(perform_tools=False)
        with patch.object(agent,'model',model):
            compiled = await agent.get_or_create_agent(InMemorySaver(),False,contextual=True,project_enabled=True,proposals_enabled=False)
            await compiled.ainvoke({'messages':conversation_messages([],'总结项目资料',str(turn['turn_id']))},
                {'configurable':{'thread_id':'isolated-project-skill'}},context=ContextSchema(1,'甲',skill_id='summary',
                    memory_enabled=False,conversation_turn_id=str(turn['turn_id']),project_snapshot=project_snapshot(await self.store.workspace(1,p['project_id']))))
        self.assertTrue(model.bound)
        self.assertTrue(all('propose_project_action' not in names for names in model.bound))
        self.assertTrue(all('get_project_workspace' in names for names in model.bound))

    async def test_stream_endpoint_loads_same_project_state_across_distinct_conversations(self):
        from fastapi import FastAPI
        from httpx import ASGITransport,AsyncClient
        from conversations_api import router
        from auth import IdentityMiddleware
        p = await self.project()
        mutation = await self.apply(p,{'kind':'add_note','note_kind':'decision','content':'先做工作台'})
        app = FastAPI()
        app.state.assistant_store = self.store
        app.state.conversation_store = self.conversations
        app.state.evaluation_store = self.audit
        from user_settings import SettingsValues
        app.state.user_settings_store = SimpleNamespace(get=AsyncMock(return_value={'values':SettingsValues(memory_enabled=False).model_dump(),'revision':1}))
        app.include_router(router)
        app.add_middleware(IdentityMiddleware)
        calls = []
        async def offline_stream(**kwargs):
            calls.append(kwargs)
            kwargs['trace'].finish('当前决定是先做工作台。')
            await kwargs['store'].save_run(kwargs['trace'].snapshot())
            yield json.dumps({'type':'answer','content':'当前决定是先做工作台。'})+'\n'
            yield json.dumps({'type':'done','status':'complete'})+'\n'
        with patch.dict(os.environ,{'ECHOMIND_AUTH_MODE':'local','ECHOMIND_LOCAL_USER_ID':'1'}),\
             patch('conversations_api.stream_agent_response',side_effect=offline_stream):
            async with AsyncClient(transport=ASGITransport(app=app,client=('127.0.0.1',1234)),base_url='http://127.0.0.1') as client:
                for _ in range(2):
                    response = await client.post('/conversations',json={'client_create_id':str(uuid4()),'scope_id':'甲','project_id':p['project_id']})
                    self.assertEqual(response.status_code,201,response.text)
                    cid = response.json()['conversation_id']
                    sent = await client.post(f'/conversations/{cid}/turns/stream',json={'client_turn_id':str(uuid4()),'expected_revision':1,'query':'继续上次工作'})
                    self.assertEqual(sent.status_code,200,sent.text)
                    self.assertIn('先做工作台',sent.text)
                    restored = await client.get(f'/conversations/{cid}/messages')
                    self.assertEqual(restored.json()['conversation']['project_id'],p['project_id'])
        self.assertNotEqual(calls[0]['session_id'],calls[1]['session_id'])
        for call in calls:
            self.assertEqual(call['project_snapshot']['recent_notes'][0]['content'],'先做工作台')
            self.assertEqual(call['project_snapshot']['project']['revision'],2)
            self.assertEqual(call['trace'].config['project']['project']['project_id'],p['project_id'])

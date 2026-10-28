"""Real LangGraph agent/checkpointer, fake model, no external providers or stores."""
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration,ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field
from schemas import ContextSchema


class RecordingModel(BaseChatModel):
    seen:list=Field(default_factory=list)
    bound:list=Field(default_factory=list)

    @property
    def _llm_type(self):return 'offline-skill-recording'

    def bind_tools(self,tools,**kwargs):
        self.bound.append([tool.name for tool in tools])
        return self

    def _generate(self,messages,stop=None,run_manager=None,**kwargs):
        self.seen.append(list(messages))
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content='离线替身回答'))])

    async def _agenerate(self,messages,stop=None,run_manager=None,**kwargs):
        return self._generate(messages,stop,run_manager,**kwargs)


class SkillMultiturnTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_agent_does_not_keep_skill_workflow_in_next_plain_turn(self):
        import agent
        model=RecordingModel()
        saver=InMemorySaver()
        config={'configurable':{'thread_id':'offline-skills-two-turn'}}
        with patch.object(agent,'model',model),patch.object(agent,'get_postgresql_client',new_callable=AsyncMock) as database:
            compiled=await agent.get_or_create_agent(saver,memory_enabled=False)
            await compiled.ainvoke({'messages':[{'role':'user','content':'第一轮主题'}]},config,
                context=ContextSchema(7,'隔离资料',memory_enabled=False,skill_id='summary'))
            await compiled.ainvoke({'messages':[{'role':'user','content':'普通下一轮'}]},config,
                context=ContextSchema(7,'隔离资料',memory_enabled=False))
        self.assertEqual(len(model.seen),2)
        first_system=[m.content for m in model.seen[0] if m.type=='system']
        second_system=[m.content for m in model.seen[1] if m.type=='system']
        self.assertIn('本轮技能工作流',''.join(first_system))
        self.assertNotIn('本轮技能工作流',''.join(second_system))
        self.assertTrue(model.bound)
        self.assertTrue(all(tools==['search_knowledge_base'] for tools in model.bound))
        checkpoint=await saver.aget_tuple(config)
        self.assertFalse(any(m.type=='system' for m in checkpoint.checkpoint['channel_values']['messages']))
        database.assert_not_awaited()

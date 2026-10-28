"""Protocol and budget boundaries are checked before any provider request."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, SystemMessage
from orchestration.context_manager import conversation_messages, project_messages, serialized_size


class ContextManagerTests(unittest.TestCase):
    def messages(self):
        return conversation_messages([{'turn_id':'past','model_query':'历史限定条件','answer':'历史结论（来源：旧文档，E1）'}],
                                     '现在的问题','now')

    def test_historical_citations_are_never_current_evidence_and_original_is_unchanged(self):
        messages = self.messages()
        self.assertNotIn('E1',messages[1].content)
        self.assertIn('历史引用',messages[1].content)
        selected,pack = project_messages(messages,SystemMessage('rules'),[],'now',65536)
        self.assertEqual(selected,messages)
        self.assertEqual(pack['reason'],'within_budget')

    def test_trim_complete_old_pairs_but_keep_current_tool_protocol_and_evidence_intact(self):
        old = conversation_messages([{'turn_id':'past','model_query':'旧问题'*2000,'answer':'旧回答'*2000}], '当前问题','now')
        current = [AIMessage(content='',tool_calls=[{'id':'tool1','name':'search','args':{}}]),
                   ToolMessage(content='当前 E1 的完整原文',tool_call_id='tool1')]
        budget,_ = serialized_size(old[2:]+current,None,[])
        selected,pack = project_messages(old+current,None,[],'now',budget)
        self.assertEqual(selected,old[2:]+current)
        self.assertEqual(pack['dropped_message_ids'],['past:user','past:assistant'])
        self.assertIn('完整原文',selected[-1].content)

    def test_current_question_or_evidence_cannot_be_silently_truncated(self):
        with self.assertRaisesRegex(ValueError,'超过输入预算'):
            project_messages([HumanMessage(content='大问题'*3000,id='now:user')],None,[],'now',2048)

    def test_original_user_goal_survives_trimming_recent_pairs(self):
        messages=conversation_messages([{'turn_id':'first','model_query':'总预算十万元','is_anchor':True},
            {'turn_id':'past','model_query':'旧问题'*2000,'answer':'旧回答'*2000}], '继续完善','now')
        minimum,_=serialized_size([messages[0],messages[-1]],None,[])
        selected,pack=project_messages(messages,None,[],'now',minimum)
        self.assertEqual(selected,[messages[0],messages[-1]])
        self.assertIn('总预算十万元',selected[0].content)

    def test_missing_boundary_and_broken_tool_protocol_fail_closed(self):
        cases = [[],[HumanMessage(content='x',id='wrong')],
                 [HumanMessage(content='x',id='now:user'),ToolMessage(content='孤立',tool_call_id='missing')],
                 [HumanMessage(content='x',id='now:user'),AIMessage(content='',tool_calls=[{'id':'a','name':'t','args':{}}])]]
        for messages in cases:
            with self.subTest(messages=messages),self.assertRaises(ValueError):
                project_messages(messages,None,[],'now',65536)

    def test_system_and_tool_schema_count_towards_input_budget(self):
        messages = [HumanMessage(content='当前问题',id='now:user')]
        small,_ = serialized_size(messages,None,[])
        schema = {'type':'function','function':{'name':'large','description':'schema '*5000,
                    'parameters':{'type':'object','properties':{}}}}
        with self.assertRaisesRegex(ValueError,'超过输入预算'):
            project_messages(messages,SystemMessage('rules'),[schema],'now',small+100)


class FrozenScopeRetrievalTests(unittest.IsolatedAsyncioTestCase):
    async def test_deleted_or_changed_source_is_rejected_before_model_input(self):
        import json
        from types import SimpleNamespace
        from unittest.mock import AsyncMock,patch
        from orchestration.context_manager import validate_visible_sources
        from observability import RunTrace
        from schemas import ContextSchema
        trace = RunTrace(1,'unit','q','甲')
        trace.add_context('final','knowledge','原文',included_in_prompt=True,file_name='文档.pdf',
            knowledge_base_id='甲',parent_id='p1',file_hash='h1',user_id=1)
        tool=ToolMessage(content=json.dumps({'evidence':[{'id':'E1','file':'文档.pdf','knowledge_base':'甲','text':'原文'}]}),tool_call_id='t1')
        with patch('postgresql_client.get_postgresql_client',AsyncMock(return_value=SimpleNamespace(pool=object()))),\
             patch('citation_service.CitationService._current_source',AsyncMock(return_value=('source_deleted',None))):
            with self.assertRaisesRegex(ValueError,'资料已变化'):
                await validate_visible_sources([tool],ContextSchema(1,'甲',allowed_scope_ids=('甲',)),trace)

    async def test_new_library_added_during_aggregate_turn_is_filtered_before_rerank(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from rag.retrieval_service import RetrievalService
        from rag.types import RetrievalScope, RetrievalOptions
        from test_rag_retrieval import hit,record
        milvus = SimpleNamespace(retrieve_knowledge_hits=AsyncMock(return_value=[hit(kb='甲'),hit(kb='新增库')]))
        database = SimpleNamespace(get_parents_with_metadata=AsyncMock(return_value=[record(kb='甲'),record(kb='新增库')]))
        rerank = AsyncMock()
        result = await RetrievalService(milvus,database,rerank).retrieve_context('问题',
            RetrievalScope(1,'默认知识库',('甲',)), options=RetrievalOptions(rerank_enabled=False,graph_enabled=False,evidence_enabled=False))
        self.assertEqual([c.knowledge_base_id for c in result.selected],['甲'])

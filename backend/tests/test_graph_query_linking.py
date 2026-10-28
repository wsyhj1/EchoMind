"""Query-key normalization preserves identity and refuses truncated ambiguity."""
from copy import deepcopy
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from graph.retriever import GraphRetriever,descriptor_key,seed_groups
from graph.settings import GraphSettings
from rag.types import RetrievalScope


class QueryLinkingTests(unittest.IsolatedAsyncioTestCase):
    async def test_descriptor_keys_are_limited_and_do_not_rewrite_identity(self):
        self.assertEqual(descriptor_key('当前演示导航系统','System'),'演示导航系统')
        for name in ('当前系统','目前业务系统','当前System','面向当前演示导航系统'):
            self.assertEqual(descriptor_key(name,'System'),name.casefold())
        self.assertEqual(descriptor_key('当前演示导航系统','Product'),'当前演示导航系统')
        seed={'normalized_name':'当前演示导航系统','entity_type':'System','knowledge_base_id':'kb','aliases':[]}
        before=deepcopy(seed)
        self.assertEqual(len(seed_groups([seed],'演示导航系统有哪些模块？')),1)
        self.assertEqual(seed,before)
        self.assertFalse(seed_groups([seed],'旧演示导航系统有哪些模块？'))

    async def test_query_candidate_overflow_refuses_before_partial_uniqueness(self):
        pool=SimpleNamespace(fetch=AsyncMock(return_value=[{} for _ in range(13)]))
        result=await GraphRetriever(SimpleNamespace(pool=pool),GraphSettings()).recall('演示导航系统有哪些模块？',RetrievalScope(1,'kb'),[])
        self.assertFalse(result.paths)
        self.assertEqual(result.seed_method,'candidate_limit')

    async def test_vector_candidate_overflow_is_also_refused(self):
        pool=SimpleNamespace(fetch=AsyncMock(side_effect=[[],[{} for _ in range(13)]]))
        result=await GraphRetriever(SimpleNamespace(pool=pool),GraphSettings()).recall('有哪些模块？',RetrievalScope(1,'kb'),[SimpleNamespace(parent_id='fixture')])
        self.assertFalse(result.paths)
        self.assertEqual(result.seed_method,'candidate_limit')


if __name__=='__main__':
    unittest.main()

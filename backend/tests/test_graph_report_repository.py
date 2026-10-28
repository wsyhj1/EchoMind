"""New predicates and per-file v3/v2 preference in an isolated PostgreSQL schema."""
from dataclasses import replace
import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from graph.retriever import GraphRetriever
from graph.schemas import Extraction
from rag.types import RetrievalScope
import test_graph_repository as fixtures
from test_graph import extraction as operation_extraction
from test_graph_report_domain import CASES, extraction


@unittest.skipUnless(os.getenv("ECHOMIND_TEST_DATABASE_URL"), "Set isolated PostgreSQL test URL")
class ReportRepositoryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.GraphRepositoryTests.asyncSetUp
    asyncTearDown = fixtures.GraphRepositoryTests.asyncTearDown
    parent = fixtures.GraphRepositoryTests.parent
    index = fixtures.GraphRepositoryTests.index

    async def test_current_descriptor_does_not_block_named_system_linking(self):
        case=('System','当前演示导航系统','HAS_MODULE','Module','页面定位模块',
              '当前演示导航系统由页面定位模块、审计模块组成。')
        parent=await self.parent(case[-1])
        await self.index(parent,Extraction.model_validate(extraction(case)),version='v3')
        retriever=GraphRetriever(self.repository,self.settings)
        result=await retriever.recall('演示导航系统有哪些模块？',RetrievalScope(1,'kb'),[])
        self.assertTrue(result.paths)
        self.assertEqual(result.paths[0].predicates,['HAS_MODULE'])
        self.assertFalse((await retriever.recall('演示导航系统有哪些模块？',RetrievalScope(2,'kb'),[])).paths)
        for prefix in ('旧','非','原来的'):
            self.assertFalse((await retriever.recall(prefix+'演示导航系统有哪些模块？',RetrievalScope(1,'kb'),[])).paths)

    async def test_descriptor_and_shared_alias_candidates_remain_ambiguous(self):
        for name in ('当前演示导航系统','演示导航系统'):
            quote=f'{name}包含页面定位模块。'
            case=('System',name,'HAS_MODULE','Module','页面定位模块',quote)
            parent=await self.parent(quote)
            await self.index(parent,Extraction.model_validate(extraction(case)),version='v3')
        result=await GraphRetriever(self.repository,self.settings).recall('演示导航系统有哪些模块？',RetrievalScope(1,'kb'),[])
        self.assertFalse(result.paths)
        for name in ('导航作品甲','导航作品乙'):
            quote=f'{name}简称页签平台。{name}包含页面定位模块。'
            case=('System',name,'HAS_MODULE','Module','页面定位模块',quote)
            raw=extraction(case)
            raw['entities'][0]['aliases']=['页签平台']
            parent=await self.parent(quote)
            await self.index(parent,Extraction.model_validate(raw),version='v3')
        result=await GraphRetriever(self.repository,self.settings).recall('页签平台有哪些模块？',RetrievalScope(1,'kb'),[])
        self.assertFalse(result.paths)

    async def test_domain_predicate_persists_and_returns_only_grounded_source(self):
        case = CASES[2]
        parent = await self.parent(case[-1])
        await self.index(parent, Extraction.model_validate(extraction(case)), version="v3")
        result = await GraphRetriever(self.repository, self.settings).recall("功能乙由什么实现？", RetrievalScope(1, "kb"), [])
        self.assertTrue(result.paths)
        self.assertEqual(result.paths[0].predicates, ["IMPLEMENTED_BY"])
        self.assertEqual({row["parent_id"] for row in result.records}, {parent["parent_id"]})
        pinned = await GraphRetriever(self.repository, replace(self.settings, version="v2")).recall("功能乙由什么实现？", RetrievalScope(1, "kb"), [])
        self.assertEqual(pinned.paths, [])

    async def test_ready_v3_removing_an_edge_shadows_same_file_v2_but_not_other_files(self):
        parent = await self.parent("订单系统使用赤霄缓存集群。")
        await self.index(parent, operation_extraction(), version="v2")
        retriever = GraphRetriever(self.repository, self.settings)
        self.assertTrue((await retriever.recall("订单系统使用什么？", RetrievalScope(1, "kb"), [])).paths)
        # A pending newer extraction must not make a completed older file vanish.
        await self.repository.enqueue(1, "kb", parent["file_hash"], "v3")
        self.assertTrue((await retriever.recall("订单系统使用什么？", RetrievalScope(1, "kb"), [])).paths)
        await self.index(parent, Extraction(entities=[], relations=[]), version="v3")
        self.assertEqual((await retriever.recall("订单系统使用什么？", RetrievalScope(1, "kb"), [])).paths, [])
        pinned = GraphRetriever(self.repository, replace(self.settings, version="v2"))
        self.assertTrue((await pinned.recall("订单系统使用什么？", RetrievalScope(1, "kb"), [])).paths)
        other_file = await self.parent("订单系统使用赤霄缓存集群。")
        await self.index(other_file, operation_extraction(), version="v2")
        result = await retriever.recall("订单系统使用什么？", RetrievalScope(1, "kb"), [])
        self.assertTrue(result.paths)
        self.assertEqual({row["parent_id"] for row in result.records}, {other_file["parent_id"]})


if __name__ == "__main__":
    unittest.main()

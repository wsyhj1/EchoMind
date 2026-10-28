"""A READY graph row is not sufficient: the quote must assert its directed predicate."""
import hashlib
import os
from pathlib import Path
import sys
import unittest
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from graph.retriever import GraphRetriever
from graph.schemas import normalize
from rag.types import RetrievalScope
import test_graph_repository as fixtures


BAD_CASES = [
    ('DEPENDS_ON', 'ASR', 'Service', '模型', 'Service', '使后续ASR、模型和图模块独立开发'),
    ('DEPENDS_ON', 'ASR', 'Service', '图模块', 'Service', '使后续ASR、模型和图模块独立开发'),
    ('USES', '研究甲', 'System', '研究乙', 'Database', '研究甲研究文本模板。研究乙提供音频数据。'),
    ('DEPENDS_ON', '报告页', 'System', '详情页', 'System', '报告页导出的状态说明与详情页一致。'),
    ('MAINTAINED_BY', '语音工具', 'Service', '某公司', 'Organization', '某公司. 语音工具[EB/OL]'),
    ('USES', 'engine.analyze', 'Service', 'Vosk', 'Service', '该测量只包围engine.analyze函数，不含输入读取、Vosk、MacBERT。'),
    ('USES', 'engine.analyze', 'Service', 'MacBERT', 'Service', '该测量只包围engine.analyze函数，不含输入读取、Vosk、MacBERT。'),
    ('USES', '本地Edge', 'System', '回环静态服务', 'Service', '浏览器检查使用本地Edge与回环静态服务'),
]


async def raw_ready_edge(test, case, kb):
    """Intentionally bypass all extraction validation, preserving real DB constraints."""
    predicate, subject, subject_type, target, target_type, quote = case
    parent = await test.parent(quote, kb=kb)
    await test.repository.enqueue(1, kb, parent['file_hash'], 'v2')
    job = await test.repository.claim(1, kb, 'v2', parent['file_hash'])
    subject_id, object_id, relation_id = uuid4(), uuid4(), uuid4()
    for eid, name, kind in [(subject_id, subject, subject_type), (object_id, target, target_type)]:
        await test.pool.execute('''INSERT INTO kg_entities(entity_id,user_id,knowledge_base_id,entity_type,canonical_name,normalized_name)
            VALUES($1,1,$2,$3,$4,$5)''', eid, kb, kind, name, normalize(name))
        await test.pool.execute('INSERT INTO kg_entity_mentions VALUES($1,$2,1,$3,$4,$5,$6)',
            eid, parent['parent_id'], kb, parent['file_hash'], 'v2', job['job_id'])
    await test.pool.execute('INSERT INTO kg_relations VALUES($1,1,$2,$3,$4,$5,$6)',
        relation_id, kb, subject_id, predicate, object_id, 'v2')
    await test.pool.execute('INSERT INTO kg_relation_evidence VALUES($1,$2,1,$3,$4,$5,$6,$7,$8,$9)',
        uuid4(), relation_id, kb, parent['parent_id'], parent['file_hash'], quote,
        hashlib.sha256(quote.encode()).hexdigest(), 'v2', job['job_id'])
    await test.pool.execute("UPDATE kg_index_jobs SET status='READY' WHERE job_id=$1", job['job_id'])
    return parent


@unittest.skipUnless(os.getenv('ECHOMIND_TEST_DATABASE_URL'), 'Set ECHOMIND_TEST_DATABASE_URL for isolated graph checks')
class GraphSemanticRepositoryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.GraphRepositoryTests.asyncSetUp
    asyncTearDown = fixtures.GraphRepositoryTests.asyncTearDown
    parent = fixtures.GraphRepositoryTests.parent

    async def test_ready_raw_storage_cannot_bypass_online_predicate_support(self):
        suffix = {'USES': '使用什么？', 'DEPENDS_ON': '依赖什么？', 'MAINTAINED_BY': '谁维护？'}
        for index, case in enumerate(BAD_CASES):
            kb = 'bad_' + str(index)
            await raw_ready_edge(self, case, kb)
            result = await GraphRetriever(self.repository, self.settings).recall(case[1] + suffix[case[0]], RetrievalScope(1, kb), [])
            with self.subTest(index=index, predicate=case[0]):
                self.assertFalse(result.paths, 'An unsupported stored edge must never reach online paths')


if __name__ == '__main__':
    unittest.main()

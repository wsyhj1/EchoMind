"""A grounded quote must assert the directed predicate, not just name endpoints."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class DirectedQuoteTests(unittest.TestCase):
    def supports(self, subject, predicate, target, quote, **kwargs):
        from graph.semantic_guard import quote_supports_relation
        return quote_supports_relation(subject, predicate, target, quote, **kwargs)

    def test_parallel_research_exclusions_bibliography_and_shared_checks_are_not_edges(self):
        cases = [
            ('ASR', 'DEPENDS_ON', '模型', '统一接口使后续ASR、模型和图模块独立开发。'),
            ('ASR', 'DEPENDS_ON', '图模块', '统一接口使后续ASR、模型和图模块独立开发。'),
            ('ScamGen', 'USES', 'TeleAntiFraud-28k', 'ScamGen研究合成对话。TeleAntiFraud-28k研究电话反诈。'),
            ('报告页', 'DEPENDS_ON', '详情页', '报告页与详情页的状态说明应一致。'),
            ('Vosk', 'MAINTAINED_BY', 'ALPHA CEPHEI', '参考文献：ALPHA CEPHEI. Vosk offline speech recognition API and models.'),
            ('engine.analyze', 'USES', 'Vosk', 'engine.analyze测量不含Vosk或MacBERT。'),
            ('engine.analyze', 'USES', 'MacBERT', 'engine.analyze测量不含Vosk或MacBERT。'),
            ('本地Edge', 'USES', '回环静态服务', '检查使用本地Edge与回环静态服务。'),
            ('乙系统', 'USES', '缓存甲', '缓存甲使用乙系统。'),
            ('甲系统', 'USES', '乙服务', '甲系统采用丙工具，乙服务单独运行。'),
            ('Alpha', 'USES', 'Gamma', 'Alpha uses Beta. Gamma runs alone.'),
            ('甲系统', 'USES', '乙服务', '例如甲系统使用乙服务。'),
        ]
        for subject, predicate, target, quote in cases:
            with self.subTest(quote=quote):
                self.assertFalse(self.supports(subject, predicate, target, quote))

    def test_explicit_direction_and_component_lists_are_supported(self):
        cases = [
            ('订单系统', 'USES', '赤霄缓存集群', '订单系统使用赤霄缓存集群。'),
            ('赤霄缓存集群', 'MAINTAINED_BY', '基础设施甲组', '赤霄缓存集群由基础设施甲组维护。'),
            ('当前可执行系统', 'USES', '导出模块', '当前可执行系统由规则引擎、浏览器交互层和导出模块组成。'),
            ('index.html', 'USES', '规则引擎', 'index.html加载规则引擎。'),
            ('index.html', 'USES', '规则引擎', '入口index.html依次加载规则引擎'),
            ('订单系统', 'DEPENDS_ON', '审计服务', '订单系统依赖审计服务。'),
            ('知止系统', 'DEVELOPED_BY', '开发甲组', '知止系统由开发甲组开发。'),
            ('赤霄缓存集群', 'HAS_ISSUE', '连接池耗尽', '赤霄缓存集群的典型故障是连接池耗尽。'),
            ('连接池耗尽', 'RESOLVED_BY', '限制突发请求', '遇到连接池耗尽时，处理步骤为：限制突发请求。'),
            ('缓存甲', 'PART_OF', '订单系统', '缓存甲属于订单系统。'),
            ('Alpha', 'USES', 'Beta', 'Alpha uses Beta.'),
            ('Alpha', 'MAINTAINED_BY', 'Team A', 'Alpha is maintained by Team A.'),
        ]
        for subject, predicate, target, quote in cases:
            with self.subTest(predicate=predicate, quote=quote):
                self.assertTrue(self.supports(subject, predicate, target, quote))

    def test_dataset_cases_cannot_become_services_and_aliases_are_quote_grounded(self):
        self.assertFalse(self.supports('当前可执行系统', 'USES', '合成案例', '当前可执行系统由合成案例组成。', target_type='Service'))
        self.assertTrue(self.supports('订单系统', 'USES', '赤霄缓存集群', '订单使用赤霄。', subject_aliases=['订单'], target_aliases=['赤霄']))
        self.assertFalse(self.supports('订单系统', 'USES', '赤霄缓存集群', '甲使用乙。'))
        self.assertFalse(self.supports('订单系统', 'USES', '赤霄缓存集群', '订单系统使用赤霄缓存集群。', target_type='Team'))
        self.assertFalse(self.supports('订单系统', 'USES', '赤霄缓存集群', '2024年订单系统使用赤霄缓存集群。'))
        self.assertFalse(self.supports('订单系统', 'USES', '赤霄缓存集群', '订单系统计划使用赤霄缓存集群。'))


if __name__ == '__main__':
    unittest.main()

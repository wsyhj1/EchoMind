"""Conservative directed quote templates for online edges; no model calls.

Grounded endpoints alone do not establish a predicate. Unrecognized wording
stays in storage for audit but cannot extend an online path.
"""
import json
import re
from datetime import datetime, timezone

from graph.schemas import DOMAINS, REPORT_GENERIC_NAMES, normalize

POLICY_VERSION = 'explicit-directed-quote-v3-report-composition'


def _names(canonical, aliases, body):
    if isinstance(aliases, str):
        try:
            aliases = json.loads(aliases)
        except ValueError:
            aliases = []
    aliases = aliases if isinstance(aliases, (list, tuple)) else []
    return [re.escape(name) for name in dict.fromkeys(normalize(value) for value in [canonical, *aliases[:8]]
                                                      if isinstance(value, str) and 0 < len(value) <= 160) if name in body]


def quote_supports_relation(subject, predicate, target, quote, *, subject_aliases=(), target_aliases=(),
                            subject_type='', target_type=''):
    if not isinstance(quote, str) or not quote.strip() or len(quote) > 1000:
        return False
    domains, ranges = DOMAINS.get(predicate, (set(), set()))
    if (subject_type and subject_type not in domains) or (target_type and target_type not in ranges):
        return False
    report_types = {'Feature', 'Module', 'Model', 'Dataset', 'Metric', 'Experiment', 'Report'}
    if (subject_type in report_types and normalize(subject) in REPORT_GENERIC_NAMES) or (target_type in report_types and normalize(target) in REPORT_GENERIC_NAMES):
        return False
    if re.search(r'曾经|以前|计划|尚未|已终止|不再|未来|预计|拟(?:在|于|使用|加入)|可能|也许|'
                 r'planned|formerly|previously|might|may\s|would', quote, re.I):
        return False
    if any(int(year) != datetime.now(timezone.utc).year for year in re.findall(r'(20\d{2})\s*年', quote)):
        return False
    # Cases and datasets do not become executable services by model relabeling.
    if target_type in ('Service', 'System', 'Database', 'CacheCluster') and re.search(r'(?:案例|样本|数据集)$|数据集|样本集|评测集|训练集', target):
        return False
    if target_type == 'Module' and re.search(r'(?:案例|样本|数据集|案例集)$|数据集|样本集|评测集|训练集', target) and not re.search(r'(?:模块|工具|引擎|服务|处理器|适配器)$', target):
        return False
    if target_type == 'Metric' and re.fullmatch(r'[+-]?\d+(?:[.,]\d+)?(?:%|分|秒|ms|指标|值)?', normalize(target)):
        return False
    # Keep sentence boundaries; PDF line wrapping is whitespace inside a sentence.
    for sentence in re.split(r'[。！？!?；;]', quote):
        body = normalize(sentence)
        if re.search(r'例如|假设|假定|示例|假想|如果|suppose|example|hypothet', body):
            continue
        if re.search(r'不(?:含|包含|包括|使用|采用|调用|加载|接入|依赖|维护|负责|属于|支持|实现|提供|报告)|未(?:使用|采用|调用|接入|维护|支持|实现|报告|记录)|'
                     r'不是|不由|并非|独立开发|(?:doesnot|donot|not)(?:use|depend|maintain|include)|without', body):
            continue
        subjects, targets = _names(subject, subject_aliases, body), _names(target, target_aliases, body)
        for source in subjects:
            for destination in targets:
                if source == destination:
                    continue
                lead = source + r'(?:系统|平台|服务|模块|组件|功能|实例|集群|缓存集群)?(?:当前|目前|主要|默认|直接|实际|依次|按需)?'
                gap = r'[^。！？;；，,.]{0,48}'
                patterns = []
                if predicate == 'USES':
                    patterns = [lead + r'(?:使用|采用|调用|加载|接入|uses?|utilizes?|loads?|calls?|integrates?)(?:了)?' + gap + destination,
                                lead + r'由[^。！？;；.]{0,240}' + destination + r'[^。！？;；.]{0,240}(?:组成|构成)',
                                destination + r'(?:被|由)' + source + r'(?:使用|调用|加载)']
                elif predicate == 'DEPENDS_ON':
                    patterns = [lead + r'(?:依赖|dependson|dependon|requires)' + gap + destination]
                elif predicate in ('MAINTAINED_BY', 'DEVELOPED_BY'):
                    verbs = r'(?:维护|管理|运维)' if predicate == 'MAINTAINED_BY' else r'(?:开发|研发)'
                    english = r'(?:ismaintainedby|maintainedby|isownedby|ownedby)' if predicate == 'MAINTAINED_BY' else r'(?:isdevelopedby|developedby)'
                    patterns = [lead + r'(?:由|归|交由)' + destination + r'(?:团队|小组|组织)?(?:负责)?' + verbs,
                                destination + r'(?:负责)?' + verbs + source,
                                destination + r'负责' + source + r'的?' + verbs,
                                lead + english + destination]
                    if predicate == 'MAINTAINED_BY':
                        patterns.append(lead + r'的?(?:维护团队|维护方|维护组)(?:为|是|:|：)' + destination)
                elif predicate == 'HAS_ISSUE':
                    patterns = [lead + r'的?(?:典型故障|故障|已知问题|问题)(?:为|是|包括|:|：)' + destination,
                                lead + r'(?:hastheissue|hasissue)' + destination]
                elif predicate == 'RESOLVED_BY':
                    patterns = [r'(?:遇到)?' + source + r'时?[,，]?(?:处理步骤|处置方法|解决方式)(?:为|是|:|：){1,2}' + destination,
                                source + r'(?:可)?(?:由|通过|使用|采用)' + destination + r'(?:解决|修复)',
                                source + r'(?:is)?resolvedby' + destination]
                elif predicate == 'PART_OF':
                    patterns = [lead + r'(?:属于|ispartof|partof)' + destination,
                                lead + r'是' + destination + r'的(?:组成部分|组件)',
                                destination + r'由[^。！？;；.]{0,240}' + source + r'[^。！？;；.]{0,240}(?:组成|构成)']
                elif predicate in ('HAS_FEATURE', 'HAS_MODULE', 'HAS_EXPERIMENT'):
                    verbs = {'HAS_FEATURE': r'(?:具有|具备|提供|支持|包含|has|provides|supports)',
                             'HAS_MODULE': r'(?:包含|包括|集成|contains|includes)',
                             'HAS_EXPERIMENT': r'(?:包含|包括|开展|进行了|conducts|includes)'}[predicate]
                    patterns = [lead + verbs + r'(?:了)?' + gap + destination]
                    if predicate == 'HAS_MODULE':
                        for composition in re.finditer(lead + r'由([^。！？;；.]{1,240}?)(?:组成|构成)', body):
                            # Punctuation-delimited complete items only. Do not
                            # expand omitted names in "audit and export module"
                            # or accept a target substring of another component.
                            items = [item.strip('“”‘’\"\'') for item in re.split(r'[、，,]', composition[1])]
                            if any(re.fullmatch(destination, item) for item in items):
                                return True
                elif predicate == 'IMPLEMENTED_BY':
                    patterns = [lead + r'(?:由|通过)' + destination + r'(?:模块|模型|服务)?(?:来)?实现',
                                destination + r'(?:模块|模型|服务)?实现' + source,
                                lead + r'(?:isimplementedby|implementedby)' + destination]
                elif predicate == 'EVALUATED_ON':
                    patterns = [lead + r'(?:在|使用|基于)' + destination + r'(?:上|进行|开展|作为数据)?(?:评估|评测|验证|测试)',
                                lead + r'的?(?:评估数据集|评测数据集)(?:为|是|:|：)' + destination,
                                lead + r'(?:isevaluatedon|evaluatedon)' + destination]
                elif predicate == 'REPORTS_METRIC':
                    patterns = [lead + r'(?:报告|记录|给出|reports|records)(?:了)?' + destination + r'(?:指标)?',
                                lead + r'的?(?:评估指标|报告指标)(?:为|是|:|：)' + destination]
                if any(re.search(pattern, body) for pattern in patterns):
                    return True
    return False

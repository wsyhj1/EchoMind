"""Conservative, quote-grounded checks for Chinese operations/relationship questions.

This deliberately does not equate similarity with entailment. Unrecognized question
types and temporal/version constraints remain partial; no model or Gold is used.
"""
from dataclasses import asdict, dataclass, field
import re

from rag.types import RetrievalCandidate

LABELS = {"uses": "使用的实例或缓存名称", "owner": "维护团队与归属关系",
          "ttl": "有效期或过期时长", "fault": "典型故障", "procedure": "故障排查与处理步骤",
          "prerequisite": "操作前置条件", "generic": "问题所需的明确原文依据"}
INTENTS = {
    "uses": r"(?:哪个|什么|名称|叫什么).{0,5}(?:缓存|集群)|缓存(?:名称|集群叫什么)|缓存在什么地方",
    "owner": r"维护|谁负责|谁来管|归.{0,8}团队|维护组|谁.{0,2}管|谁.{0,2}负责",
    "ttl": r"有效期|过期|保鲜|TTL|ttl",
    "fault": r"典型故障|故障是什么",
    "procedure": r"排障步骤|怎么处理|如何处理|处理步骤|有哪些操作|排查步骤|如何排查",
    "prerequisite": r"(?:故障|处理|操作|执行)前.{0,8}(?:做|必须|准备)",
}
ENTITY = r"[\u4e00-\u9fffA-Za-z0-9_-]{2,40}?"
INSTANCE = ENTITY + r"(?:缓存集群|演练缓存|缓存|集群|实例|系统|服务)"
NEGATED = re.compile(r"不使用|不维护|不负责|尚未|计划|可能|假设|已终止|历史")
CONSTRAINED = re.compile(r"20\d{2}|\bv\d|版本|去年|今年|明年|历史|旧职责|未来|迁移计划|过去|曾经|以前|先前|之前|前任|将来|之后|目前|当前|现在", re.I)


@dataclass
class SubGoal:
    goal_id: str
    kind: str
    question: str


@dataclass
class Support:
    key: tuple[int, str, str]
    quote: str


@dataclass
class GoalCoverage:
    goal_id: str
    kind: str
    status: str
    supports: list[Support] = field(default_factory=list)
    reason: str = ""


@dataclass
class Coverage:
    goals: list[SubGoal]
    coverage: list[GoalCoverage]
    linked_entities: list[str] = field(default_factory=list)

    @property
    def missing(self):
        return [item for item in self.coverage if item.status in ("partial", "missing")]

    def snapshot(self):
        return asdict(self)


@dataclass
class Fact:
    subject: str
    kind: str
    value: str
    support: Support


def _clean_subject(value):
    value = re.sub(r"^(?:当前版本\d+|实例故障手册|缓存职责登记|架构说明|说明|查找|谁负责)", "", value)
    return value.strip()


def facts(candidate: RetrievalCandidate):
    text = candidate.text
    found = []
    patterns = [
        ("uses", rf"({INSTANCE})使用({INSTANCE})"),
        ("owner", rf"({INSTANCE})由({ENTITY}(?:组|团队|部门|部))维护"),
        ("owner", rf"({INSTANCE})由({ENTITY}(?:组|团队|部门|部))负责"),
        ("fault", rf"({INSTANCE})的典型故障是([^。；\n]+)"),
    ]
    for kind, pattern in patterns:
        for match in re.finditer(pattern, text):
            quote = match.group(0)
            if not NEGATED.search(quote):
                found.append(Fact(_clean_subject(match[1]), kind, match[2], Support(candidate.key, quote)))
    # Local sentence context binds a procedure to an explicit instance in the same parent.
    for match in re.finditer(r"[^。\n]+[。]?", text):
        sentence = match.group(0)
        uses = [fact for fact in found if fact.kind == "uses" and fact.support.quote in sentence]
        ttl = re.search(r"(?:有效期|过期时间)(?:为|是)?\s*(\d+(?:\.\d+)?\s*(?:秒|分钟|小时|天))", sentence)
        if ttl and uses:
            found.append(Fact(uses[0].subject, "ttl", ttl[1], Support(candidate.key, sentence)))
        for kind, pattern in (("procedure", r"(?:处理步骤为|处理步骤是)[：:]?\s*([^。]+)。"),
                              ("prerequisite", r"(?:执行|操作|处理)前必须([^。]+)。")):
            value = re.search(pattern, sentence)
            anchors = [fact for fact in found if fact.kind == "fault"]
            if value and len({(fact.subject, fact.value) for fact in anchors}) == 1 and not NEGATED.search(sentence):
                # Keep the binding sentence as part of the exact contiguous quote.
                anchor = anchors[0]
                start = text.find(anchor.support.quote)
                if 0 <= start <= match.start():
                    quote = text[start:match.end()]
                    found.append(Fact(anchor.subject, kind, value[1], Support(candidate.key, quote)))
    return found


def decompose(query):
    if len(query) > 2000:
        raise ValueError("Query exceeds the evidence-check budget")
    kinds = [kind for kind, pattern in INTENTS.items() if re.search(pattern, query)]
    if "prerequisite" in kinds and "procedure" in kinds:
        kinds.remove("procedure")
    if not kinds:
        kinds = ["generic"]
    if len(kinds) > 3:
        kinds = kinds[:2] + ["generic"]  # Never silently certify omitted facets.
    return [SubGoal(f"g{i}", kind, query + "\n所需依据：" + LABELS[kind]) for i, kind in enumerate(kinds, 1)]


def _anchor(query, all_facts):
    subjects = {fact.subject for fact in all_facts} | {fact.value for fact in all_facts if fact.kind == "uses"}
    exact = [subject for subject in subjects if subject in query]
    if exact:
        # Two independent named subjects require a richer comparison checker.
        longest = max(exact, key=len)
        return longest if all(subject in longest for subject in exact) else None
    cleaned = re.sub(r"^(?:请|说明|查找|谁负责|谁维护|生产)+", "", query)
    prefix = re.split(r"由|遇到|处理|这个实例|谁|的|缓存|实例|集群|[，,？?。\s]", cleaned, maxsplit=1)[0]
    if len(prefix) < 2:
        return None
    matches = [subject for subject in subjects if subject.startswith(prefix)]
    return matches[0] if len(matches) == 1 else None


def _fault_constraint(query, anchor):
    match = re.search(r"(?:^|[，,；;。])([^，,；;。？?]{0,80}?)(?:怎么处理|如何处理|有哪些操作)", query)
    if not match:
        return None
    prefix = match[1]
    if anchor:
        prefix = prefix.replace(anchor, "")
    prefix = re.sub(r"^.*(?:遇到|出现|发生)", "", prefix)
    prefix = re.sub(r"^(?:请问|请|的|缓存|实例|集群|故障|问题|时)+", "", prefix).strip()
    return prefix or None


class EvidenceChecker:
    async def check(self, query, candidates):
        goals = decompose(query)
        all_facts = [fact for candidate in candidates for fact in facts(candidate)]
        anchor = _anchor(query, all_facts)
        links = [fact for fact in all_facts if fact.subject == anchor and fact.kind == "uses"]
        targets = {fact.value for fact in links}
        entities = sorted({anchor} - {None} | targets)
        rows = []
        for goal in goals:
            if CONSTRAINED.search(query):
                rows.append(GoalCoverage(goal.goal_id, goal.kind, "partial", reason="time_or_version_requires_review"))
                continue
            if goal.kind == "generic":
                rows.append(GoalCoverage(goal.goal_id, goal.kind, "partial" if candidates else "missing",
                                         reason="unsupported_question_type"))
                continue
            direct = [fact for fact in all_facts if fact.subject == anchor and fact.kind == goal.kind]
            indirect = [fact for fact in all_facts if fact.subject in targets and fact.kind == goal.kind]
            fault_constraint = _fault_constraint(query, anchor) if goal.kind == "procedure" else None
            if fault_constraint:
                allowed = {fact.support.key for fact in all_facts if fact.kind == "fault"
                           and fault_constraint in fact.value}
                direct = [fact for fact in direct if fact.support.key in allowed]
                indirect = [fact for fact in indirect if fact.support.key in allowed]
            matched = direct or indirect
            supports = [fact.support for fact in matched]
            if indirect and not direct:
                supports = [fact.support for fact in links] + supports
            # A relation with two possible targets is not resolved by ranking or majority vote.
            conflicting = len({fact.value for fact in matched}) > 1 or (bool(indirect) and len(targets) > 1)
            if matched:
                unique = {(item.key, item.quote): item for item in supports}
                rows.append(GoalCoverage(goal.goal_id, goal.kind, "conflicting" if conflicting else "supported",
                                         list(unique.values()), "explicit_local_facts"))
            else:
                rows.append(GoalCoverage(goal.goal_id, goal.kind, "missing", reason="no_explicit_support"))
        return Coverage(goals, rows, entities)

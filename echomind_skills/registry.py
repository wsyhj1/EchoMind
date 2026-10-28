"""Deterministic, versioned EchoMind skills; independent of UI, models and tools."""
from dataclasses import dataclass, field
from hashlib import sha256
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Literal


class SkillCommandError(ValueError):
    """User-visible command/registration error; never guesses another skill."""


@dataclass(frozen=True)
class ResolvedCommand:
    skill_id: str
    argument: str
    kind: Literal["agent", "local"]
    action: str
    version: str


@dataclass(frozen=True)
class Skill:
    id: str
    name: str
    description: str
    aliases: tuple[str, ...]
    examples: tuple[str, ...]
    kind: Literal["agent", "local"]
    requires_input: bool
    default_input: str
    action: str
    version: str
    workflow: tuple[str, ...] = field(repr=False)


_FIELDS = frozenset({"id", "name", "description", "aliases", "examples", "kind", "requires_input", "default_input", "action", "workflow"})
_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")
_ALIAS = re.compile(r"(?:[a-zA-Z][a-zA-Z0-9_-]{0,31}|[\u3400-\u9fff]{1,16})\Z")


def _text(value, field_name, maximum, *, empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise SkillCommandError(f"技能目录字段无效：{field_name}")
    return value


class SkillRegistry:
    def __init__(self, catalog):
        if not isinstance(catalog, dict) or set(catalog) != {"version", "policy", "skills"}:
            raise SkillCommandError("技能目录需要version、policy、skills，且不能包含未知字段")
        declared_version = _text(catalog["version"], "version", 64)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", declared_version):
            raise SkillCommandError("技能目录版本格式无效")
        self._policy = _text(catalog["policy"], "policy", 6000)
        rows = catalog["skills"]
        if not isinstance(rows, list) or not 1 <= len(rows) <= 128:
            raise SkillCommandError("技能目录需要1至128项声明")
        # Includes the shared policy and every workflow; metadata-only hashes could
        # otherwise silently keep an old trace version after instruction changes.
        try:
            canonical = json.dumps(catalog, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            raise SkillCommandError("技能目录必须是可序列化的JSON声明") from None
        self.sha256 = sha256(canonical.encode("utf-8")).hexdigest()
        self.declared_version = declared_version
        self.version = f"{declared_version}:{self.sha256[:16]}"
        skills, aliases = {}, {}
        for row in rows:
            if not isinstance(row, dict) or set(row) != _FIELDS:
                raise SkillCommandError("技能声明字段缺失或包含未知字段")
            skill_id = _text(row["id"], "id", 32)
            if not _IDENTIFIER.fullmatch(skill_id) or skill_id in skills:
                raise SkillCommandError(f"技能id无效或重复：{skill_id}")
            name = _text(row["name"], "name", 64)
            description = _text(row["description"], "description", 500)
            kind = row["kind"]
            if kind not in ("agent", "local") or type(row["requires_input"]) is not bool:
                raise SkillCommandError(f"技能类型或输入声明无效：{skill_id}")
            default = _text(row["default_input"], "default_input", 1000, empty=True)
            action = row["action"]
            if (kind == "agent" and action != "run") or (kind == "local" and action not in ("help", "settings", "new_chat")):
                raise SkillCommandError(f"技能动作不允许：{skill_id}")
            if row["requires_input"] and default:
                raise SkillCommandError(f"必填输入不能设置默认值：{skill_id}")
            if kind == "local" and (row["requires_input"] or default):
                raise SkillCommandError(f"本地命令不能请求模型输入：{skill_id}")
            names = row["aliases"]
            if not isinstance(names, list) or not 1 <= len(names) <= 16:
                raise SkillCommandError(f"技能别名列表无效：{skill_id}")
            seen = set()
            for alias in names:
                if not isinstance(alias, str) or not _ALIAS.fullmatch(alias) or alias.casefold() in seen:
                    raise SkillCommandError(f"技能别名无效或重复：{skill_id}")
                seen.add(alias.casefold())
            for alias in (skill_id, *names):
                normalized = alias.casefold()
                owner = aliases.get(normalized)
                if owner is not None and owner != skill_id:
                    raise SkillCommandError(f"技能别名冲突：{alias}")
                aliases[normalized] = skill_id
            examples = row["examples"]
            if not isinstance(examples, list) or not 1 <= len(examples) <= 8:
                raise SkillCommandError(f"技能示例列表无效：{skill_id}")
            own_names = {alias.casefold() for alias in (skill_id, *names)}
            for example in examples:
                _text(example, "examples", 1000)
                parts = example.split(maxsplit=1)
                if example[0] not in "/&" or parts[0][1:].casefold() not in own_names:
                    raise SkillCommandError(f"示例必须使用本技能完整命令：{skill_id}")
                if row["requires_input"] and len(parts) != 2:
                    raise SkillCommandError(f"技能示例缺少必填输入：{skill_id}")
                if kind == "local" and len(parts) != 1:
                    raise SkillCommandError(f"本地命令示例不能带参数：{skill_id}")
            workflow = row["workflow"]
            if not isinstance(workflow, list) or len(workflow) > 16 or (kind == "agent" and not workflow) or (kind == "local" and workflow):
                raise SkillCommandError(f"技能工作流与类型不匹配：{skill_id}")
            for step in workflow:
                _text(step, "workflow", 2000)
            skills[skill_id] = Skill(skill_id, name, description, tuple(names), tuple(examples), kind,
                                     row["requires_input"], default, action, self.version, tuple(workflow))
        self._skills = MappingProxyType(skills)
        self._aliases = MappingProxyType(aliases)

    @classmethod
    def from_file(cls, path):
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def get_skill(self, skill_id):
        """Internal immutable definition, addressed only by its canonical ID."""
        if not isinstance(skill_id, str) or skill_id not in self._skills:
            raise SkillCommandError("技能不存在，请使用/help查看已注册技能")
        return self._skills[skill_id]

    def public_skills(self):
        """Fresh allowlisted metadata; never include policy/workflow/version/action."""
        return [{"id": skill.id, "name": skill.name, "description": skill.description,
                 "aliases": list(skill.aliases), "examples": list(skill.examples), "kind": skill.kind,
                 "requires_input": skill.requires_input, "default_input": skill.default_input}
                for skill in self._skills.values()]

    def resolve_command(self, text):
        if not isinstance(text, str):
            raise TypeError("命令输入必须是文本")
        value = text.lstrip()
        if not value or value[0] not in "/&" or value.startswith(("//", "&&")):
            return None
        if len(value) == 1 or value[1].isspace():
            raise SkillCommandError("命令前缀后需要直接写完整名称，例如/help")
        parts = value[1:].split(maxsplit=1)
        if not parts:
            raise SkillCommandError("请输入完整命令，例如/help")
        name = parts[0]
        # Leading URL/file paths remain text, never a fuzzy /summary prefix hit.
        if value[0] == "/" and any(mark in name for mark in ("/", "\\", "?", "#", ":", ".")):
            return None
        skill_id = self._aliases.get(name.casefold())
        if skill_id is None:
            raise SkillCommandError("未知技能命令，请使用/help查看完整命令；若要输入普通前缀文本，可使用//或&&转义")
        skill = self._skills[skill_id]
        argument = parts[1].strip() if len(parts) == 2 else ""
        if skill.kind == "local" and argument:
            raise SkillCommandError(f"/{name}是本地命令，不接受附加文本")
        if skill.requires_input and not argument:
            raise SkillCommandError(f"{skill.name}需要补充任务，例如：{skill.examples[0]}")
        if not argument:
            argument = skill.default_input
        return ResolvedCommand(skill.id, argument, skill.kind, skill.action, self.version)

    def build_skill_prompt(self, skill_id):
        skill = self.get_skill(skill_id)
        if skill.kind != "agent":
            raise SkillCommandError("本地命令由界面处理，不使用模型工作流")
        steps = "\n".join(f"{index}. {step}" for index, step in enumerate(skill.workflow, 1))
        return f"【EchoMind技能：{skill.name}】\n{self._policy}\n【工作流】\n{steps}"


_REGISTRY = SkillRegistry.from_file(Path(__file__).with_name("catalog.json"))


def public_skills():
    return _REGISTRY.public_skills()


def resolve_command(text):
    return _REGISTRY.resolve_command(text)


def get_skill(skill_id):
    return _REGISTRY.get_skill(skill_id)


def build_skill_prompt(skill_id):
    return _REGISTRY.build_skill_prompt(skill_id)


def registry_metadata():
    return {"version": _REGISTRY.version, "declared_version": _REGISTRY.declared_version, "sha256": _REGISTRY.sha256}

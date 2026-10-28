"""Public skill metadata and an enhancer for the existing native chat input."""
from pathlib import Path
import re

import streamlit as st
import streamlit.components.v2 as components

PUBLIC_FIELDS = ("id", "name", "description", "aliases", "kind", "requires_input", "default_input", "examples")


def _text(value, limit):
    return value[:limit] if isinstance(value, str) else ""


def public_catalog(catalog):
    """Only metadata goes to the browser; instructions and callables never do."""
    result, seen = [], set()
    for item in catalog if isinstance(catalog, (list, tuple)) else []:
        if not isinstance(item, dict):
            continue
        skill_id = _text(item.get("id"), 64).lstrip("/&")
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", skill_id) or skill_id in seen:
            continue
        seen.add(skill_id)
        aliases = item.get("aliases", [])
        examples = item.get("examples", [])
        result.append({"id": skill_id, "name": _text(item.get("name"), 80) or skill_id,
                       "description": _text(item.get("description"), 320),
                       "aliases": [_text(a, 80).lstrip("/&") for a in aliases[:16] if isinstance(a, str)] if isinstance(aliases, (list, tuple)) else [],
                       "kind": _text(item.get("kind"), 32), "requires_input": item.get("requires_input") is not False,
                       "default_input": _text(item.get("default_input"), 320),
                       "examples": [_text(e, 320) for e in examples[:4] if isinstance(e, str)] if isinstance(examples, (list, tuple)) else []})
        if len(result) >= 100:
            break
    return result


def _register_renderer(javascript):
    return components.component("echomind_skill_palette", js=javascript,
                                html='<span aria-hidden="true" style="display:none"></span>',
                                isolate_styles=False)


def _get_renderer():
    # Register in the current Streamlit runtime. A renderer cached across runtimes
    # can reference a component absent from a new app/session manager.
    return _register_renderer(Path(__file__).with_name("skill_palette.js").read_text("utf-8"))


def render_skill_palette(catalog, disabled=False):
    metadata = public_catalog(catalog)
    return _get_renderer()(key="echomind_skill_palette", height=0,
                           data={"catalog": metadata, "disabled": bool(disabled) or not metadata})


def render_skill_help(catalog, *, closable=True):
    metadata = public_catalog(catalog)
    with st.expander("技能与命令", expanded=bool(st.session_state.get("skills_help_open", False))):
        st.caption("在输入框开头输入 / 或 & 查找技能。↑↓选择，Enter或Tab填入命令，Esc关闭；选择不会发送消息。")
        query = st.text_input("查找技能", key="skill_help_query", placeholder="按命令、中文名称或别名查找")
        needle = query.strip().lstrip("/&").casefold()
        matches = [s for s in metadata if not needle or any(needle in text.casefold() for text in [s["id"], s["name"], *s["aliases"]])]
        if matches:
            st.dataframe([{"命令": f"/{s['id']} · &{s['id']}", "名称": s["name"], "说明": s["description"],
                           "别名": "、".join(s["aliases"]), "需要内容": "是" if s["requires_input"] else "否",
                           "示例": s["examples"][0] if s["examples"] else ""} for s in matches],
                         hide_index=True, width="stretch")
        else:
            st.info("没有匹配的技能。" if metadata else "当前没有可用技能。")
        if closable and st.session_state.get("skills_help_open") and st.button("关闭技能说明", key="skills_help_close"):
            st.session_state.skills_help_open = False
            st.rerun()

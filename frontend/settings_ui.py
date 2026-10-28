"""One settings entry point; all editable preferences are persisted by the API."""
from api_client import api_requests as requests
from operations_ui import render_operations
import streamlit as st

from profile_ui import FIELD_LABELS, load_profile, render_profile_editor, _change_setting

SECTIONS = ["RAG 设置", "画像设置", "对话与记忆", "技能与命令", "模型与服务"]
GRAPH_CHOICES = {"跟随服务默认": None, "开启": True, "关闭": False}


def open_settings(section="RAG 设置", *, profile_prepared=False):
    st.session_state.settings_open = True
    st.session_state.settings_nav = section
    st.session_state.settings_profile_prepared = profile_prepared
    st.session_state.profile_dialog_open = False
    if section != "画像设置":
        st.session_state.pop("settings_data", None)
    _clear_drafts()


def _close_settings():
    st.session_state.settings_open = False


def _change_section():
    st.session_state.settings_profile_prepared = False
    _clear_drafts()


def _clear_drafts():
    for key in ("settings_mode", "settings_top_k", "settings_rerank", "settings_graph", "settings_evidence", "settings_memory"):
        st.session_state.pop(key, None)


def _load_settings(api_base, user_id):
    if st.session_state.get("settings_data") is None:
        response = requests.get(f"{api_base}/user-settings", params={"user_id": user_id}, timeout=5)
        response.raise_for_status()
        st.session_state.settings_data = response.json()
    st.session_state.settings_summary_data = st.session_state.settings_data
    return st.session_state.settings_data


def _save(api_base, user_id, saved, changes):
    try:
        response = requests.put(f"{api_base}/user-settings", params={"user_id": user_id}, timeout=8,
                                json={"values": {**saved["values"], **changes}, "expected_revision": saved["revision"]})
        response.raise_for_status()
        st.session_state.settings_data = response.json()
        st.session_state.settings_summary_data = st.session_state.settings_data
        st.session_state.settings_notice = "设置已保存，将从下一次提问起生效。"
        st.session_state.settings_reset_drafts = True
        st.rerun()
    except requests.RequestException as error:
        if error.response is not None and error.response.status_code == 409:
            # Preserve the draft, but require an explicit refresh before retrying.
            st.warning("设置已在其他页面更新。当前草稿仍保留，请重新加载并核对后再保存。")
        else:
            st.error("设置未保存，请检查后端服务后重试。")


def _rag_settings(api_base, user_id, saved):
    st.subheader("RAG 设置")
    st.caption("调整知识库检索方式。设置适用于你的所有知识库，新提问会使用最新保存的配置。")
    values = saved["values"]
    for key, value in {"settings_mode": values["retrieval_mode"], "settings_top_k": values["top_k"],
                       "settings_rerank": values["rerank_enabled"],
                       "settings_graph": next(label for label, value in GRAPH_CHOICES.items()
                                              if value is values["graph_enabled"]),
                       "settings_evidence": next(label for label, value in GRAPH_CHOICES.items()
                                                 if value is values.get("evidence_enabled"))}.items():
        st.session_state.setdefault(key, value)
    with st.form("rag_settings_form"):
        mode = st.selectbox("检索方式", ["hybrid", "dense"], key="settings_mode",
                            format_func=lambda value: {"hybrid": "混合检索 · 语义 + 关键词", "dense": "语义检索"}[value],
                            help="混合检索同时照顾含义相近的内容和专有名词；语义检索侧重问题与文档的含义。")
        top_k = st.number_input("参考片段数量（Top K）", min_value=1, max_value=20, step=1, key="settings_top_k",
                                help="送给模型的片段数量上限。增加数量可能补充证据，也会增加上下文长度。")
        rerank = st.toggle("检索结果重排", key="settings_rerank",
                           help="使用重排模型再次筛选相关片段，通常有助于相关性，也会增加一次模型调用。")
        graph = st.selectbox("图谱辅助召回", list(GRAPH_CHOICES), key="settings_graph",
                             help="通过实体关系补充跨文档证据；需要知识库已完成图谱索引。")
        runtime = saved["runtime"]
        st.caption(f"服务默认图谱召回：{'开启' if runtime['graph_default_enabled'] else '关闭'}。"
                   "缺少图谱索引、图谱不可用或查询超时时，会继续使用常规检索。")
        if not runtime["graph_extraction_enabled"]:
            st.info("当前服务未开启上传时自动构建图谱。开启召回可使用已有索引；新增文档需要先由服务端构建索引。")
        evidence = st.selectbox("证据完整性检查（实验）", list(GRAPH_CHOICES), key="settings_evidence",
                                help="保守检查中文运维与关系问题，最多补检一次；其他问题和时间版本限定保持不确定。")
        st.caption(f"服务默认：{'开启' if runtime.get('evidence_default_enabled', False) else '关闭'}。"
                   "检查本身不调用模型；发现缺失时可能增加一次检索和重排。")
        save_col, reset_col = st.columns(2)
        submit = save_col.form_submit_button("保存 RAG 设置", type="primary", width="stretch")
        reset = reset_col.form_submit_button("恢复 RAG 默认值", width="stretch")
    keys = ("retrieval_mode", "top_k", "rerank_enabled", "graph_enabled", "evidence_enabled")
    if submit:
        _save(api_base, user_id, saved, dict(zip(keys, (mode, int(top_k), rerank, GRAPH_CHOICES[graph], GRAPH_CHOICES[evidence]))))
    if reset:
        _save(api_base, user_id, saved, {key: saved["defaults"].get(key) for key in keys})


def _profile_settings(api_base, user_id):
    st.subheader("画像设置")
    if not st.session_state.get("settings_profile_prepared"):
        try:
            profile = load_profile(api_base, user_id, force=True)
        except requests.RequestException:
            st.error("画像暂时无法加载，请稍后重试。")
            return
        for key in FIELD_LABELS:
            st.session_state[f"profile_field_{key}"] = profile["fields"].get(key, "")
        st.session_state.profile_editor_revision = profile["revision"]
        st.session_state.confirm_clear_profile = False
        st.session_state.settings_profile_prepared = True
    profile = st.session_state.profile_data
    st.session_state.settings_profile_use = profile["personalization_enabled"]
    st.toggle("在回答中使用我的画像", key="settings_profile_use", on_change=_change_setting,
              args=(api_base, user_id, "personalization_enabled", "settings_profile_use"))
    render_profile_editor(api_base, user_id)


def _memory_settings(api_base, user_id, saved):
    st.subheader("对话与记忆")
    st.session_state.setdefault("settings_memory", saved["values"]["memory_enabled"])
    with st.form("memory_settings_form"):
        enabled = st.toggle("使用长期记忆与画像", key="settings_memory")
        st.caption("开启后，助手可检索长期记忆，并按画像开关决定是否使用你的画像。"
                   "关闭后，两者均不参与新回答；当前会话上下文、历史保存和后台整理仍保留。")
        st.caption("自动更新画像摘要可在「画像设置 → 设置」中单独控制。")
        submit = st.form_submit_button("保存记忆设置", type="primary")
    if submit:
        _save(api_base, user_id, saved, {"memory_enabled": enabled})


@st.dialog("设置", width="large", on_dismiss=_close_settings)
def _settings_dialog(api_base, user_id):
    if st.session_state.pop("settings_reset_drafts", False):
        _clear_drafts()
    navigation, content = st.columns([1, 3], gap="large")
    with navigation:
        with st.container(key="settings_navigation"):
            st.radio("设置分类", SECTIONS, key="settings_nav", label_visibility="collapsed", on_change=_change_section)
            st.caption("EchoMind\n\n个人知识工作台")
            if st.button("完成", key="close_settings", width="stretch"):
                _close_settings()
                st.rerun()
    with content:
        if st.session_state.get("settings_notice"):
            st.success(st.session_state.pop("settings_notice"))
        section = st.session_state.settings_nav
        if section == "技能与命令":
            from skills_ui import render_skill_help
            from echomind_skills import public_skills
            st.session_state.skills_help_open = True
            render_skill_help(public_skills(), closable=False)
            return
        if section == "画像设置":
            _profile_settings(api_base, user_id)
            return
        try:
            saved = _load_settings(api_base, user_id)
        except requests.RequestException:
            st.error("设置暂时无法加载，请确认后端服务已启动并更新。")
            if st.button("重试加载设置", key="retry_settings"):
                st.rerun()
            return
        if st.button("重新加载已保存设置", key="reload_settings", type="tertiary", icon=":material/refresh:"):
            st.session_state.pop("settings_data", None)
            st.session_state.settings_reset_drafts = True
            st.rerun()
        if section == "RAG 设置":
            _rag_settings(api_base, user_id, saved)
        elif section == "对话与记忆":
            _memory_settings(api_base, user_id, saved)
        else:
            st.subheader("模型与服务")
            st.caption("当前服务的模型配置。模型连接由服务端统一管理。")
            for label, key in [("对话模型", "agent_model"), ("向量模型", "embedding_model"), ("重排模型", "rerank_model")]:
                st.text(f"{label}：{saved['runtime'].get(key) or '未配置'}")
            render_operations(api_base,user_id)
        st.caption(f"已保存版本 v{saved['revision']} · 配置保存在服务端，刷新页面后仍有效。")


def render_settings_button(*, disabled=False):
    st.button("设置", key="open_settings", icon=":material/settings:", width="stretch",
              disabled=disabled, on_click=open_settings, help="设置 RAG、画像、对话与记忆")


def render_settings_summary(api_base, user_id, *, disabled=False):
    """Compact shortcuts reflect saved preferences; editing stays in settings."""
    with st.container(key="settings_summary"):
        st.markdown('<div class="section-label">回答配置 <span>PREFERENCES</span></div>', unsafe_allow_html=True)
        try:
            saved = (st.session_state.get("settings_summary_data") if st.session_state.get("settings_open")
                     else _load_settings(api_base, user_id))
        except requests.RequestException:
            st.caption("检索配置暂不可用，可通过齿轮设置重试。")
            saved = None
        if saved is not None:
            values = saved["values"]
            mode = {"hybrid": "混合检索", "dense": "语义检索"}[values["retrieval_mode"]]
            graph = values["graph_enabled"] if values["graph_enabled"] is not None else saved["runtime"]["graph_default_enabled"]
            evidence = values.get("evidence_enabled")
            if evidence is None:
                evidence = saved["runtime"].get("evidence_default_enabled", False)
            st.button(f"{mode} · Top {values['top_k']}", key="open_rag", icon=":material/manage_search:",
                      width="stretch", type="tertiary", disabled=disabled, on_click=open_settings,
                      args=("RAG 设置",), help="查看与修改 RAG 设置")
            st.markdown('<div class="preference-status">'
                        f'<span>重排{"开启" if values["rerank_enabled"] else "关闭"}</span>'
                        f'<span>图谱{"开启" if graph else "关闭"}</span>'
                        f'<span>证据检查{"开启" if evidence else "关闭"}</span></div>', unsafe_allow_html=True)
        try:
            profile = load_profile(api_base, user_id)
        except requests.RequestException:
            profile_label = "画像暂不可用"
        else:
            if _memory_disabled(saved):
                profile_label = "画像 · 随记忆停用"
            elif not profile["personalization_enabled"]:
                profile_label = "画像 · 已停用"
            elif profile["source"] == "empty":
                profile_label = "画像 · 未设置"
            else:
                profile_label = "画像 · " + {"manual": "使用手动画像", "automatic": "使用自动摘要"}[profile["source"]]
        st.button(profile_label, key="open_profile", icon=":material/person_outline:",
                  width="stretch", type="tertiary", disabled=disabled, on_click=open_settings,
                  args=("画像设置",), help="打开画像设置，编辑个人信息与回答偏好")
        if _memory_disabled(saved):
            st.caption("长期记忆与画像已停用")
        if st.session_state.get("profile_notice"):
            st.warning(st.session_state.pop("profile_notice"))


def _memory_disabled(saved):
    return saved is not None and not saved["values"]["memory_enabled"]


def render_settings_dialog(api_base, user_id):
    if st.session_state.get("settings_open"):
        _settings_dialog(api_base, user_id)

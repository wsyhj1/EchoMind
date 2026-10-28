"""Native clickable evidence controls; document text never becomes HTML."""
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import streamlit as st
from api_client import api_requests as requests

STATUS = {
    "structure_verified": "引用结构与当前来源通过；语义未核实",
    "pair_mismatch": "文件名与证据编号不匹配",
    "unverified": "未核实：缺少完整配对或实际可见来源",
    "ambiguous": "未核实：同一配对存在多个来源",
    "source_deleted": "来源已删除或当前不可用",
    "source_changed": "来源已变化，不能用当前原文代替历史引文",
    "deletion_pending": "来源正在删除，暂不展示原文",
}


def _local_time(value):
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
    except (AttributeError, TypeError, ValueError):
        return "未记录"


def _load(run_id, api_base, user_id):
    response = requests.get(f"{api_base}/runs/{run_id}/citations", params={"user_id": user_id}, timeout=8)
    response.raise_for_status()
    return response.json()


def render_answer_citations(chat, api_base, user_id):
    run_id = chat.get("run_id")
    if not run_id or chat.get("persisted") is False:
        return
    answer = chat.get("content", "")
    labels = list(dict.fromkeys(re.findall(r"(?<![A-Za-z0-9_])E[1-9]\d*(?![A-Za-z0-9_])", answer)))
    if not labels and not re.search(r"来源\s*[:：]", answer):
        return
    key = f"citations_{user_id}_{run_id}"
    state = st.session_state.setdefault(key, {})
    if "snapshot" not in state:
        # These labels come from the answer; no source is inferred before the API check.
        for label in labels or ["核验来源"]:
            if st.button(f"{label} · 查看引文", key=f"{key}_open_{label}", type="tertiary"):
                try:
                    state["snapshot"] = _load(run_id, api_base, user_id)
                    refs = state["snapshot"].get("citations", [])
                    match = next((r for r in refs if r.get("evidence_id") == label), refs[0] if refs else None)
                    state["selected"] = match["citation_id"] if match else None
                except requests.RequestException:
                    st.warning("引用暂时无法核验，请稍后重试。")
    if "snapshot" in state:
        render_citation_browser(run_id, api_base, user_id, state, key)


def render_citation_browser(run_id, api_base, user_id, state=None, key=None):
    key = key or f"citation_browser_{user_id}_{run_id}"
    state = state if state is not None else st.session_state.setdefault(key, {})
    if st.button("刷新引用核验", key=f"{key}_refresh", icon=":material/refresh:"):
        try:
            state["snapshot"] = _load(run_id, api_base, user_id)
        except requests.RequestException:
            state.pop("snapshot", None)
            st.warning("当前无法核对来源，暂不展示历史缓存原文。")
    if "snapshot" not in state:
        st.caption("点击刷新，核对本轮实际引文与当前来源状态。")
        return
    snapshot = state["snapshot"]
    st.caption("此处只校验引用结构、实际模型可见原文和来源是否仍存在，不判断回答结论是否成立。")
    statistics = snapshot.get("statistics", {})
    st.caption(f"{statistics.get('citation_occurrences', 0)} 处引用 · 审计快照 {snapshot.get('validation_id', '未记录')}")
    refs = snapshot.get("citations", [])
    if not refs:
        st.info("本回答没有可核验的引用配对；不会自动补写或猜测来源。")
        return
    for ref in refs:
        label = ref.get("evidence_id") or "缺少编号"
        if st.button(f"{label} · 引用 {ref['citation_id'][1:]}", key=f"{key}_{ref['citation_id']}", type="tertiary"):
            state["selected"] = ref["citation_id"]
        st.caption(STATUS.get(ref.get("status"), "未核实"))
    selected = state.get("selected")
    if not selected:
        return
    with st.expander("所选引用原文", expanded=True):
        if st.button("关闭原文", key=f"{key}_close"):
            state.pop("selected", None)
            return
        # Every display rechecks source existence. Plaintext is never session-cached.
        try:
            response = requests.get(f"{api_base}/runs/{run_id}/citations/{selected}",
                                    params={"user_id": user_id}, timeout=8)
            response.raise_for_status()
            detail = response.json()
            ref = detail["citation"]
            st.text(ref.get("file") or "未记录文件名")
            if ref.get("status") == "structure_verified" and isinstance(detail.get("text"), str):
                st.info("语义未核实：请对照原文检查结论、时间、实例和完整关系链。")
                st.text(detail["text"])
            else:
                st.warning(STATUS.get(ref.get("status"), "未核实"))
                st.caption("未展示原文，也未用其他文件替换这条引用。")
            st.caption(f"本次核验（北京时间）：{_local_time(detail.get('checked_at'))}")
        except requests.RequestException:
            st.warning("当前无法核对来源，暂不展示历史缓存原文。")

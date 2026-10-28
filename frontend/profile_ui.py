"""A small user-owned profile panel; viewing/editing never calls a model."""
from datetime import datetime
import time
from zoneinfo import ZoneInfo

from api_client import api_requests as requests
import streamlit as st

FIELD_LABELS = {"background": "背景与经验", "goals": "当前目标", "interests": "关注领域", "preferences": "回答偏好"}
SOURCE_LABELS = {"manual": "手动填写", "automatic": "自动整理 · 待核实", "empty": "尚未建立画像"}


def _remember(profile):
    st.session_state.profile_data = profile
    st.session_state.profile_loaded_at = time.monotonic()
    return profile


def load_profile(api_base, user_id, *, force=False):
    if not force and st.session_state.get("profile_data") is not None and time.monotonic() - st.session_state.get("profile_loaded_at", 0) < 30:
        return st.session_state.profile_data
    response = requests.get(f"{api_base}/user-profile", params={"user_id": user_id}, timeout=5)
    response.raise_for_status()
    return _remember(response.json())


def _updated(profile):
    if not profile.get("updated_at"):
        return "历史摘要 · 更新时间未记录" if profile.get("automatic_summary") else "还没有画像更新记录"
    return "最近更新 " + datetime.fromisoformat(profile["updated_at"]).astimezone(ZoneInfo("Asia/Shanghai")).strftime("%m-%d %H:%M")


def _change_setting(api_base, user_id, setting, widget_key):
    previous = st.session_state.profile_data
    try:
        response = requests.patch(f"{api_base}/user-profile/settings", params={"user_id": user_id},
                                  json={setting: st.session_state[widget_key], "expected_revision": previous["revision"]}, timeout=8)
        response.raise_for_status()
        profile = _remember(response.json())
        if st.session_state.get("profile_editor_revision") == previous["revision"]:
            st.session_state.profile_editor_revision = profile["revision"]
    except requests.RequestException as error:
        st.session_state[widget_key] = previous[setting]
        if error.response is not None and error.response.status_code == 409:
            st.session_state.profile_notice = "画像已在其他地方更新，请重新打开面板后操作。"
            try:
                load_profile(api_base, user_id, force=True)
            except requests.RequestException:
                pass
        else:
            st.session_state.profile_notice = "设置未保存，请检查服务后重试。"


def _dismiss_profile():
    st.session_state.profile_dialog_open = False
    st.session_state.settings_open = False


def render_profile_editor(api_base, user_id):
    profile = st.session_state.profile_data
    st.caption(f"{SOURCE_LABELS[profile['source']]} · v{profile['revision']} · {_updated(profile)}")
    editor, automatic, settings = st.tabs(["我的画像", "自动整理", "设置"])
    with editor:
        st.caption("填写后，回答使用你手动设置的内容，自动摘要不参与画像注入。四项均留空时，使用已有自动摘要。")
        with st.form("profile_editor"):
            fields = {}
            for col, keys in zip(st.columns(2), [("background", "goals"), ("interests", "preferences")]):
                with col:
                    for key in keys:
                        fields[key] = st.text_area(FIELD_LABELS[key], key=f"profile_field_{key}", max_chars=2000, height=100,
                                                  placeholder={"background": "例如：熟悉 Python，正在学习 RAG",
                                                               "goals": "希望通过这个系统完成什么？",
                                                               "interests": "经常关注的主题或领域",
                                                               "preferences": "例如：先给结论，再给可执行步骤"}[key])
            submitted = st.form_submit_button("保存画像", type="primary")
        if submitted:
            try:
                response = requests.put(f"{api_base}/user-profile", params={"user_id": user_id},
                                        json={"fields": fields, "expected_revision": st.session_state.profile_editor_revision}, timeout=8)
                response.raise_for_status()
                _remember(response.json())
                _dismiss_profile()
                st.toast("画像已保存，将从下一轮回答起生效。")
                st.rerun()
            except requests.RequestException as error:
                if error.response is not None and error.response.status_code == 409:
                    try:
                        latest = load_profile(api_base, user_id, force=True)
                        st.session_state.profile_editor_revision = latest["revision"]
                        st.warning("画像已更新，你填写的草稿仍保留。请核对最新内容后再次保存，或关闭并重新打开以加载最新内容。")
                        st.caption("服务器上的最新手动内容：")
                        st.text("\n".join(f"{label}：{latest['fields'][key]}" for key, label in FIELD_LABELS.items()
                                           if latest["fields"].get(key)) or "暂无手动内容。")
                    except requests.RequestException:
                        st.error("无法读取最新画像，请稍后重试。")
                else:
                    st.error("画像未保存，请检查服务后重试。")
    with automatic:
        st.caption("这是后台根据历史对话整理的摘要，可能包含推断。手动填写画像后，这段摘要仅供参考，不会覆盖你的设置。")
        if profile["automatic_summary"]:
            st.text(profile["automatic_summary"])
        else:
            st.info("还没有自动摘要。后台每 3 分钟检查一次，单个会话未整理内容超过约 1000 tokens 后才会提取。")
        st.caption("你可以将有用内容整理到「我的画像」，用明确填写的内容替代自动推断。")
    with settings:
        st.session_state.profile_auto_toggle = profile["auto_update_enabled"]
        st.toggle("自动更新画像摘要", key="profile_auto_toggle", on_change=_change_setting,
                  args=(api_base, user_id, "auto_update_enabled", "profile_auto_toggle"))
        st.caption("暂停后，后台不再更新画像摘要；历史对话保存和其他长期记忆整理仍会继续。")
        st.divider()
        st.caption("清空会删除当前手动画像与自动摘要，并暂停自动更新。历史对话、长期记忆和已有回答的评测快照仍保留。")
        confirmed = st.checkbox("确认清空当前画像", key="confirm_clear_profile")
        if st.button("清空画像并暂停自动更新", icon=":material/delete:", key="clear_profile", disabled=not confirmed):
            try:
                response = requests.delete(f"{api_base}/user-profile", params={"user_id": user_id,
                                            "expected_revision": st.session_state.profile_data["revision"]}, timeout=8)
                response.raise_for_status()
                _remember(response.json())
                _dismiss_profile()
                st.toast("当前画像已清空，自动更新已暂停。")
                st.rerun()
            except requests.RequestException:
                st.error("未能清空画像，请重新打开面板后重试。")
    if st.session_state.get("profile_notice"):
        st.warning(st.session_state.pop("profile_notice"))


def render_personalization_details(chat):
    snapshot = chat.get("personalization")
    if snapshot is None:
        st.info("此历史回答未记录画像注入信息。")
        return
    if snapshot.get("injected"):
        st.caption(f"已加入本轮提示词 · v{snapshot['revision']} · {SOURCE_LABELS.get(snapshot['source'], snapshot['source'])}")
        st.text(snapshot.get("profile_text", ""))
        st.caption("记录的是本轮实际加入提示词的画像，不代表模型一定遵循了所有偏好。后续修改不会改变此历史快照。")
    else:
        reason = {"empty": "本轮尚无可用画像。", "disabled": "本轮画像注入已关闭。", "pending": "本轮未执行画像注入。"}
        st.info(reason.get(snapshot.get("reason"), "本轮未注入画像。"))
    st.caption("画像用于调整表达方式，不作为知识库事实依据；长期记忆按对话需要独立检索。")

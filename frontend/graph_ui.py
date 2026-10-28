"""Read-only graph inspection until a scoped, explicitly consented submission."""
import hashlib
import streamlit as st
from api_client import api_requests as requests

STATUS = {"UNINDEXED": "尚未建图", "PENDING": "等待处理", "RUNNING": "正在建图",
          "READY": "建图完成", "FAILED": "建图失败", "STALE": "已取消或索引待更新"}


def _status(api_base, user_id, kb):
    response = requests.get(f"{api_base}/graph-status", params={"user_id": user_id, "knowledge_base_id": kb}, timeout=8)
    response.raise_for_status()
    return response.json()


def render_graph_management(api_base, user_id, knowledge_base_id, *, disabled=False):
    if not knowledge_base_id:
        return
    key = f"graph_{user_id}_{knowledge_base_id}"
    if st.button("管理知识图谱", key=f"{key}_open", icon=":material/hub:", disabled=disabled):
        st.session_state[f"{key}_visible"] = True
    if not st.session_state.get(f"{key}_visible"):
        return
    with st.expander("知识图谱管理", expanded=True):
        if knowledge_base_id == "默认知识库":
            st.info("请选择具体知识库，逐份文档查看或提交建图。")
            return
        st.caption("查看状态不发送文档。建图会将所选文档片段发送给下方模型服务。")
        st.button("刷新建图状态", key=f"{key}_refresh", icon=":material/refresh:")
        try:
            data = _status(api_base, user_id, knowledge_base_id)
        except requests.RequestException:
            st.warning("建图状态暂不可用，请检查服务后刷新。")
            return
        service = data.get("service", {})
        host, model = service.get("host"), service.get("model")
        target_id = service.get("target_id")
        st.text(f"模型：{model or '未配置'}\n服务主机：{host or '未配置'}")
        if not data.get("schema_ready"):
            st.warning("图谱表尚未初始化，暂不能提交任务。")
        documents = data.get("documents", [])
        if not documents:
            st.info("当前知识库没有可建图文档。")
            return
        st.dataframe([{"文件": d.get("file_name", "未命名"), "状态": STATUS.get(d.get("status"), d.get("status")),
                       "已处理父块": d.get("parents_completed", 0), "父块总数": d.get("parents_total", 0),
                       "可用关系": d.get("usable_relations", 0), "拒绝关系": d.get("rejected_relations", 0),
                       "错误": d.get("error_type") or ""} for d in documents], hide_index=True, width="stretch")
        selected_hash = st.selectbox("选择建图文档", [d["file_hash"] for d in documents],
                                     format_func=lambda h: next(d.get("file_name", h) for d in documents if d["file_hash"] == h),
                                     key=f"{key}_file", disabled=disabled)
        document = next(d for d in documents if d["file_hash"] == selected_hash)
        st.text(document.get("file_name", "未命名"))
        status = document.get("status", "UNINDEXED")
        st.caption(STATUS.get(status, status))
        completed, total = document.get("parents_completed", 0) or 0, document.get("parents_total", 0) or 0
        if total:
            st.progress(max(0.0, min(1.0, completed / total)), text=f"已处理 {completed}/{total} 个父块")
        else:
            st.caption("父块数量尚未统计。")
        st.caption(f"模型调用 {document.get('model_calls', 0)} 次 · 上限 {document.get('max_calls', data.get('limits', {}).get('max_calls', '未配置'))} 次")
        st.caption(f"任务父块上限：{document.get('max_parents', data.get('limits', {}).get('max_parents', '未配置'))}")
        st.dataframe([{"类型": "实体", "提取": document.get("raw_entities", 0), "可用": document.get("usable_entities", 0), "拒绝": document.get("rejected_entities", 0)},
                      {"类型": "关系", "提取": document.get("raw_relations", 0), "可用": document.get("usable_relations", 0), "拒绝": document.get("rejected_relations", 0)}],
                     hide_index=True, width="stretch")
        st.caption("READY表示任务完成；可用与拒绝数量单独列示，不代表全部抽取事实均可信。")
        if document.get("error_type"):
            st.text(f"任务错误：{document['error_type']}")
        if document.get("cancel_requested"):
            st.warning("已请求取消，将阻止后续模型请求；已发送的外部请求无法撤回。")
        if status in ("PENDING", "RUNNING"):
            if st.button("取消建图任务", key=f"{key}_cancel_{selected_hash}", disabled=disabled or document.get("cancel_requested", False)):
                try:
                    response = requests.post(f"{api_base}/graph-jobs/cancel", json={"user_id": user_id, "knowledge_base_id": knowledge_base_id, "file_hash": selected_hash}, timeout=8)
                    response.raise_for_status()
                    st.success("已提交取消请求。已发送给外部服务的数据无法撤回。")
                except requests.RequestException:
                    st.warning("取消未成功，请刷新核对任务状态。")
            return
        target = hashlib.sha256(f"{target_id}|{host}|{model}|{selected_hash}".encode()).hexdigest()[:16]
        force = st.checkbox("强制重建并覆盖当前图谱索引", key=f"{key}_force_{selected_hash}", disabled=disabled or status != "READY")
        force = bool(force) if status == "READY" else False
        consent = st.checkbox("我同意将这份文档片段发送给上述模型服务进行抽取", key=f"{key}_consent_{target}", disabled=disabled)
        can_submit = bool(consent and host and model and isinstance(target_id, str) and len(target_id) == 64
                          and data.get("schema_ready") and (status != "READY" or force))
        label = "重试建图" if status in ("FAILED", "STALE") else "开始建图"
        if st.button(label, key=f"{key}_submit_{selected_hash}", type="primary", disabled=disabled or not can_submit):
            try:
                response = requests.post(f"{api_base}/graph-jobs", json={"user_id": user_id,
                    "knowledge_base_id": knowledge_base_id, "file_hash": selected_hash,
                    "external_model_consent": True, "consent_target": target_id, "force": bool(force)}, timeout=8)
                response.raise_for_status()
                st.success("已提交建图任务，请刷新查看逐文档进度。")
            except requests.RequestException:
                st.warning("提交未成功，请刷新核对状态后重试。")

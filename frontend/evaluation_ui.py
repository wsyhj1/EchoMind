"""Compact answer metadata, with performance, evaluation and evidence on demand."""
from html import escape
import json
from api_client import api_requests as requests
import streamlit as st
from profile_ui import render_personalization_details
from citations_ui import render_citation_browser

LABELS = {
    "embedding": "查询向量化", "retrieval": "知识召回", "parent_fetch": "父块读取",
    "rerank": "重排序", "model": "所有模型调用", "generation": "最终回答生成",
    "memory": "记忆检索", "profile": "画像读取", "checkpoint": "会话检查点",
    "agent_setup": "Agent 准备", "history": "原始对话读取",
}
METRIC_LABELS = {
    "faithfulness": "Faithfulness 忠实度", "response_relevancy": "回答相关性",
    "context_precision": "上下文精度", "context_recall": "上下文召回",
    "factual_correctness": "答案事实正确性 F1", "precision_at_k": "Precision@K（证据 ID）",
    "recall_at_k": "Recall@K（证据 ID）",
}
STATUS_LABELS = {"queued": "已排队 · 等待评测进程", "running": "正在评测，请稍后刷新",
                 "completed": "评测已完成", "failed": "评测任务失败"}


def seconds(value):
    return "—" if value is None else f"{value / 1000:.2f}s"


def _run_snapshot(run_id, api_base, user_id):
    """Keep explicitly requested immutable snapshots visible across widget reruns."""
    cache = st.session_state.setdefault("run_detail_cache", {})
    if run_id not in cache:
        response = requests.get(f"{api_base}/runs/{run_id}", params={"user_id": user_id}, timeout=8)
        response.raise_for_status()
        cache[run_id] = response.json()
    return cache[run_id]


def _performance(chat, api_base, user_id):
    timings = chat.get("timings")
    if not timings:
        st.info("这条回答没有记录运行耗时。")
        return
    stages = timings.get("stages", {})
    for col, (label, value) in zip(st.columns(3), [
        ("回答耗时", timings.get("server_total_ms")), ("首字时间", timings.get("ttft_ms")),
        ("回答生成", stages.get("generation", {}).get("duration_ms")),
    ]):
        col.metric(label, seconds(value))
    rows = [{"阶段": label, "耗时（秒）": round(stages[kind]["duration_ms"] / 1000, 3),
             "调用次数": stages[kind]["calls"], "失败次数": int(stages[kind].get("failures", 0))}
            for kind, label in LABELS.items() if kind in stages]
    if rows:
        st.dataframe(rows, hide_index=True, width="stretch",
                     column_config={"耗时（秒）": st.column_config.NumberColumn(format="%.3f")})
    st.caption("仅列出已调用阶段。各阶段可能并行或嵌套，耗时不能相加；生成包含模型响应等待，回答耗时不含随后保存统计的时间。")
    if chat.get("client_total_ms") is not None:
        st.caption(f"本机本轮等待 {seconds(chat['client_total_ms'])} · 收到首字 {seconds(chat.get('client_ttft_ms'))}")
    run_id = chat.get("run_id")
    if run_id and st.button("展开调用时间线", icon=":material/timeline:", key=f"timeline_{run_id}"):
        try:
            _run_snapshot(run_id, api_base, user_id)
            st.session_state[f"show_timeline_{run_id}"] = True
        except requests.RequestException:
            st.info("调用详情暂不可用，请稍后重试。")
    if run_id and st.session_state.get(f"show_timeline_{run_id}"):
        snapshot = _run_snapshot(run_id, api_base, user_id)
        st.dataframe([{"阶段": LABELS.get(s["kind"], s["kind"]), "来源": s["source"],
                       "开始(ms)": round(s["start_ms"], 1), "耗时(ms)": round(s["duration_ms"] or 0, 1),
                       "状态": s["status"], "参数": json.dumps(s["attributes"], ensure_ascii=False)}
                      for s in snapshot["spans"]], hide_index=True, width="stretch")


def _evidence(run_id, api_base, user_id):
    with st.expander("核验回答引用与查看原文", expanded=False):
        render_citation_browser(run_id, api_base, user_id)
    st.caption("以下为生成当时的历史审计快照，不代表来源目前仍有效。当前来源状态请使用上方引用核验；父块 ID 可用于召回评测。")
    if st.button("加载本轮证据", key=f"evidence_{run_id}", icon=":material/library_books:"):
        try:
            _run_snapshot(run_id, api_base, user_id)
            st.session_state[f"show_evidence_{run_id}"] = True
        except requests.RequestException:
            st.info("证据详情暂不可用，请稍后重试。")
    if st.session_state.get(f"show_evidence_{run_id}"):
        evidence = [c for c in _run_snapshot(run_id, api_base, user_id)["contexts"]
                    if c["stage"] == "final" and c["source_type"] == "knowledge"]
        if evidence:
            st.dataframe([{"文件": c.get("file_name") or "未记录", "顺序": c["rank"],
                           "父块 ID": c["source_id"], "证据": c["text"]} for c in evidence],
                          hide_index=True, width="stretch")
        else:
            st.info("本轮没有新的知识库证据。")


def _job_result(job, run_id, api_base, user_id):
    st.caption(STATUS_LABELS.get(job["status"], job["status"]))
    if job.get("result"):
        scores = job["result"]["scores"]
        st.dataframe([
            {"指标": METRIC_LABELS.get(metric, metric), "分数": score.get("value"),
             "状态": {"completed": "完成", "failed": "失败", "not_applicable": "不适用"}.get(score["status"], score["status"]),
             "说明": score.get("error") or score.get("reason") or ("无参考答案代理指标" if score.get("proxy") else "")}
            for metric, score in scores.items()
        ], hide_index=True, width="stretch", column_config={
            "分数": st.column_config.ProgressColumn("分数", min_value=0, max_value=1, format="%.3f"),
        })
        st.caption(f"评测模型：{job['payload']['config']['model']}。分数不是答案正确的概率；未标注的指标显示不适用。")
        if any(name in scores for name in ("precision_at_k", "recall_at_k")):
            st.caption("Precision@K 按固定 K 作分母，少返回的位置按未命中计；Recall@K 按标注相关证据数作分母。")
    elif job.get("error"):
        st.warning(job["error"])
    retryable = job["status"] == "failed" or any(
        score["status"] == "failed" for score in (job.get("result") or {}).get("scores", {}).values())
    if retryable and job["attempts"] < 3 and st.button("重试失败指标", key=f"retry_{run_id}", help="会产生额外 API 用量"):
        response = requests.post(f"{api_base}/evaluations/{job['job_id']}/retry", params={"user_id": user_id}, timeout=10)
        response.raise_for_status()
        st.success("已重新排队，请稍后刷新。")


def _quality(chat, api_base, user_id):
    run_id = chat["run_id"]
    jobs = st.session_state.setdefault("evaluation_jobs", {})
    job_id = jobs.get(run_id) or chat.get("evaluation_job_id")
    if job_id:
        st.button("刷新结果", key=f"refresh_eval_{run_id}", icon=":material/refresh:")
        try:
            response = requests.get(f"{api_base}/evaluations/{job_id}", params={"user_id": user_id}, timeout=5)
            response.raise_for_status()
            _job_result(response.json(), run_id, api_base, user_id)
        except requests.RequestException:
            st.info("评测状态暂不可用，请稍后刷新。")
        if not st.toggle("配置新的评测", key=f"configure_eval_{run_id}"):
            return
        st.divider()
    st.caption("按需检查回答质量。提交后会将本轮问题、回答和证据发送给评测模型，并产生额外 API 用量。")
    with st.form(f"eval_{run_id}"):
        metrics = st.multiselect("评测指标", list(METRIC_LABELS), default=list(METRIC_LABELS)[:3],
                                 format_func=METRIC_LABELS.get, placeholder="选择需要评测的指标")
        reference = st.text_area("标准答案（可选）", max_chars=30000, height=90,
                                 placeholder="评测上下文召回、答案事实正确性时需要填写")
        ids = st.text_area("相关证据 ID（可选）", height=70,
                           placeholder="每行一个父块 ID，用于 Precision@K / Recall@K；可在「知识证据」中查看")
        required = st.checkbox("本问题需要知识库检索，未召回也计入失败")
        submitted = st.form_submit_button("开始评测", type="primary", disabled=not metrics)
    if submitted:
        try:
            response = requests.post(f"{api_base}/runs/{run_id}/evaluations", params={"user_id": user_id},
                                     json={"metrics": metrics, "reference": reference.strip() or None,
                                           "reference_context_ids": [s.strip() for s in ids.splitlines() if s.strip()] or None,
                                           "requires_retrieval": True if required else None}, timeout=10)
            response.raise_for_status()
            jobs[run_id] = response.json()["job_id"]
            st.rerun()
        except requests.RequestException:
            st.error("提交评测失败，请检查后端服务后重试。")


def render_run_details(chat, api_base, user_id):
    run_id = chat.get("run_id")
    timings = chat.get("timings")
    if timings:
        stages = timings.get("stages", {})
        parts = [f"耗时 {seconds(timings.get('server_total_ms'))}", f"首字 {seconds(timings.get('ttft_ms'))}"]
        for kind, label in [("embedding", "向量化"), ("retrieval", "召回"), ("generation", "生成")]:
            if kind in stages:
                parts.append(f"{label} {seconds(stages[kind]['duration_ms'])}")
        snapshot = chat.get("personalization")
        if snapshot is not None:
            parts.append("画像已注入" if snapshot.get("injected") else ("画像已关闭" if not snapshot.get("enabled") else "未注入画像"))
        st.markdown('<div class="answer-meta">' + ''.join(f'<span>{escape(part)}</span>' for part in parts) + '</div>',
                    unsafe_allow_html=True)
    persisted = bool(run_id) and chat.get("persisted") is not False
    if not timings and not persisted:
        return
    with st.expander("回答详情", icon=":material/tune:", expanded=False):
        if persisted:
            performance, quality, evidence, personalization = st.tabs(["运行表现", "质量评测", "知识证据", "个性化"])
            with performance:
                _performance(chat, api_base, user_id)
            with quality:
                _quality(chat, api_base, user_id)
            with evidence:
                _evidence(run_id, api_base, user_id)
            with personalization:
                render_personalization_details(chat)
        else:
            _performance(chat, api_base, user_id)
            render_personalization_details(chat)
            st.caption("本条统计未成功保存，质量评测与证据详情暂不可用。")

# app.py - 修复异步加载问题版本
import streamlit as st
import pandas as pd
from datetime import datetime
import os
import sys
import aiohttp
import asyncio
import nest_asyncio
import hashlib
import time
import json
import re
from html import escape
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo
from api_client import api_requests as requests, transport_headers, DEFAULT_API_BASE_URL
from identity_ui import bootstrap_identity, render_identity
from assistant_ui import render_assistant
from evaluation_ui import render_run_details
from citations_ui import render_answer_citations
from graph_ui import render_graph_management
from settings_ui import render_settings_button, render_settings_dialog, render_settings_summary, open_settings
from skills_ui import render_skill_palette, render_skill_help
from conversations_ui import (initialize_navigation, bootstrap_navigation, new_conversation,
    select_scope, prepare_turn, render_directory, render_legacy_history, render_conversation_controls,
    input_unavailable, load_conversation, invalidate_directory)
from typing import Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor
from threading import Thread
from queue import Queue, Empty

_project_root = str(Path(__file__).resolve().parents[1])
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
from echomind_skills import public_skills, resolve_command, SkillCommandError

SKILL_CATALOG = public_skills()

nest_asyncio.apply()

# 页面配置
st.set_page_config(
    page_title="EchoMind · 个人工作台",
    page_icon="✳️",
    layout="wide",
    initial_sidebar_state="auto"
)

# ==========================================
# 加载CSS（已分离到 style.css）
# ==========================================
def load_css():
    """从外部文件加载CSS样式"""
    css_content = Path(__file__).with_name("style.css").read_text(encoding="utf-8")
    return f"<style>{css_content}</style>"

st.markdown(load_css(), unsafe_allow_html=True)


# 后端API地址
# Backend listens on IPv4. On Windows, localhost may try ::1 first and spend
# ~2 seconds rejecting each connection before falling back to 127.0.0.1.
API_BASE_URL = os.getenv("ECHOMIND_API_BASE_URL", DEFAULT_API_BASE_URL)
USER_ID = bootstrap_identity(API_BASE_URL)

# ==========================================
# 初始化会话状态（修复版）
# ==========================================
def init_session_state():
    """初始化所有session state，避免KeyError"""
    defaults = {
        'chat_history': [],
        'knowledge_bases': {},    # 空字典，与加载完成后的格式一致
        'knowledge_base_files_info': {},
        'selected_kb': None,
        'pending_delete': None,
        'streaming': False,
        'current_response': "",
        'is_streaming': False,
        'initialized': False,
        'loading': True,
        'operation_in_progress': False,
        # 异步加载相关状态
        'data_loading': True,
        'kb_loading_status': {},
        'pending_operations': [],
        'task_inbox': Queue(),
        'last_refresh_time': 0,
        'optimistic_files': {},
        'pending_upload': None,
        'needs_rerun': False,
        'files_loaded': False,
        'load_error': None,  # 加载错误信息
        'skills_help_open': False,
        'skill_notice': None,
        'workspace_view': '工作台',
    }

    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value

init_session_state()
initialize_navigation()

# 延迟执行 rerun，确保在脚本执行完成后
if st.session_state.get('needs_rerun'):
    st.session_state.needs_rerun = False
    st.rerun()

# ==========================================
# 骨架屏渲染函数
# ==========================================

def render_sidebar_skeleton():
    """渲染侧边栏骨架屏"""
    st.markdown("""
    <div style="padding: 20px 0;">
        <div class="skeleton skeleton-title"></div>
        <div class="skeleton skeleton-text" style="width: 80%;"></div>
        <div style="margin: 20px 0;">
            <div class="skeleton skeleton-card"></div>
            <div class="skeleton skeleton-card"></div>
        </div>
        <div style="margin: 20px 0;">
            <div class="skeleton skeleton-title" style="width: 40%;"></div>
            <div class="kb-skeleton-item"></div>
            <div class="kb-skeleton-item"></div>
            <div class="kb-skeleton-item"></div>
        </div>
    </div>
    """, unsafe_allow_html=True)

def render_chat_skeleton():
    """渲染聊天区域骨架屏"""
    st.markdown("""
    <div style="padding: 40px 20px; max-width: 800px; margin: 0 auto;">
        <div style="display: flex; margin: 20px 0; align-items: flex-start;">
            <div class="skeleton skeleton-circle" style="margin-right: 12px; flex-shrink: 0;"></div>
            <div style="flex: 1;">
                <div class="skeleton skeleton-text" style="width: 90%;"></div>
                <div class="skeleton skeleton-text" style="width: 70%;"></div>
            </div>
        </div>
        <div style="display: flex; margin: 20px 0; align-items: flex-start; justify-content: flex-end;">
            <div style="flex: 1; margin-right: 12px;">
                <div class="skeleton skeleton-text" style="width: 80%; margin-left: auto;"></div>
                <div class="skeleton skeleton-text" style="width: 50%; margin-left: auto;"></div>
            </div>
            <div class="skeleton skeleton-circle" style="flex-shrink: 0;"></div>
        </div>
        <div style="display: flex; margin: 20px 0; align-items: flex-start;">
            <div class="skeleton skeleton-circle" style="margin-right: 12px; flex-shrink: 0;"></div>
            <div style="flex: 1;">
                <div class="skeleton skeleton-text" style="width: 95%;"></div>
                <div class="skeleton skeleton-text" style="width: 60%;"></div>
            </div>
        </div>
    </div>
    """, unsafe_allow_html=True)

# ==========================================
# 异步数据加载函数（修复 session state 访问问题）
# ==========================================

async def fetch_files_async(session: aiohttp.ClientSession, kb_id: str, user_id: int) -> Dict:
    """异步获取单个知识库的文件列表"""
    try:
        url = f"{API_BASE_URL}/knowledge-bases/{kb_id}/files"
        params = {"user_id": user_id}

        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as response:
            if response.status == 200:
                data = await response.json()

                if data.get("status") == "success":
                    files = data.get("files", [])
                elif "files" in data:
                    files = data.get("files", [])
                else:
                    files = []

                file_list = []
                file_info_dict = {}

                for f in files:
                    if isinstance(f, dict):
                        file_name = f.get("file_name", "")
                        file_hash = f.get("file_hash", "")
                        if file_name:
                            # 存储原始文件名（编码后的），显示时再解码
                            file_list.append(file_name)
                            file_info_dict[file_name] = file_hash
                    else:
                        file_str = str(f)
                        file_list.append(file_str)
                        file_info_dict[file_str] = ""

                return {
                    "kb_id": kb_id,
                    "files": file_list,
                    "files_info": file_info_dict
                }

            return {"kb_id": kb_id, "files": [], "files_info": {}}
    except Exception as e:
        print(f"异步获取知识库 {kb_id} 文件失败: {e}")
        return {"kb_id": kb_id, "files": [], "files_info": {}}

async def load_all_data_async(*, user_id, auth_headers):
    """
    异步加载所有数据：先加载知识库列表，再并发加载文件
    修复：不直接访问 st.session_state，而是通过返回值更新
    """
    result = {
        'knowledge_bases': {},
        'kb_loading_status': {},
        'knowledge_base_files_info': {},
        'selected_kb': None,
        'error': None
    }
    
    try:
        timeout = aiohttp.ClientTimeout(total=10)

        async with aiohttp.ClientSession(timeout=timeout, headers=auth_headers) as session:
            # 1. 异步获取知识库列表
            url = f"{API_BASE_URL}/knowledge-bases"
            params = {"user_id": user_id}

            try:
                async with session.get(url, params=params) as response:
                    if response.status != 200:
                        result['error'] = f"获取知识库列表失败: HTTP {response.status}"
                        return result

                    data = await response.json()
            except asyncio.TimeoutError:
                result['error'] = "获取知识库列表超时"
                return result

            # 解析知识库列表
            knowledge_bases = []
            if isinstance(data, dict):
                if "knowledge_bases" in data:
                    knowledge_bases = data.get("knowledge_bases", [])
                elif "data" in data:
                    knowledge_bases = data.get("data", [])
            elif isinstance(data, list):
                knowledge_bases = data

            if not knowledge_bases:
                knowledge_bases = ["默认知识库"]

            # 2. 初始化知识库结构
            new_knowledge_bases = {}
            new_kb_loading_status = {}

            for kb in knowledge_bases:
                if isinstance(kb, dict):
                    kb_id = kb.get("knowledge_base_id") or kb.get("name") or kb.get("id")
                    if not kb_id:
                        continue
                else:
                    kb_id = str(kb)

                if kb_id:
                    new_knowledge_bases[kb_id] = []
                    new_kb_loading_status[kb_id] = True

            if "默认知识库" not in new_knowledge_bases:
                new_knowledge_bases["默认知识库"] = []
                new_kb_loading_status["默认知识库"] = True

            result['knowledge_bases'] = new_knowledge_bases
            result['kb_loading_status'] = new_kb_loading_status

            # 设置默认选中的知识库
            if new_knowledge_bases:
                result['selected_kb'] = "默认知识库" if "默认知识库" in new_knowledge_bases else list(new_knowledge_bases.keys())[0]

            # 3. 并发异步加载所有知识库的文件
            tasks = [
                fetch_files_async(session, kb_id, user_id)
                for kb_id in new_knowledge_bases.keys()
            ]

            results = await asyncio.gather(*tasks, return_exceptions=True)

            # 更新文件信息
            for res in results:
                if isinstance(res, Exception):
                    print(f"加载文件时发生错误: {res}")
                    continue

                kb_id = res.get("kb_id")
                if kb_id and kb_id in result['knowledge_bases']:
                    result['knowledge_bases'][kb_id] = res["files"]
                    result['knowledge_base_files_info'][kb_id] = res["files_info"]
                    result['kb_loading_status'][kb_id] = False

        return result

    except Exception as e:
        print(f"异步加载数据失败: {e}")
        result['error'] = str(e)
        return result

def _start_background(kind, operation, metadata=None):
    inbox = st.session_state.task_inbox
    user_id, headers = USER_ID, transport_headers()
    def run():
        try:
            result = asyncio.run(operation(user_id=user_id, auth_headers=headers))
        except Exception as error:
            result = {"status": "error", "error": type(error).__name__, "message": "后台操作未完成"}
        inbox.put({"kind": kind, "result": result, "metadata": metadata or {}, "user_id": user_id})
    Thread(target=run, daemon=True).start()


def start_async_load():
    _start_background("load", load_all_data_async)

def apply_async_load_result(result):
    if result.get("error"):
        st.session_state.load_error = result["error"]
    else:
        for key in ("knowledge_bases", "knowledge_base_files_info", "kb_loading_status"):
            st.session_state[key] = result.get(key, {})
        if st.session_state.selected_kb is None:
            st.session_state.selected_kb = result.get("selected_kb")
    st.session_state.data_loading = False
    st.session_state.files_loaded = True
    st.session_state.last_refresh_time = time.time()

async def refresh_single_knowledge_base_async(knowledge_base_id: str, *, user_id, auth_headers) -> Dict:
    """异步刷新单个知识库"""
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout, headers=auth_headers) as session:
            result = await fetch_files_async(session, knowledge_base_id, user_id)
            return result
    except Exception as e:
        print(f"异步刷新知识库 {knowledge_base_id} 失败: {e}")
        return {"kb_id": knowledge_base_id, "files": [], "files_info": {}, "error": str(e)}

def start_refresh_single_kb(kb_id):
    _start_background("refresh", lambda **identity: refresh_single_knowledge_base_async(kb_id, **identity))

def apply_single_refresh_result(result):
    kb_id = result.get("kb_id")
    if result.get("error"):
        st.warning("文档列表暂时无法刷新，请重试。")
    elif kb_id in st.session_state.knowledge_bases:
        st.session_state.knowledge_bases[kb_id] = result["files"]
        st.session_state.knowledge_base_files_info[kb_id] = result["files_info"]
        st.session_state.kb_loading_status[kb_id] = False
        st.session_state.optimistic_files.pop(kb_id, None)

# ==========================================
# API 调用函数
# ==========================================

async def upload_document_async(knowledge_base_id, file_name, file_content, file_type, *, user_id, auth_headers):
    """异步上传文档"""
    try:
        url = f"{API_BASE_URL}/document_upload"

        data = aiohttp.FormData()
        data.add_field('file', file_content, filename=file_name, content_type=file_type)
        data.add_field('knowledge_base_id', knowledge_base_id)
        data.add_field('user_id', str(user_id))

        timeout = aiohttp.ClientTimeout(total=60)

        async with aiohttp.ClientSession(timeout=timeout, headers=auth_headers) as session:
            async with session.post(url, data=data) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    return {"status": "error", "message": f"HTTP {response.status}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

def upload_document_api_optimized(knowledge_base_id, file):
    file_name = file.name
    st.session_state.optimistic_files.setdefault(knowledge_base_id, []).append({
        "name": file_name, "status": "uploading", "timestamp": time.time()})
    if file_name not in st.session_state.knowledge_bases.get(knowledge_base_id, []):
        st.session_state.knowledge_bases[knowledge_base_id].append(file_name)
    file.seek(0)
    file_content, file_type = file.read(), file.type
    _start_background("upload", lambda **identity: upload_document_async(
        knowledge_base_id, file_name, file_content, file_type, **identity),
        {"kb_id": knowledge_base_id, "file_name": file_name})
    st.session_state.needs_rerun = True
    return True

async def delete_file_async(knowledge_base_id: str, file_hash: str, *, user_id, auth_headers) -> Dict:
    """异步删除文件"""
    try:
        url = f"{API_BASE_URL}/knowledge-bases/{knowledge_base_id}/documents/{file_hash}"
        params = {"user_id": user_id}

        timeout = aiohttp.ClientTimeout(total=10)

        async with aiohttp.ClientSession(timeout=timeout, headers=auth_headers) as session:
            async with session.delete(url, params=params) as response:
                if response.status == 200:
                    return await response.json()
                elif response.status == 400:
                    return {"status": "error", "message": "文件不存在或无权删除"}
                else:
                    return {"status": "error", "message": f"HTTP {response.status}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

def delete_file_api_optimized(knowledge_base_id, file_name, file_hash):
    if not file_hash:
        st.error("无法获取文档标识，请刷新后重试。")
        return False
    if file_name in st.session_state.knowledge_bases.get(knowledge_base_id, []):
        st.session_state.knowledge_bases[knowledge_base_id].remove(file_name)
    st.session_state.knowledge_base_files_info.get(knowledge_base_id, {}).pop(file_name, None)
    _start_background("delete_file", lambda **identity: delete_file_async(knowledge_base_id, file_hash, **identity),
        {"kb_id": knowledge_base_id, "file_name": file_name, "file_hash": file_hash})
    st.session_state.needs_rerun = True
    return True

async def create_knowledge_base_async(knowledge_base_id: str, *, user_id, auth_headers) -> Dict:
    """异步创建知识库"""
    try:
        url = f"{API_BASE_URL}/knowledge-bases"
        params = {
            "knowledge_base_id": knowledge_base_id,
            "user_id": user_id
        }

        timeout = aiohttp.ClientTimeout(total=5)

        async with aiohttp.ClientSession(timeout=timeout, headers=auth_headers) as session:
            async with session.post(url, params=params) as response:
                if response.status == 200:
                    return await response.json()
                elif response.status == 409:
                    return {"status": "exists", "message": "知识库已存在"}
                else:
                    return {"status": "error", "message": f"HTTP {response.status}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

def create_knowledge_base_api_optimized(knowledge_base_id):
    if knowledge_base_id == "默认知识库":
        st.warning("默认知识库是保留名称，请使用其他名称。")
        return False
    if knowledge_base_id in st.session_state.knowledge_bases:
        st.warning("该知识库已存在。")
        return False
    st.session_state.knowledge_bases[knowledge_base_id] = []
    st.session_state.kb_loading_status[knowledge_base_id] = False
    _start_background("create_kb", lambda **identity: create_knowledge_base_async(knowledge_base_id, **identity),
        {"kb_id": knowledge_base_id})
    st.session_state.needs_rerun = True
    return True

async def delete_knowledge_base_async(knowledge_base_id: str, *, user_id, auth_headers) -> Dict:
    """异步删除知识库"""
    try:
        url = f"{API_BASE_URL}/knowledge-bases/{knowledge_base_id}"
        params = {"user_id": user_id}

        timeout = aiohttp.ClientTimeout(total=5)

        async with aiohttp.ClientSession(timeout=timeout, headers=auth_headers) as session:
            async with session.delete(url, params=params) as response:
                if response.status == 200:
                    return await response.json()
                elif response.status == 404:
                    return {"status": "success", "message": "知识库已删除"}
                else:
                    return {"status": "error", "message": f"HTTP {response.status}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

def delete_knowledge_base_api_optimized(knowledge_base_id):
    if knowledge_base_id == "默认知识库":
        st.warning("默认知识库不可删除。")
        return False
    metadata = {"kb_id": knowledge_base_id,
        "files_backup": st.session_state.knowledge_bases.get(knowledge_base_id, []).copy(),
        "info_backup": st.session_state.knowledge_base_files_info.get(knowledge_base_id, {}).copy()}
    st.session_state.knowledge_bases.pop(knowledge_base_id, None)
    st.session_state.knowledge_base_files_info.pop(knowledge_base_id, None)
    if st.session_state.selected_kb == knowledge_base_id:
        st.session_state.selected_kb = next(iter(st.session_state.knowledge_bases), None)
    _start_background("delete_kb", lambda **identity: delete_knowledge_base_async(knowledge_base_id, **identity), metadata)
    st.session_state.needs_rerun = True
    return True

# ==========================================
# 辅助函数
# ==========================================

def rollback_optimistic_update(knowledge_base_id, file_name):
    """回滚乐观更新"""
    if knowledge_base_id in st.session_state.optimistic_files:
        st.session_state.optimistic_files[knowledge_base_id] = [
            f for f in st.session_state.optimistic_files[knowledge_base_id] 
            if f["name"] != file_name
        ]

    if file_name in st.session_state.knowledge_bases.get(knowledge_base_id, []):
        st.session_state.knowledge_bases[knowledge_base_id].remove(file_name)

    st.session_state.needs_rerun = True

def open_skill_settings():
    st.session_state.skills_help_open = True
    open_settings("技能与命令")


def change_workspace_view():
    """Make a workbench/chat switch survive a browser reload, too."""
    conversation = st.session_state.get('active_conversation') or {}
    st.query_params.clear()
    if st.session_state.workspace_view == '工作台':
        project_id = conversation.get('project_id') or st.session_state.get('assistant_active_project')
        if project_id:
            st.session_state.assistant_active_project = project_id
            st.query_params['project'] = project_id
    elif conversation:
        st.query_params['conversation'] = conversation['conversation_id']


def handle_send(input_text=None):
    query = input_text or st.session_state.get("user_query_input")
    if not query or not query.strip():
        return
    if st.session_state.is_streaming:
        return
    try:
        command = resolve_command(query)
    except SkillCommandError as error:
        st.session_state.skill_notice = str(error)
        st.session_state.skills_help_open = True
        return
    st.session_state.skill_notice = None
    if command is not None and command.kind == 'local':
        if command.action == 'help':
            open_skill_settings()
        elif command.action == 'settings':
            open_settings()
        elif command.action == 'new_chat':
            clear_chat_history()
        return

    try:
        prepare_turn(API_BASE_URL, USER_ID, query.strip())
    except (requests.RequestException, ValueError, KeyError):
        st.session_state.skill_notice = '未能创建或发送会话，请检查服务并刷新当前会话后重试。'
        return
    st.session_state.chat_history.append({
        "role": "user",
        "content": query.strip(),
        "timestamp": datetime.now().strftime("%H:%M")
    })

    st.session_state.is_streaming = True
    st.session_state.current_response = ""

def clear_chat_history():
    new_conversation()
    st.session_state.skills_help_open = False
    st.toast("已开始新对话，原对话可从会话列表打开。")

def request_delete(delete_type, kb_name, file_name=None, file_hash=None):
    if delete_type == 'kb' and kb_name == "默认知识库":
        st.warning("⚠️ 默认知识库不可删除")
        return
    st.session_state.pending_delete = {
        'type': delete_type, 
        'kb_name': kb_name, 
        'file_name': file_name,
        'file_hash': file_hash
    }

def confirm_delete():
    if st.session_state.pending_delete:
        info = st.session_state.pending_delete
        try:
            success = False
            if info['type'] == 'file':
                success = delete_file_api_optimized(
                    knowledge_base_id=info['kb_name'],
                    file_name=info['file_name'],
                    file_hash=info['file_hash']
                )
            elif info['type'] == 'kb':
                success = delete_knowledge_base_api_optimized(info['kb_name'])

            if success:
                return
            else:
                st.error("❌ 删除失败")
        except Exception as e:
            st.error(f"❌ 异常：{e}")
        finally:
            st.session_state.pending_delete = None

def cancel_delete():
    st.session_state.pending_delete = None

def select_knowledge_base():
    select_scope(API_BASE_URL, USER_ID, st.session_state.kb_selector)


def render_message(chat):
    """Use native Markdown so tables, code and copying work during streaming too."""
    role = chat["role"]
    with st.chat_message(role, avatar=":material/person:" if role == "user" else ":material/asterisk:"):
        st.markdown(
            f'<div class="turn-label" data-role="{role}">{"你" if role == "user" else "EchoMind"}'
            f'<span>{escape(chat["timestamp"])}</span></div>', unsafe_allow_html=True)
        st.markdown(chat["content"])
        if chat.get('skill'):
            skill_id = chat['skill'].get('id')
            skill = next((item for item in SKILL_CATALOG if item['id'] == skill_id), None)
            if skill:
                st.caption('本轮技能：' + skill['name'])
        if role == "assistant":
            render_answer_citations(chat, API_BASE_URL, USER_ID)
            render_run_details(chat, API_BASE_URL, USER_ID)


# ==========================================
# 处理异步回调结果
# ==========================================

def process_async_results():
    changed = False
    while True:
        try:
            item = st.session_state.task_inbox.get_nowait()
        except Empty:
            break
        if item["user_id"] != USER_ID:
            continue
        changed = True
        kind, result, metadata = item["kind"], item["result"], item["metadata"]
        kb_id = metadata.get("kb_id")
        failed = result.get("status") == "error" or bool(result.get("error"))
        if kind == "load":
            apply_async_load_result(result)
        elif kind == "refresh":
            apply_single_refresh_result(result)
        elif kind == "upload":
            if failed:
                rollback_optimistic_update(kb_id, metadata["file_name"])
                st.error("上传未完成：" + result.get("message", "请重试"))
            else:
                st.toast("文档已上传，正在后台解析。")
                start_refresh_single_kb(kb_id)
        elif kind == "delete_file":
            if failed and kb_id in st.session_state.knowledge_bases:
                if metadata["file_name"] not in st.session_state.knowledge_bases[kb_id]:
                    st.session_state.knowledge_bases[kb_id].append(metadata["file_name"])
                st.session_state.knowledge_base_files_info.setdefault(kb_id, {})[metadata["file_name"]] = metadata["file_hash"]
                st.error("文档删除未完成，请刷新后重试。")
            else:
                start_refresh_single_kb(kb_id)
        elif kind == "create_kb":
            if failed or result.get("status") == "exists":
                st.session_state.knowledge_bases.pop(kb_id, None)
                st.warning("知识库未创建，请刷新列表确认后重试。")
            else:
                st.session_state.selected_kb = kb_id
                st.toast("知识库已创建。")
        elif kind == "delete_kb" and failed:
            st.session_state.knowledge_bases[kb_id] = metadata["files_backup"]
            st.session_state.knowledge_base_files_info[kb_id] = metadata["info_backup"]
            st.error("知识库删除未完成，请刷新后重试。")
    if changed:
        st.rerun()

# ==========================================
# 初始化（异步加载策略 - 修复版）
# ==========================================

# 处理异步回调结果
process_async_results()
# 首次加载：启动后台异步加载，立即显示骨架屏
if not st.session_state.initialized:
    st.session_state.initialized = True
    start_async_load()
    st.rerun()

# 自动刷新机制：数据加载中时自动轮询刷新
if st.session_state.get('data_loading') and not st.session_state.get('files_loaded'):
    # 显示加载提示
    loading_placeholder = st.sidebar.empty()
    with loading_placeholder.container():
        st.caption("🔄 正在加载知识库数据...")
    # 短暂延迟后自动刷新
    import time
    time.sleep(0.3)
    st.rerun()

# 数据加载完成后触发刷新
if st.session_state.get('needs_rerun'):
    st.session_state.needs_rerun = False
    st.rerun()

# ==========================================
# 侧边栏 UI（支持骨架屏和异步加载）
# ==========================================
bootstrap_navigation(API_BASE_URL, USER_ID)
next_view = st.session_state.pop('_workspace_next_view', None)
if next_view:
    st.session_state.workspace_view = next_view
elif not st.session_state.get('workspace_route_initialized') and (st.query_params.get('conversation') or st.query_params.get('session')):
    st.session_state.workspace_view = '知识对话'
st.session_state.workspace_route_initialized = True
if st.session_state.selected_kb is None and st.session_state.knowledge_bases:
    st.session_state.selected_kb = next(iter(st.session_state.knowledge_bases))
draft_scope = st.session_state.get('conversation_draft_scope', st.session_state.selected_kb)
if draft_scope != st.session_state.selected_kb and not st.session_state.legacy_read_only:
    select_scope(API_BASE_URL, USER_ID, st.session_state.selected_kb)
st.session_state.conversation_draft_scope = st.session_state.selected_kb
total_kbs = len(st.session_state.knowledge_bases)
total_files = sum(len(files) for files in st.session_state.knowledge_bases.values())
busy = st.session_state.is_streaming

with st.sidebar:
    render_identity()
    st.markdown("""
    <div class="brand"><div class="brand-symbol">✳</div><div>
        <div class="brand-name">EchoMind<span> / </span></div>
    </div></div>
    """, unsafe_allow_html=True)
    st.radio('工作区',['工作台','知识对话'],key='workspace_view',horizontal=True,
             label_visibility='collapsed',disabled=busy,on_change=change_workspace_view)
    with st.container(key="sidebar_actions"):
        new_chat_column, settings_column = st.columns([4, 1], vertical_alignment="center")
        with new_chat_column:
            st.button("新对话", icon=":material/add:", key="new_chat", type="primary",
                      width="stretch", on_click=clear_chat_history, disabled=busy)
        with settings_column:
            render_settings_button(disabled=busy)
    st.markdown('<div class="section-label">知识库</div>', unsafe_allow_html=True)
    if st.session_state.knowledge_bases:
        kb_options = list(st.session_state.knowledge_bases)
        bound_scope = (st.session_state.active_conversation or {}).get('scope_id')
        if bound_scope and bound_scope not in kb_options:
            kb_options.append(bound_scope)
        if st.session_state.selected_kb not in kb_options:
            st.session_state.selected_kb = kb_options[0]
        if st.session_state.get("kb_selector") != st.session_state.selected_kb:
            st.session_state.kb_selector = st.session_state.selected_kb
        st.selectbox("当前知识库", kb_options, key="kb_selector", on_change=select_knowledge_base,
                     label_visibility="collapsed", disabled=busy)
        st.caption("搜索全部知识库的文档" if st.session_state.selected_kb == "默认知识库" else "仅搜索当前知识库的文档")
        if bound_scope and bound_scope not in st.session_state.knowledge_bases:
            st.caption('会话已绑定此库，本页资料目录待刷新。')
            if st.button('刷新知识库目录', disabled=busy):
                start_async_load()
                st.rerun()
    else:
        st.info("创建一个知识库，开始积累你的知识。")
    render_directory(API_BASE_URL, USER_ID, disabled=busy)

    if st.session_state.pending_delete:
        info = st.session_state.pending_delete
        target = info['file_name'] or info['kb_name']
        st.warning(f"确认删除「{target}」？此操作无法撤销。")
        yes, no = st.columns(2)
        yes.button("确认删除", key="confirm_del_btn", on_click=confirm_delete, width="stretch")
        no.button("取消", key="cancel_del_btn", on_click=cancel_delete, width="stretch")

    with st.expander("资料管理", icon=":material/folder_open:", expanded=not bool(st.session_state.knowledge_bases)):
        with st.expander("创建知识库", icon=":material/create_new_folder:"):
            with st.form("create_kb", clear_on_submit=True):
                new_kb_name = st.text_input("知识库名称", placeholder="例如：研究资料、工作笔记", max_chars=80)
                create_submitted = st.form_submit_button("创建", width="stretch", disabled=busy)
            if create_submitted:
                if new_kb_name.strip():
                    create_knowledge_base_api_optimized(new_kb_name.strip())
                else:
                    st.warning("请输入知识库名称。")

        selected_kb = st.session_state.selected_kb
        if selected_kb in st.session_state.knowledge_bases:
            files = st.session_state.knowledge_bases[selected_kb]
            is_loading = st.session_state.kb_loading_status.get(selected_kb, False)
            st.markdown(f'<div class="section-label">本库文档 <span>{len(files):02d} FILES</span></div>', unsafe_allow_html=True)
            with st.container(key="document_list", height=250 if len(files) > 5 else "content"):
                if is_loading:
                    st.caption("正在更新文件列表…")
                if not files and not is_loading:
                    st.markdown('<div class="empty-docs"><span>▤</span>还没有文档<br><small>上传资料后，回答就有据可循。</small></div>', unsafe_allow_html=True)
                from urllib.parse import unquote
                for i, file_name in enumerate(files):
                    display_name = unquote(file_name)
                    uploading = any(f["name"] == file_name and f["status"] == "uploading"
                                    for f in st.session_state.optimistic_files.get(selected_kb, []))
                    name_col, action_col = st.columns([5, 1], vertical_alignment="center")
                    with name_col:
                        st.markdown(f'<div class="document-row" title="{escape(display_name, quote=True)}">'
                                    f'<span>{"◌" if uploading else "▤"}</span>{escape(display_name)}</div>', unsafe_allow_html=True)
                    with action_col:
                        st.button("删除文件", icon=":material/close:", type="tertiary",
                                  key=f"del_f_{selected_kb}_{i}", help=f"删除 {display_name}",
                                  disabled=uploading or busy, on_click=request_delete,
                                  args=('file', selected_kb, file_name,
                                        st.session_state.knowledge_base_files_info.get(selected_kb, {}).get(file_name)))
            with st.expander("添加文档", icon=":material/upload_file:"):
                uploaded_file = st.file_uploader("选择文档", type=['pdf', 'docx', 'doc'], key=f"uploader_{selected_kb}",
                                                 label_visibility="collapsed", disabled=busy)
                st.caption("支持 PDF、Word 文档，上传后自动解析。")
                if uploaded_file is not None and st.button("上传到知识库", type="primary", width="stretch",
                                                            key=f"upload_btn_{selected_kb}", disabled=busy):
                    if uploaded_file.name in files:
                        st.warning("文件已存在，请选择其他文件。")
                    elif upload_document_api_optimized(selected_kb, uploaded_file):
                        st.rerun()
            if selected_kb != "默认知识库":
                render_graph_management(API_BASE_URL, USER_ID, selected_kb, disabled=busy)
                with st.expander("知识库设置", icon=":material/settings:"):
                    st.caption(f"删除「{selected_kb}」及其全部文档。")
                    st.button("删除知识库", key=f"del_kb_{selected_kb}", icon=":material/delete:",
                              on_click=request_delete, args=('kb', selected_kb), disabled=busy)
    with st.expander("更多", icon=":material/more_horiz:"):
        st.toggle('查看归档会话', key='conversation_archived_filter', value=False, disabled=busy)
        render_settings_summary(API_BASE_URL, USER_ID, disabled=busy)
        st.button("技能与命令", key="open_skill_help", icon=":material/bolt:", type="tertiary", on_click=open_skill_settings)
        render_legacy_history(API_BASE_URL, USER_ID, disabled=busy)
        st.caption(f"{total_kbs} 个知识库 · {total_files} 份文档")
        st.caption("EchoMind · 个人知识工作台")

# ==========================================
# 主界面
# ==========================================
if st.session_state.workspace_view == '工作台':
    render_assistant(API_BASE_URL, USER_ID, disabled=busy)
    render_settings_dialog(API_BASE_URL, USER_ID)
    st.stop()

project_conversation = st.session_state.active_conversation or {}
if project_conversation.get('project_id'):
    with st.container(border=True):
        project_label, project_back = st.columns([4,1],vertical_alignment='center')
        project_label.caption('项目对话 · '+project_conversation.get('project_title','当前项目')+' · 使用该项目的持久进度')
        if project_back.button('返回项目',key='assistant_back_to_project',disabled=busy):
            st.session_state.assistant_active_project = project_conversation['project_id']
            st.session_state._workspace_next_view = '工作台'
            st.query_params.clear()
            st.query_params['project'] = project_conversation['project_id']
            st.rerun()
scope_name = "全部知识库" if selected_kb == "默认知识库" else (selected_kb or "尚未选择知识库")
scope_files = total_files if selected_kb == "默认知识库" else len(st.session_state.knowledge_bases.get(selected_kb, []))
st.markdown(f"""
<div class="workspace-header"><div class="workspace-breadcrumb">知识工作台 <span>/</span> 对话</div>
    <div class="scope-chip"><i></i>{escape(scope_name)}<span>· {scope_files} 份文档</span></div>
</div>
""", unsafe_allow_html=True)

if st.session_state.load_error:
    st.warning("知识库暂时加载失败，请检查后端服务后重试。")
    if st.button("重新加载知识库", icon=":material/refresh:"):
        st.session_state.load_error = None
        st.session_state.data_loading = True
        st.session_state.files_loaded = False
        start_async_load()
        st.rerun()

if st.session_state.skill_notice:
    st.warning(st.session_state.skill_notice)
render_conversation_controls(API_BASE_URL, USER_ID, disabled=busy)

if not st.session_state.chat_history:
    st.markdown("""
    <div class="welcome welcome-simple">
        <div class="welcome-symbol" aria-hidden="true">✳</div>
        <h1>让知识，成为答案。</h1>
        <p>选好知识库，开始你的问题。</p>
    </div>
    """, unsafe_allow_html=True)
    if not st.session_state.knowledge_bases:
        st.info("在左侧创建知识库并添加文档，即可开始第一段对话。")

chat_container = st.container(key="conversation")
with chat_container:
    for chat in st.session_state.chat_history:
        render_message(chat)

# ==========================================
# 流式处理（保持原有逻辑）
# ==========================================
if st.session_state.is_streaming and st.session_state.knowledge_bases:
    pending_turn = st.session_state.pending_conversation_turn
    last_user_msg = pending_turn['query']
    selected_kb = st.session_state.selected_kb
    accumulated_answer = ""
    has_received_response = False
    response_metadata = {}
    client_started = time.perf_counter()

    with chat_container, st.chat_message("assistant", avatar=":material/asterisk:"):
        st.markdown('<div class="turn-label">EchoMind <span>正在回复</span></div>', unsafe_allow_html=True)
        thinking_placeholder = st.empty()
        status_placeholder = st.empty()
        answer_placeholder = st.empty()

        thinking_placeholder.markdown('<div class="thinking"><span></span>正在阅读问题，整理相关线索…</div>', unsafe_allow_html=True)

    try:
        active_id = pending_turn['conversation_id']
        url = f"{API_BASE_URL}/conversations/{active_id}/turns/stream"
        params = {"user_id": USER_ID}
        payload = {key: pending_turn[key] for key in ('query','client_turn_id','expected_revision')}
        with requests.post(url, params=params, json=payload, stream=True, timeout=(10, 160)) as response:
            if response.status_code != 200:
                accumulated_answer = f"❌ 后端错误：{response.status_code}"
                has_received_response = True
                thinking_placeholder.empty()
                answer_placeholder.markdown(accumulated_answer)
            else:
                for line in response.iter_lines(chunk_size=1, decode_unicode=True):
                    if not line:
                        continue

                    try:
                        data = json.loads(line.strip())
                    except json.JSONDecodeError:
                        continue
                    msg_type = data.get("type")
                    if data.get('conversation_id') != active_id:
                        continue
                    content = data.get("content", "")
                    if msg_type == "run":
                        response_metadata["run_id"] = data["run_id"]
                        if data.get('skill'):
                            response_metadata['skill'] = data['skill']
                    elif msg_type == "metrics":
                        response_metadata.update({"run_id": data["run_id"], "timings": data["timings"],
                                                  "persisted": data.get("persisted", False),
                                                  "personalization": data.get("personalization")})
                    elif msg_type == "status":
                        status_placeholder.caption(str(content))
                    elif msg_type == "answer":
                        if not has_received_response:
                            has_received_response = True
                            response_metadata["client_ttft_ms"] = (time.perf_counter() - client_started) * 1000
                            thinking_placeholder.empty()
                        accumulated_answer += content
                        answer_placeholder.markdown(accumulated_answer)
                    elif msg_type == "error":
                        accumulated_answer = f"❌ {content}"
                        thinking_placeholder.empty()
                        answer_placeholder.markdown(accumulated_answer)
                    elif msg_type == 'done':
                        response_metadata['status'] = data.get('status')

    except Exception as e:
        accumulated_answer = f"❌ 错误：{str(e)}"
        answer_placeholder.markdown(accumulated_answer)
        if not has_received_response:
            thinking_placeholder.empty()

    thinking_placeholder.empty()
    status_placeholder.empty()

    st.session_state.chat_history.append({
        "role": "assistant",
        "content": accumulated_answer,
        "timestamp": datetime.now().strftime("%H:%M"),
        "client_total_ms": (time.perf_counter() - client_started) * 1000,
        **response_metadata
    })

    st.session_state.is_streaming = False
    st.session_state.conversation_send_uncertain = response_metadata.get('status') not in ('complete','failed','cancelled','running')
    invalidate_directory()
    if response_metadata.get('status') in ('complete','failed','cancelled','running'):
        load_conversation(API_BASE_URL, USER_ID, pending_turn['conversation_id'])
    st.rerun()

# ==========================================
# 输入框区域
# ==========================================
if st.session_state.knowledge_bases:
    st.chat_input("输入问题，或用 / 和 & 唤出技能…", key="user_query_input", on_submit=handle_send,
                  disabled=st.session_state.is_streaming or input_unavailable())
    render_skill_palette(SKILL_CATALOG, disabled=st.session_state.is_streaming or input_unavailable())

render_settings_dialog(API_BASE_URL, USER_ID)

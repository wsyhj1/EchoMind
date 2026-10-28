"""Conversation navigation. Each browser page owns one active stream."""
import time
from datetime import datetime
from html import escape
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import streamlit as st
from api_client import api_requests as requests


def initialize_navigation():
    defaults = {'active_conversation': None, 'conversation_directory_cache': {}, 'conversation_recent': {},
                'conversation_ready': False, 'conversation_error': None, 'legacy_read_only': False,
                'conversation_before': None, 'conversation_create_key': str(uuid4()),
                'chat_session_id': uuid4().hex}
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def _get(api, path, user_id, **params):
    response = requests.get(api+path, params={'user_id': user_id, **params}, timeout=8)
    response.raise_for_status()
    return response.json()


def _chat_items(items):
    messages = []
    for item in items:
        timestamp = datetime.fromisoformat(item['started_at']).astimezone(ZoneInfo('Asia/Shanghai')).strftime('%H:%M')
        messages.append({'role': 'user', 'content': item['query'], 'timestamp': timestamp})
        answer = item.get('answer', '')
        if item.get('status') != 'complete':
            note = '正在回答，请稍后刷新。' if item.get('status') == 'running' else (item.get('error') or '本轮未完成，可重新发送。')
            answer = (answer+'\n\n' if answer else '')+'> '+note
        messages.append({'role': 'assistant', 'content': answer, 'timestamp': timestamp,
            'run_id': item['run_id'], 'persisted': item.get('status') == 'complete',
            'timings': item.get('timings') or {}, 'personalization': item.get('personalization'),
            'skill': item.get('skill') or item.get('config', {}).get('skill')})
    return messages


def load_conversation(api, user_id, conversation_id):
    try:
        data = _get(api, f'/conversations/{conversation_id}/messages', user_id)
        conversation = data['conversation']
        st.session_state.active_conversation = conversation
        st.session_state.selected_kb = conversation['scope_id']
        st.session_state.conversation_recent[conversation['scope_id']] = conversation['conversation_id']
        st.session_state.chat_session_id = conversation['session_id']
        st.session_state.chat_history = _chat_items(data['items'])
        st.session_state.conversation_before = data['next_cursor']
        st.session_state.conversation_error = None
        st.session_state.legacy_read_only = False
        st.session_state.conversation_running = any(item['status'] == 'running' for item in data['items'])
        st.session_state.conversation_draft_scope = conversation['scope_id']
        st.session_state.is_streaming = False
        st.session_state.pending_conversation_turn = None
        st.session_state._workspace_next_view = '知识对话'
        st.query_params.clear()
        st.query_params['conversation'] = conversation['conversation_id']
    except (requests.RequestException, ValueError, KeyError):
        st.session_state.active_conversation = None
        st.session_state.chat_history = []
        st.session_state.is_streaming = False
        st.session_state.pending_conversation_turn = None
        st.session_state.conversation_failed_id = conversation_id
        st.session_state.conversation_error = '会话暂时无法打开，请重试或新建对话。'
        return False
    return True


def open_legacy(api, user_id, session_id):
    try:
        # Managed session links resolve to the new directory; old ones stay read-only.
        response = requests.get(api+f'/conversations/resolve-session/{session_id}', params={'user_id': user_id}, timeout=8)
        if response.ok:
            return load_conversation(api, user_id, response.json()['conversation_id'])
        if response.status_code != 404:
            response.raise_for_status()
        runs = _get(api, '/runs', user_id, session_id=session_id, limit=100)
        st.session_state.active_conversation = None
        st.session_state.chat_session_id = session_id
        st.session_state.chat_history = _chat_items(runs)
        st.session_state.legacy_read_only = True
        st.session_state.conversation_before = None
        st.session_state.conversation_running = False
        st.session_state.conversation_error = None
        st.session_state.is_streaming = False
        st.query_params.clear()
        st.query_params['session'] = session_id
        return True
    except (requests.RequestException, ValueError, KeyError):
        st.session_state.conversation_error = '旧版会话暂时无法读取，请重试。'
        return False


def bootstrap_navigation(api, user_id):
    initialize_navigation()
    if st.session_state.conversation_ready:
        return
    conversation_id = st.query_params.get('conversation')
    legacy_session = st.query_params.get('session')
    if conversation_id:
        try:
            UUID(conversation_id)
            load_conversation(api, user_id, conversation_id)
        except ValueError:
            st.session_state.conversation_error = '会话链接格式不正确，请从会话列表打开。'
    elif legacy_session:
        import re
        if re.fullmatch(r'[A-Za-z0-9_-]{1,64}', legacy_session):
            open_legacy(api, user_id, legacy_session)
        else:
            st.session_state.conversation_error = '会话链接格式不正确。'
    st.session_state.conversation_ready = True


def new_conversation():
    initialize_navigation()
    st.session_state.workspace_view = '知识对话'
    st.session_state.active_conversation = None
    st.session_state.chat_session_id = uuid4().hex
    st.session_state.chat_history = []
    st.session_state.conversation_create_key = str(uuid4())
    st.session_state.conversation_before = None
    st.session_state.conversation_error = None
    st.session_state.conversation_running = False
    st.session_state.legacy_read_only = False
    st.session_state.pending_conversation_turn = None
    st.session_state.conversation_send_uncertain = False
    st.session_state.conversation_failed_id = None
    st.session_state.conversation_draft_scope = st.session_state.get('selected_kb')
    st.session_state.conversation_recent[st.session_state.get('selected_kb')] = None
    st.session_state.is_streaming = False
    st.session_state.current_response = ''
    st.session_state.evaluation_jobs = {}
    st.session_state.skill_notice = None
    st.query_params.clear()


def select_scope(api, user_id, scope_id):
    remembered = st.session_state.conversation_recent.get(scope_id)
    st.session_state.selected_kb = scope_id
    if remembered:
        load_conversation(api, user_id, remembered)
    else:
        new_conversation()


def prepare_turn(api, user_id, query):
    if st.session_state.legacy_read_only or st.session_state.conversation_error:
        raise ValueError('请先打开有效会话或新建对话。')
    conversation = st.session_state.active_conversation
    if conversation and conversation['scope_id'] != st.session_state.selected_kb:
        raise ValueError('会话知识范围与页面不一致，请重新打开会话。')
    if not conversation:
        response = requests.post(api+'/conversations', params={'user_id': user_id}, timeout=8,
            json={'scope_id': st.session_state.selected_kb, 'client_create_id': st.session_state.conversation_create_key})
        response.raise_for_status()
        conversation = response.json()
        st.session_state.active_conversation = conversation
        st.session_state.chat_session_id = conversation['session_id']
        st.session_state.conversation_recent[conversation['scope_id']] = conversation['conversation_id']
        st.query_params.clear()
        st.query_params['conversation'] = conversation['conversation_id']
    if conversation['archived'] or conversation['read_only']:
        raise ValueError('请先恢复会话，或新建对话。')
    st.session_state.pending_conversation_turn = {'conversation_id': conversation['conversation_id'],
        'query': query, 'client_turn_id': str(uuid4()), 'expected_revision': conversation['revision']}
    st.session_state.conversation_send_uncertain = False
    st.session_state.conversation_draft_scope = conversation['scope_id']


def invalidate_directory():
    st.session_state.conversation_directory_cache = {}


def _directory(api, user_id, scope, archived, search, cursor=None):
    key = (scope, archived, search, cursor)
    cache = st.session_state.conversation_directory_cache
    saved = cache.get(key)
    if not saved or time.monotonic()-saved[0]>30:
        data = _get(api, '/conversations', user_id, scope_id=scope, archived=archived, search=search, cursor=cursor, limit=30)
        cache[key] = (time.monotonic(), data)
    return cache[key][1]


def render_directory(api, user_id, disabled=False):
    scope = st.session_state.selected_kb
    if not scope:
        return
    with st.container(key='conversation_directory'):
        title, refresh = st.columns([5, 1])
        title.markdown('<div class="section-label">会话</div>', unsafe_allow_html=True)
        if refresh.button('刷新会话', icon=':material/refresh:', key='refresh_conversations',
                          type='tertiary', help='刷新其他标签页的会话', disabled=disabled):
            invalidate_directory()
        search = st.text_input('搜索会话', placeholder='搜索会话', key='conversation_search', label_visibility='collapsed', disabled=disabled)
        archived = st.session_state.get('conversation_archived_filter', False)
        page_key = (scope, archived, search)
        if st.session_state.get('conversation_page_key') != page_key:
            st.session_state.conversation_page_key = page_key
            st.session_state.conversation_cursors = [None]
        try:
            next_cursor = None
            count = 0
            for cursor in st.session_state.get('conversation_cursors', [None]):
                data = _directory(api, user_id, scope, archived, search, cursor)
                for conversation in data['items']:
                    count += 1
                    active = st.session_state.active_conversation or {}
                    st.button(conversation['title'], key='conversation_'+conversation['conversation_id'],
                        icon=':material/chat_bubble_outline:', width='stretch', disabled=disabled,
                        type='primary' if conversation['conversation_id']==active.get('conversation_id') else 'tertiary',
                        on_click=load_conversation, args=(api, user_id, conversation['conversation_id']))
                next_cursor = data['next_cursor']
            if not count:
                st.caption('暂无匹配会话' if search or archived else '新对话会保存在这里')
            if next_cursor and st.button('更多会话', key='conversation_load_more', disabled=disabled):
                st.session_state.conversation_cursors.append(next_cursor)
                st.rerun()
        except (requests.RequestException, ValueError, KeyError):
            st.caption('会话列表暂时不可用，可点击刷新重试。')


def render_legacy_history(api, user_id, disabled=False):
    if st.checkbox('旧版历史会话', key='show_legacy_conversations', disabled=disabled):
        try:
            items = _get(api, '/conversations/legacy', user_id)['items']
            for item in items:
                label = ' / '.join(item['scopes'])+' · '+item['updated_at'][:10]
                st.button(label, key='legacy_'+item['session_id'], width='stretch', disabled=disabled,
                    on_click=open_legacy, args=(api,user_id,item['session_id']))
            st.caption('旧记录保留只读；新建对话后使用独立上下文。')
        except (requests.RequestException, ValueError, KeyError):
            st.caption('旧版历史暂时不可用。')


def render_conversation_controls(api, user_id, disabled=False):
    if st.session_state.conversation_error:
        st.warning(st.session_state.conversation_error)
        failed_id = st.session_state.get('conversation_failed_id')
        if failed_id and st.button('重试打开会话', disabled=disabled):
            load_conversation(api,user_id,failed_id)
            st.rerun()
        return
    if st.session_state.legacy_read_only:
        st.info('这是旧版历史会话，仅供查看。点击“新对话”继续提问。')
        return
    conversation = st.session_state.active_conversation
    if not conversation:
        return
    with st.container(key='conversation_heading'):
        heading, actions = st.columns([9, 1], vertical_alignment='center')
        heading.markdown('<div class="conversation-title">'+escape(conversation['title'])+'</div>', unsafe_allow_html=True)
    with actions.popover('会话菜单', icon=':material/more_horiz:', disabled=disabled):
        st.link_button('在新标签页打开', '?conversation='+conversation['conversation_id'], width='stretch')
        if st.button('刷新当前会话', icon=':material/sync:', key='reload_active_conversation', type='tertiary', disabled=disabled):
            load_conversation(api, user_id, conversation['conversation_id'])
            invalidate_directory()
            st.rerun()
        with st.form('rename_conversation'):
            title = st.text_input('会话名称', value=conversation['title'], max_chars=160)
            rename = st.form_submit_button('重命名', disabled=disabled)
        archive = st.button('恢复会话' if conversation['archived'] else '归档会话', disabled=disabled)
        if rename or archive:
            payload = {'expected_revision': conversation['revision']}
            payload.update({'title': title} if rename else {'archived': not conversation['archived']})
            try:
                response = requests.patch(api+'/conversations/'+conversation['conversation_id'],
                    params={'user_id': user_id}, json=payload, timeout=8)
                response.raise_for_status()
                st.session_state.active_conversation = response.json()
                invalidate_directory()
                st.rerun()
            except (requests.RequestException, ValueError):
                st.warning('未能修改，会话可能已在其他标签页更新。请刷新后重试。')
    if st.session_state.get('conversation_running'):
        st.info('此会话正在回答，可稍后刷新查看结果；其他会话可在新标签页打开。')
    if conversation['archived']:
        st.info('此会话已归档，在会话菜单中恢复后可继续提问。')
    if st.session_state.get('conversation_send_uncertain') and st.session_state.get('pending_conversation_turn'):
        st.warning('上次发送未确认。可用原发送标识重试，或刷新当前会话核对结果。')
        if st.button('重试上次发送', key='conversation_retry_send', disabled=disabled):
            st.session_state.is_streaming = True
            st.session_state.conversation_send_uncertain = False
            st.rerun()
    if st.session_state.conversation_before:
        if st.button('加载更早消息', key='conversation_older', disabled=disabled):
            try:
                data = _get(api, f"/conversations/{conversation['conversation_id']}/messages", user_id,
                            before=st.session_state.conversation_before)
                st.session_state.chat_history = _chat_items(data['items'])+st.session_state.chat_history
                st.session_state.conversation_before = data['next_cursor']
                st.rerun()
            except (requests.RequestException, ValueError, KeyError):
                st.warning('更早消息暂时无法加载，请重试。')


def input_unavailable():
    conversation = st.session_state.get('active_conversation') or {}
    return bool(st.session_state.get('legacy_read_only') or st.session_state.get('conversation_error') or
                st.session_state.get('conversation_running') or conversation.get('archived') or conversation.get('read_only'))

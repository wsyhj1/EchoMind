"""In-app reminders; the server scheduler remains independent of Streamlit reruns."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import streamlit as st
from api_client import api_requests as requests

ZONE = ZoneInfo('Asia/Shanghai')
STATES = {'scheduled':'待提醒','delivered':'已送达','cancelled':'已取消'}
REASONS = {'user_cancelled':'你已取消','project_archived':'项目已归档','task_closed':'任务已完成或取消','scope_unavailable':'资料范围已不可用'}


def reset_inbox_page():
    st.session_state.assistant_inbox_page = 0


def local_time(value):
    return datetime.fromisoformat(value).astimezone(ZONE).strftime('%Y-%m-%d %H:%M')


@st.fragment(run_every='15s')
def render_inbox(api, user_id, *, disabled=False):
    from assistant_ui import _get, _send
    disabled = disabled or bool(st.session_state.get('assistant_uncertain'))
    try:
        status = _get(api, user_id, '/assistant/reminders/status')
        # Read first to show the unread count even when the expander is closed.
        history = st.session_state.get('assistant_inbox_history', False)
        page = st.session_state.get('assistant_inbox_page', 0)
        inbox = _get(api, user_id, '/assistant/notifications', unread=not history, offset=page*10, limit=10)
    except (requests.RequestException, ValueError, KeyError):
        st.warning('提醒收件箱暂时无法读取，请稍后刷新。')
        return
    with st.expander(f"提醒收件箱 · {inbox['unread_count']} 条未读", expanded=inbox['unread_count']>0):
        st.caption('时间：Asia/Shanghai · 工作台打开时每15秒检查新提醒。关闭网页后，服务仍会将到期提醒存入收件箱。')
        if not status['healthy']:
            st.warning('提醒调度尚未就绪或心跳延迟；待提醒事项仍已保存，服务恢复后补发。')
        st.checkbox('包含已读提醒', value=history, key='assistant_inbox_history',on_change=reset_inbox_page)
        if not inbox['items']:
            st.caption('这一页暂无提醒。可以在项目的「提醒」页设置。')
        for item in inbox['items']:
            nid = item['notification_id']
            with st.container(border=True):
                st.write(item['title'])
                st.caption(item['project_title']+(' · '+item['task_title'] if item.get('task_title') else ''))
                st.caption('计划 '+local_time(item['due_at'])+' · 送达 '+local_time(item['delivered_at']))
                if (datetime.fromisoformat(item['delivered_at'])-datetime.fromisoformat(item['due_at'])).total_seconds()>60:
                    st.caption('延迟送达：服务恢复或处理队列后补发。')
                a,b,c = st.columns(3)
                if a.button('标为已读',key=nid+'_read',disabled=disabled or bool(item['read_at'])):
                    _send(api,user_id,f'/assistant/notifications/{nid}/read',{},nid+'_read')
                active = item['reminder_status']=='delivered' and item['generation']==item['current_generation']
                if b.button('30分钟后提醒',key=nid+'_snooze',disabled=disabled or not active):
                    due = datetime.now(ZONE)+timedelta(minutes=30)
                    _send(api,user_id,f"/assistant/projects/{item['project_id']}/reminders/{item['reminder_id']}/snooze",
                        {'expected_revision':item['reminder_revision'],'due_at':due.isoformat(),'timezone':'Asia/Shanghai'},nid+'_snooze')
                c.link_button('打开项目','?project='+item['project_id'])
        a,b = st.columns(2)
        if a.button('上一页提醒',key='assistant_inbox_previous',disabled=page==0):
            st.session_state.assistant_inbox_page = page-1
            st.rerun()
        if b.button('下一页提醒',key='assistant_inbox_next',disabled=not inbox['has_more']):
            st.session_state.assistant_inbox_page = page+1
            st.rerun()


def render_reminders(api, user_id, workspace, *, disabled=False):
    from assistant_ui import _form_key, _get, _send
    project = workspace['project']
    pid = project['project_id']
    st.caption('设置一次性站内提醒。时间按Asia/Shanghai填写；电脑或后端关闭期间的提醒会在服务重启后补发。')
    st.caption('到期时若项目已归档、关联任务已完成或取消，将自动跳过。提醒不会替你修改任务状态。')
    area = pid+'_reminder'
    form = _form_key(area)
    tasks = {t['task_id']:t for t in workspace['tasks'] if t['status'] not in ('done','cancelled')}
    with st.form(form):
        title = st.text_input('提醒内容',max_chars=200,key=form+'_title',placeholder='复盘今天的进展，确定明天的第一步')
        task_id = st.selectbox('关联任务（可选）',[None,*tasks],format_func=lambda key:tasks[key]['title'] if key else '整个项目',key=form+'_task')
        default = datetime.now(ZONE)+timedelta(hours=1)
        a,b = st.columns(2)
        day = a.date_input('提醒日期',value=default.date(),key=form+'_date')
        clock = b.time_input('提醒时间',value=default.time().replace(second=0,microsecond=0),key=form+'_time')
        submitted = st.form_submit_button('设置提醒',type='primary',disabled=disabled or project['archived'])
    if submitted:
        due = datetime.combine(day,clock,tzinfo=ZONE)
        _send(api,user_id,f'/assistant/projects/{pid}/reminders',
            {'title':title,'task_id':task_id,'due_at':due.isoformat(),'timezone':'Asia/Shanghai'},area)
    page_key = pid+'_reminder_page'
    page = st.session_state.get(page_key,0)
    st.button('刷新提醒状态',key=pid+'_refresh_reminders')
    try:
        result = _get(api,user_id,f'/assistant/projects/{pid}/reminders',offset=page*10,limit=10)
    except (requests.RequestException, ValueError, KeyError):
        st.error('暂时无法读取提醒，请刷新重试。')
        return
    for item in result['items']:
        with st.container(border=True):
            st.write(item['title'])
            st.caption(STATES[item['status']]+' · '+local_time(item['due_at'])+' · Asia/Shanghai')
            if item.get('task_title'): st.caption('关联任务：'+item['task_title'])
            if item['reason']: st.caption(REASONS.get(item['reason'],item['reason']))
            if st.button('取消提醒',key=item['reminder_id']+'_cancel',disabled=disabled or item['status']=='cancelled'):
                _send(api,user_id,f"/assistant/projects/{pid}/reminders/{item['reminder_id']}/cancel",
                    {'expected_revision':item['revision']},item['reminder_id']+'_cancel')
    if not result['items']: st.caption('这一页暂无已设置的提醒。')
    a,b = st.columns(2)
    if a.button('上一页记录',key=pid+'_reminder_previous',disabled=page==0):
        st.session_state[page_key] = page-1
        st.rerun()
    if b.button('下一页记录',key=pid+'_reminder_next',disabled=not result['has_more']):
        st.session_state[page_key] = page+1
        st.rerun()

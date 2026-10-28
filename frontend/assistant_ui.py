"""Project workbench. Writes are durable API operations, not browser-only state."""
from datetime import date, datetime
from hashlib import sha256
from html import escape
import json
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import streamlit as st
from api_client import api_requests as requests

STATUS = {'todo':'待开始','in_progress':'进行中','blocked':'受阻','done':'已完成','cancelled':'已取消'}
PRIORITY = {'high':'高','normal':'中','low':'低'}
NOTES = {'progress':'进展','decision':'决定','blocker':'阻碍','learning':'学习'}


def _localtime(value):
    return datetime.fromisoformat(value).astimezone(ZoneInfo('Asia/Shanghai')).strftime('%m-%d %H:%M')


def _get(api, user_id, path, **params):
    response = requests.get(api+path,params={'user_id':user_id,**params},timeout=10)
    response.raise_for_status()
    return response.json()


def _post(api,user_id,path,body):
    response = requests.post(api+path,params={'user_id':user_id},json=body,timeout=12)
    response.raise_for_status()
    return response.json()


def _form_key(area):
    epochs = st.session_state.setdefault('assistant_form_epochs',{})
    return f'assistant_{area}_{epochs.get(area,0)}'


def _send(api,user_id,path,body,area):
    if st.session_state.get('assistant_uncertain'):
        st.error('上一项保存结果尚待核对，请先重试原操作。')
        return
    mark = sha256(json.dumps([path,body],ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    keys = st.session_state.setdefault('assistant_operation_keys',{})
    key = keys.setdefault(mark,str(uuid4()))
    payload = {**body, ('client_create_id' if path in ('/assistant/projects','/conversations') else 'operation_id'):key}
    pending = {'path':path,'body':payload,'area':area,'mark':mark}
    _attempt(api,user_id,pending)


def _attempt(api,user_id,pending):
    try:
        result = _post(api,user_id,pending['path'],pending['body'])
    except requests.HTTPError as error:
        response = error.response
        if response is not None and response.status_code < 500:
            st.session_state.pop('assistant_uncertain',None)
            message = '输入未通过校验，请检查字段。'
            try:
                detail = response.json().get('detail')
                if isinstance(detail,str):
                    message = detail
            except (ValueError,AttributeError):
                pass
            st.error(message)
            return
        st.session_state.assistant_uncertain = pending
        st.error('保存结果暂时无法确认，原操作标识已保留；请重试原操作核对结果。')
        st.rerun()
    except (requests.RequestException,ValueError):
        st.session_state.assistant_uncertain = pending
        st.error('连接中断，保存结果尚待确认。重试原操作不会重复创建。')
        st.rerun()
    st.session_state.pop('assistant_uncertain',None)
    st.session_state.assistant_operation_keys.pop(pending['mark'],None)
    epochs = st.session_state.setdefault('assistant_form_epochs',{})
    epochs[pending['area']] = epochs.get(pending['area'],0)+1
    st.session_state.pop('assistant_review',None)
    if pending['path']=='/assistant/projects':
        st.session_state.assistant_active_project = result['project_id']
        st.session_state.pop('assistant_project_picker',None)
        st.query_params['project'] = result['project_id']
    elif pending['path']=='/conversations':
        from conversations_ui import load_conversation
        if load_conversation(api,user_id,result['conversation_id']):
            st.session_state._workspace_next_view = '知识对话'
    st.toast('已保存。' if pending['path']!='/conversations' else '已打开项目对话。')
    st.rerun()


def _action(api,user_id,project,action,area):
    _send(api,user_id,f"/assistant/projects/{project['project_id']}/actions",
          {'expected_revision':project['revision'],'action':action},area)


def _project_picker(api,user_id,disabled):
    archived = st.toggle('查看归档项目',value=False,key='assistant_archived',disabled=disabled)
    try:
        projects = _get(api,user_id,'/assistant/projects',archived=archived)['items']
    except (requests.RequestException,ValueError,KeyError):
        st.error('项目列表暂时无法读取，请确认后端已启动并刷新。')
        return None
    with st.expander('新建项目',expanded=not projects):
        area = 'create_project'
        form = _form_key(area)
        with st.form(form):
            title = st.text_input('项目名称',max_chars=120,key=form+'_title',placeholder='例如：完成 EchoMind 个人助手第一版')
            goal = st.text_area('目标与完成标准',max_chars=3000,key=form+'_goal',placeholder='希望完成什么，怎样判断完成？')
            scopes = list(dict.fromkeys(['默认知识库',*st.session_state.get('knowledge_bases',{})]))
            scope = st.selectbox('资料范围',scopes,key=form+'_scope',help='默认知识库表示检索你有权访问的全部知识库。创建后固定范围。')
            submit = st.form_submit_button('创建项目',type='primary',disabled=disabled)
        if submit:
            _send(api,user_id,'/assistant/projects',{'title':title,'goal':goal,'scope_id':scope},area)
    if not projects:
        st.info('先创建一个项目，记下目标和下一步。任务、笔记与工作恢复无需上传文档。')
        return None
    mapping = {p['project_id']:p for p in projects}
    requested = st.query_params.get('project')
    active = st.session_state.get('assistant_active_project')
    if requested and requested not in mapping:
        # A bad/stale link must not silently show an unrelated previous project.
        st.warning('链接中的项目不可访问，或不在当前归档筛选内。请选择项目。')
        options = [None,*mapping]
        index = 0
        st.session_state.pop('assistant_project_picker',None)
    else:
        options = list(mapping)
        active = requested or active
        index = options.index(active) if active in options else 0
    chosen = st.selectbox('当前项目',options,index=index,
        format_func=lambda pid: f"{mapping[pid]['title']} · {mapping[pid]['open_tasks']}项待推进" if pid else '请选择项目',
        key='assistant_project_picker',disabled=disabled)
    if chosen:
        st.session_state.assistant_active_project = chosen
        st.query_params['project'] = chosen
    return chosen


def _tasks(api,user_id,workspace,disabled):
    project = workspace['project']
    pid = project['project_id']
    area = pid+'_new_task'
    form = _form_key(area)
    with st.expander('添加工作或学习任务',expanded=not workspace['tasks']):
        with st.form(form):
            title = st.text_input('任务标题',max_chars=200,key=form+'_title')
            description = st.text_area('任务说明',max_chars=3000,key=form+'_description')
            first,second = st.columns(2)
            category = first.selectbox('任务类型',['work','study'],format_func=lambda x:'工作' if x=='work' else '学习',key=form+'_category')
            priority = second.selectbox('优先级',list(PRIORITY),index=1,format_func=PRIORITY.get,key=form+'_priority')
            estimate = first.number_input('预计分钟数（0表示未知）',min_value=0,max_value=1440,step=5,key=form+'_estimate')
            has_due = second.checkbox('设置截止日期',key=form+'_has_due')
            due = second.date_input('截止日期',value=datetime.now(ZoneInfo('Asia/Shanghai')).date(),key=form+'_due')
            submit = st.form_submit_button('保存任务',type='primary',disabled=disabled)
        if submit:
            _action(api,user_id,project,{'kind':'add_task','title':title,'description':description,'category':category,
                'priority':priority,'estimated_minutes':estimate or None,'due_date':due.isoformat() if has_due else None},area)
    tasks = workspace['tasks']
    if not tasks:
        st.caption('添加一个能开始的小任务，让项目有下一步。')
        return
    st.dataframe([{'任务':t['title'],'类型':'学习' if t['category']=='study' else '工作','状态':STATUS[t['status']],
        '优先级':PRIORITY[t['priority']],'预计分钟':t.get('estimated_minutes'),'截止':t.get('due_date') or '未设置'}
        for t in tasks],hide_index=True,width='stretch')
    by_id = {t['task_id']:t for t in tasks}
    selected = st.selectbox('选择要更新的任务',list(by_id),format_func=lambda key:by_id[key]['title'],key=pid+'_edit_task')
    task = by_id[selected]
    area = pid+'_edit_'+selected
    form = _form_key(area)+'_'+str(project['revision'])
    with st.form(form):
        title = st.text_input('修改标题',value=task['title'],max_chars=200,key=form+'_title')
        description = st.text_area('修改说明',value=task['description'],max_chars=3000,key=form+'_description')
        a,b = st.columns(2)
        status = a.selectbox('更新状态',list(STATUS),index=list(STATUS).index(task['status']),format_func=STATUS.get,key=form+'_status')
        priority = b.selectbox('更新优先级',list(PRIORITY),index=list(PRIORITY).index(task['priority']),format_func=PRIORITY.get,key=form+'_priority')
        estimate = a.number_input('调整预计分钟（0表示未知）',min_value=0,max_value=1440,step=5,value=task.get('estimated_minutes') or 0,key=form+'_estimate')
        has_due = b.checkbox('保留截止日期',value=bool(task.get('due_date')),key=form+'_has_due')
        due = b.date_input('调整截止日期',value=date.fromisoformat(task['due_date']) if task.get('due_date') else datetime.now(ZoneInfo('Asia/Shanghai')).date(),key=form+'_due')
        completion = st.text_area('完成依据（完成时必填，记录为你的报告）',value=task.get('completion_note',''),max_chars=3000,key=form+'_completion')
        blocker = st.text_area('阻碍原因（受阻时必填）',value=task.get('blocker',''),max_chars=3000,key=form+'_blocker')
        submit = st.form_submit_button('更新任务',disabled=disabled)
    if submit:
        _action(api,user_id,project,{'kind':'update_task','task_id':selected,'title':title,'description':description,
            'status':status,'priority':priority,'estimated_minutes':estimate or None,'due_date':due.isoformat() if has_due else None,
            'completion_note':completion,'blocker':blocker},area)


def _notes(api,user_id,workspace,disabled):
    project,notes = workspace['project'],workspace['notes']
    area = project['project_id']+'_note'
    form = _form_key(area)
    with st.form(form):
        kind = st.selectbox('记录类型',list(NOTES),format_func=NOTES.get,key=form+'_kind')
        content = st.text_area('记录内容',max_chars=4000,key=form+'_content',placeholder='记录进展、选择方案的原因、困难或学到的东西。')
        choices = {n['note_id']:n for n in notes}
        replaces = st.selectbox('更正已有记录（可选）',[None,*choices],format_func=lambda key:NOTES[choices[key]['note_kind']]+'：'+choices[key]['content'][:45] if key else '新增记录',key=form+'_replaces')
        submit = st.form_submit_button('保存记录',disabled=disabled)
    if submit:
        _action(api,user_id,project,{'kind':'add_note','note_kind':kind,'content':content,'supersedes_note_id':replaces},area)
    for note in notes[:30]:
        with st.container(border=True):
            st.caption(NOTES[note['note_kind']]+' · '+_localtime(note['created_at'])+' · '+('用户采纳的助手候选' if note['source']=='assistant_proposal' else '用户记录'))
            st.write(note['content'])
            st.caption('记录编号：'+note['note_id'])
    if len(notes)>30:
        st.caption(f'当前展示最近30条，共{len(notes)}条；更早记录仍保存在项目中。')


def _resources(api,user_id,workspace,disabled):
    project = workspace['project']
    area = project['project_id']+'_resource'
    form = _form_key(area)
    kind = st.radio('关联资料类型',['link','document'],format_func=lambda x:'网页链接' if x=='link' else '已有知识库文档',horizontal=True,key=form+'_kind')
    candidates = {}
    for kb,files in st.session_state.get('knowledge_base_files_info',{}).items():
        if project['scope_id'] not in ('默认知识库',kb):
            continue
        for name,hash_value in files.items():
            if hash_value:
                candidates[kb+' / '+name] = hash_value
    with st.form(form):
        label = st.text_input('资料名称',max_chars=200,key=form+'_label')
        if kind=='link':
            reference = st.text_input('HTTP(S)链接',max_chars=2000,key=form+'_reference')
        else:
            selected = st.selectbox('已有文档',[None,*candidates],format_func=lambda x:x or '请选择已上传文档',key=form+'_document')
            reference = candidates.get(selected,'')
            st.caption('关联只保存来源指针；项目对话检索后才能依据正文回答。')
        submit = st.form_submit_button('关联资料',disabled=disabled)
    if submit:
        _action(api,user_id,project,{'kind':'add_resource','resource_type':kind,'label':label,'reference':reference},area)
    for resource in workspace['resources']:
        if resource['resource_type']=='link':
            st.link_button(resource['label'],resource['reference'])
        else:
            st.write(resource['label'])
            st.caption(resource['knowledge_base_id']+' · '+('来源可用' if resource['available'] else '来源已不可用'))


def _proposals(api,user_id,workspace,disabled):
    project = workspace['project']
    if not workspace['proposals']:
        st.caption('项目对话中保存的计划和变更候选会出现在这里，采纳后才更新正式状态。')
    for proposal in workspace['proposals']:
        with st.container(border=True):
            action = proposal['action']
            kind = action['kind']
            if kind in ('add_task','add_tasks'):
                tasks = action['tasks'] if kind=='add_tasks' else [action]
                st.markdown('**新增任务建议**')
                for task in tasks:
                    st.write(task['title'])
                    st.caption(('学习' if task.get('category')=='study' else '工作')+' · '+
                        (str(task['estimated_minutes'])+'分钟' if task.get('estimated_minutes') else '耗时待估计')+
                        (' · 截止'+task['due_date'] if task.get('due_date') else ''))
                    if task.get('description'): st.write(task['description'])
            elif kind=='update_task':
                names = {task['task_id']:task['title'] for task in workspace['tasks']}
                st.markdown('**更新任务建议**')
                st.write(action.get('title') or names.get(action['task_id'],'指定任务'))
                if action.get('status'): st.write('状态调整为：'+STATUS[action['status']])
                if action.get('completion_note'): st.write('完成依据：'+action['completion_note'])
                if action.get('blocker'): st.write('阻碍：'+action['blocker'])
                if action.get('description'): st.write(action['description'])
            elif kind=='add_note':
                st.markdown('**记录'+NOTES[action.get('note_kind','progress')]+'建议**')
                st.write(action['content'])
            elif kind=='add_resource':
                st.markdown('**关联资料建议**')
                st.write(action['label'])
                st.write(action['reference'])
            else:
                st.markdown('**调整项目建议**')
                for key,label in [('title','项目名称'),('goal','目标'),('archived','归档')]:
                    if key in action: st.write(label+'：'+str(action[key]))
            stale = proposal['base_revision']!=project['revision']
            st.caption('来自项目对话 · '+_localtime(proposal['created_at']))
            with st.expander('查看完整变更与来源'):
                st.json(action,expanded=True)
                st.caption('来源轮次：'+proposal['source_turn_id'])
            if stale:
                st.caption('项目已变化。此候选可忽略；需要时请在项目对话中重新生成。')
            a,b = st.columns(2)
            for column,decision,label in [(a,'apply','采纳变更'),(b,'reject','忽略候选')]:
                if column.button(label,key=proposal['proposal_id']+'_'+decision,disabled=disabled or (stale and decision=='apply')):
                    _send(api,user_id,f"/assistant/projects/{project['project_id']}/proposals/{proposal['proposal_id']}/{decision}",
                        {'expected_revision':project['revision']},proposal['proposal_id']+'_'+decision)


def _review(api,user_id,workspace):
    project = workspace['project']
    days = st.selectbox('复盘时间范围',[7,1,14,30],format_func=lambda days:f'最近{days}天',key=project['project_id']+'_review_days')
    if st.button('生成有依据的复盘',key='assistant_generate_review'):
        try:
            st.session_state.assistant_review = _get(api,user_id,f"/assistant/projects/{project['project_id']}/review",days=days)
        except (requests.RequestException,ValueError,KeyError):
            st.error('复盘暂时无法生成，请重试。')
    review = st.session_state.get('assistant_review')
    if review and review['project_id']==project['project_id']:
        if review['project_revision']!=project['revision']:
            st.caption('项目已有新变化，可以重新生成复盘。')
        st.markdown(review['markdown'])
        st.download_button('下载复盘 Markdown',review['markdown'],file_name='EchoMind-项目复盘.md',mime='text/markdown',key='assistant_review_download')
    with st.expander('变更来源与历史'):
        for event in workspace['events']:
            st.caption(f"版本 {event['revision']} · {event['kind']} · {event['source']} · {event['created_at']}")
            st.json(event['payload'],expanded=False)
        if workspace['events_has_more']:
            st.caption('展示最近30条变更；更早记录可由项目事件API分页读取。')


def render_assistant(api,user_id,*,disabled=False):
    st.markdown('<div class="assistant-eyebrow">ECHOMIND / PERSONAL WORKSPACE</div><h1 class="assistant-title">接上进度，继续向前。</h1>',unsafe_allow_html=True)
    st.caption('把目标、任务和资料放在一起。记住你的决定，让下一次开始更轻松。')
    pending = st.session_state.get('assistant_uncertain')
    if pending:
        st.warning('有一项保存结果待核对，其他修改暂时停用。')
        if st.button('重试原操作并核对',key='assistant_retry_pending'):
            _attempt(api,user_id,pending)
        disabled = True
    from reminders_ui import render_inbox, render_reminders
    render_inbox(api,user_id,disabled=disabled)
    pid = _project_picker(api,user_id,disabled)
    if not pid:
        return
    try:
        workspace = _get(api,user_id,'/assistant/projects/'+pid)
    except (requests.RequestException,ValueError,KeyError):
        st.error('此项目暂时无法打开。没有展示其他项目的旧数据，请刷新后重试。')
        return
    project = workspace['project']
    st.markdown('<div class="assistant-project"><span>当前目标</span><h2>'+escape(project['title'])+'</h2><p>'+escape(project['goal'] or '还没有填写完成标准。')+'</p></div>',unsafe_allow_html=True)
    counts = {status:sum(task['status']==status for task in workspace['tasks']) for status in STATUS}
    a,b,c = st.columns(3)
    a.metric('待推进',counts['todo']+counts['in_progress'])
    b.metric('已完成',counts['done'])
    c.metric('受阻',counts['blocked'])
    controls,chat = st.columns([3,1],vertical_alignment='bottom')
    minutes = controls.number_input('今天可以投入多少分钟？',min_value=5,max_value=480,value=60,step=5,key=pid+'_minutes')
    if chat.button('开始项目对话',type='primary',key='assistant_start_chat',disabled=disabled or project['archived'],width='stretch'):
        _send(api,user_id,'/conversations',{'scope_id':project['scope_id'],'project_id':pid},pid+'_conversation')
    try:
        recovery = _get(api,user_id,f'/assistant/projects/{pid}/resume',minutes=minutes)
        st.subheader('接下来做什么')
        if project['archived']:
            st.info('项目已归档。可查看记录和复盘，在项目设置中恢复后继续推进。')
        elif not recovery['next_steps']:
            st.info('暂无可推进的任务。可以添加下一步，或先处理受阻事项。')
        else:
            for index,step in enumerate(recovery['next_steps'],1):
                with st.container(border=True):
                    st.markdown(f"**{index}. {step['task']['title']}**")
                    st.caption(f"安排 {step['planned_minutes']} 分钟 · "+step['reason'])
            if recovery['unallocated_minutes']:
                st.caption(f"还留有 {recovery['unallocated_minutes']} 分钟，尚未安排。")
        for task in recovery['blocked_tasks']:
            st.warning(task['title']+'：'+task['blocker'])
        recent = [note for note in recovery.get('recent_notes',[]) if note['note_kind'] in ('progress','decision')][:3]
        if recent:
            with st.expander('上次的进展与决定',expanded=True):
                for note in recent:
                    st.caption(NOTES[note['note_kind']]+' · '+_localtime(note['created_at']))
                    st.write(note['content'][:600]+('…（完整内容见进展与学习）' if len(note['content'])>600 else ''))
    except (requests.RequestException,ValueError,KeyError):
        st.caption('下一步建议暂时不可用，仍可查看下方项目记录。')
    with st.expander('快速记录：明确命令'):
        area = pid+'_command'
        form = _form_key(area)
        with st.form(form):
            text = st.text_input('输入明确命令',key=form+'_text',placeholder='新增任务：整理实验结果；预计30分钟；优先级高',max_chars=5000)
            submit = st.form_submit_button('执行并保存',disabled=disabled or project['archived'])
        st.caption('支持新增任务、新增学习任务，以及记录进展、决定、阻碍、学习。自由描述请使用项目对话。')
        if submit:
            _send(api,user_id,f'/assistant/projects/{pid}/commands',{'text':text,'expected_revision':project['revision']},area)
    tabs = st.tabs(['任务','进展与学习','资料','助手候选','复盘','项目设置','提醒'])
    locked = disabled or project['archived']
    with tabs[0]: _tasks(api,user_id,workspace,locked)
    with tabs[1]: _notes(api,user_id,workspace,locked)
    with tabs[2]: _resources(api,user_id,workspace,locked)
    with tabs[3]: _proposals(api,user_id,workspace,locked)
    with tabs[4]: _review(api,user_id,workspace)
    with tabs[5]:
        area = pid+'_settings'
        form = _form_key(area)+'_'+str(project['revision'])
        with st.form(form):
            title = st.text_input('项目名称与标题',value=project['title'],max_chars=120,key=form+'_title')
            goal = st.text_area('调整目标与完成标准',value=project['goal'],max_chars=3000,key=form+'_goal')
            archived = st.checkbox('归档项目',value=project['archived'],key=form+'_archived')
            save = st.form_submit_button('保存项目设置',disabled=disabled)
        if save:
            _action(api,user_id,project,{'kind':'edit_project','title':title,'goal':goal,'archived':archived},area)
        st.caption('资料范围：'+project['scope_id'])
        st.caption(f"项目版本 {project['revision']} · 更新于 {project['updated_at']}")
    with tabs[6]: render_reminders(api,user_id,workspace,disabled=disabled)
    if workspace['conversations']:
        with st.expander('继续已有项目对话'):
            for conversation in workspace['conversations']:
                if st.button(conversation['title'],key='assistant_chat_'+conversation['conversation_id'],disabled=disabled):
                    from conversations_ui import load_conversation
                    if load_conversation(api,user_id,conversation['conversation_id']):
                        st.session_state._workspace_next_view = '知识对话'
                        st.rerun()

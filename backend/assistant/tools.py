"""Project-scoped tools: read facts or save a candidate, never silently mutate tasks."""
import json
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from langchain.tools import ToolRuntime, tool
from pydantic import ValidationError

from assistant.models import ACTION
from assistant.service import project_snapshot
from assistant.store import AssistantConflict, AssistantStore
from postgresql_client import get_postgresql_client
from schemas import ContextSchema


async def bound_store(runtime):
    context = runtime.context
    if not context.project_snapshot or not context.conversation_turn_id:
        raise ValueError('此会话未绑定项目。')
    project = context.project_snapshot['project']
    database = await get_postgresql_client()
    return AssistantStore(database.pool), context.user_id, project


@tool
async def get_project_workspace(runtime:ToolRuntime[ContextSchema],section:Literal['summary','tasks','notes','resources']='summary',offset:int=0,limit:int=10) -> str:
    """读取绑定项目最新状态。summary是有限摘要；tasks/notes/resources可分页读取完整记录，每页最多10条。不会读取资料正文。"""
    try:
        if not 0<=offset<=200 or not 1<=limit<=10:
            raise ValueError('分页范围超限。')
        store, user_id, project = await bound_store(runtime)
        workspace = await store.workspace(user_id, project['project_id'])
        # Keep the exact revision used by a later proposal in the request-local snapshot.
        snapshot = project_snapshot(workspace)
        runtime.context.project_snapshot.clear()
        runtime.context.project_snapshot.update(snapshot)
        result = {'status':'read','workspace':snapshot}
        if section!='summary':
            rows = workspace[section]
            result['records'] = rows[offset:offset+limit]
            result['section'],result['total'] = section,len(rows)
            result['next_offset'] = offset+limit if offset+limit<len(rows) else None
        return json.dumps(result,ensure_ascii=False)
    except (LookupError,ValueError) as error:
        return json.dumps({'status':'not_read','reason':str(error)},ensure_ascii=False)


@tool
async def propose_project_action(action:dict, runtime:ToolRuntime[ContextSchema]) -> str:
    """保存一项待用户采纳的项目变更，不能声称正式任务已改变。

    action示例：{"kind":"add_task","title":"整理实验结果","estimated_minutes":30}；
    多项计划应使用一项原子候选：{"kind":"add_tasks","tasks":[{"title":"阅读SSE","category":"study"},{"title":"实现流式输出"}]}；
    {"kind":"add_note","note_kind":"decision","content":"先完成演示"}；
    {"kind":"update_task","task_id":"实际任务UUID","status":"done","completion_note":"用户报告的完成依据"}。
    不得猜测任务ID、完成依据或用户已决定的事实。用户可以在工作台采纳或忽略候选。
    """
    try:
        validated = ACTION.validate_python(action)
        store, user_id, project = await bound_store(runtime)
        call_id = runtime.tool_call_id
        if not call_id:
            raise ValueError('缺少真实工具调用标识，未保存候选。')
        key = uuid5(NAMESPACE_URL, 'echomind-project:'+runtime.context.conversation_turn_id+':'+call_id)
        row = await store.propose(user_id, project['project_id'], key, project['revision'], validated,
                                  runtime.context.conversation_turn_id)
        return json.dumps({'status':'proposal_saved','proposal_id':row['proposal_id'],
            'proposal_status':row['status'],'action':row['action'],
            'message':'已保存候选；正式项目状态尚未改变，请到工作台核对并采纳。'},ensure_ascii=False)
    except (LookupError,ValueError,AssistantConflict,ValidationError) as error:
        return json.dumps({'status':'not_saved','reason':str(error)},ensure_ascii=False)

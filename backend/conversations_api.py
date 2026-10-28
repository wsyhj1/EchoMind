"""Conversation directory and idempotent turns; identity comes from middleware."""
import asyncio
import json
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, ConfigDict, field_validator

from agent import stream_agent_response, event
from conversation_store import ConversationConflict
from observability import RunTrace
from runtime_limits import ChatLimits
from skill_registry import resolve_command, SkillCommandError
from user_settings import SettingsValues
from assistant.store import AssistantConflict

router = APIRouter()


async def operation(awaitable):
    try:
        return await awaitable
    except LookupError as error:
        raise HTTPException(404, str(error)) from None
    except (ConversationConflict, AssistantConflict) as error:
        raise HTTPException(409, str(error)) from None


class CreateConversation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    scope_id: str = Field(min_length=1, max_length=128)
    client_create_id: UUID
    project_id: Optional[UUID] = None


class EditConversation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=1)
    title: Optional[str] = Field(default=None, min_length=1, max_length=160)
    archived: Optional[bool] = None

    @field_validator('title')
    @classmethod
    def clean_title(cls, value):
        if value is not None:
            value = ' '.join(value.split())
            if not value:
                raise ValueError('标题不能为空。')
        return value


class SendTurn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    query: str = Field(min_length=1, max_length=8000)
    client_turn_id: UUID
    expected_revision: int = Field(ge=1)


@router.post('/conversations', status_code=201)
async def create_conversation(payload: CreateConversation, request: Request, user_id: int = Query(ge=1)):
    if payload.project_id:
        return await operation(request.app.state.assistant_store.create_conversation(
            user_id,payload.project_id,payload.scope_id,payload.client_create_id))
    conversation = await operation(request.app.state.conversation_store.create(user_id, payload.scope_id, payload.client_create_id))
    assistant = getattr(request.app.state,'assistant_store',None)
    if assistant and await operation(assistant.for_conversation(user_id,conversation['conversation_id'])):
        raise HTTPException(409,'此创建标识已属于项目会话，请从对应项目打开。')
    return conversation


@router.get('/conversations')
async def list_conversations(request: Request, scope_id: str = Query(min_length=1, max_length=128),
                             user_id: int = Query(ge=1), archived: bool = False,
                             search: str = Query(default='', max_length=160), cursor: Optional[str] = Query(default=None,max_length=1000),
                             limit: int = Query(default=30, ge=1, le=100)):
    return await operation(request.app.state.conversation_store.list(user_id, scope_id,
        archived=archived, search=search, cursor=cursor, limit=limit))


@router.get('/conversations/legacy')
async def legacy_conversations(request: Request, user_id: int = Query(ge=1)):
    return {'items': await request.app.state.conversation_store.legacy_sessions(user_id)}


@router.get('/conversations/resolve-session/{session_id}')
async def resolve_session(session_id: str, request: Request, user_id: int = Query(ge=1)):
    conversation = await operation(request.app.state.conversation_store.by_session(user_id, session_id))
    if not conversation:
        raise HTTPException(404, '没有对应的新制会话。')
    return conversation


@router.get('/conversations/{conversation_id}')
async def get_conversation(conversation_id: UUID, request: Request, user_id: int = Query(ge=1)):
    return await operation(request.app.state.conversation_store.get(user_id, conversation_id))


@router.patch('/conversations/{conversation_id}')
async def edit_conversation(conversation_id: UUID, payload: EditConversation, request: Request,
                            user_id: int = Query(ge=1)):
    return await operation(request.app.state.conversation_store.edit(user_id, conversation_id,
        payload.expected_revision, title=payload.title, archived=payload.archived))


@router.get('/conversations/{conversation_id}/messages')
async def conversation_messages(conversation_id: UUID, request: Request, user_id: int = Query(ge=1),
                                before: Optional[int] = Query(default=None, ge=1),
                                limit: int = Query(default=40, ge=1, le=100)):
    result = await operation(request.app.state.conversation_store.messages(user_id, conversation_id,
                                                                        before=before, limit=limit))
    assistant = getattr(request.app.state,'assistant_store',None)
    if assistant:
        workspace = await operation(assistant.for_conversation(user_id,conversation_id))
        if workspace:
            result['conversation']['project_id'] = workspace['project']['project_id']
            result['conversation']['project_title'] = workspace['project']['title']
    return result


@router.post('/conversations/{conversation_id}/turns/stream')
async def send_turn(conversation_id: UUID, payload: SendTurn, request: Request,
                    background_tasks: BackgroundTasks, user_id: int = Query(ge=1)):
    try:
        command = resolve_command(payload.query)
    except SkillCommandError as error:
        raise HTTPException(422, str(error)) from None
    if not payload.query.strip() or (command and command.kind == 'local'):
        raise HTTPException(422, '请在界面执行本地命令，或输入问题。')
    store = request.app.state.conversation_store
    limits = ChatLimits.from_env()
    trace = RunTrace(user_id, 'pending', payload.query, 'pending')
    try:
        async with asyncio.timeout(limits.total_seconds):
            conversation = await operation(store.get(user_id, conversation_id))
            trace.session_id, trace.knowledge_base_id = conversation['session_id'], conversation['scope_id']
            settings = await request.app.state.user_settings_store.get(user_id)
            values = SettingsValues.model_validate(settings['values']).model_dump()
            trace.config['settings_revision'] = settings['revision']
            frozen = {'values': values, 'settings_revision': settings['revision'],
                      'skill_id': command.skill_id if command else None, 'skill_version': command.version if command else None}
            assistant = getattr(request.app.state,'assistant_store',None)
            project = await operation(assistant.for_conversation(user_id,conversation_id)) if assistant else None
            if project:
                from assistant.service import project_snapshot
                frozen['project_snapshot'] = project_snapshot(project)
                trace.config['project'] = frozen['project_snapshot']
            accepted = await operation(store.accept_turn(user_id, conversation_id, payload.client_turn_id,
                payload.expected_revision, payload.query, command.argument if command else payload.query,
                conversation['scope_id'], frozen, trace, limits.total_seconds+30))
    except TimeoutError:
        raise HTTPException(504, '会话准备超时，请用原发送标识重试或刷新核对。') from None
    turn = accepted['turn']
    envelope = {'conversation_id': str(conversation_id), 'turn_id': str(turn['turn_id']), 'run_id': str(turn['run_id'])}

    async def stream():
        if accepted['replayed']:
            # Never call the model again or reset the original budget/settings.
            snapshot = await request.app.state.evaluation_store.get_run(turn['run_id'], user_id)
            yield event('run', **envelope, replayed=True, session_id=conversation['session_id'],
                        skill=(snapshot or {}).get('config', {}).get('skill'))
            if turn['status'] == 'running':
                yield event('status', **envelope, content='此请求正在执行，请刷新会话查看结果。')
            elif turn['answer']:
                yield event('answer', **envelope, content=turn['answer'])
            if turn['status'] in ('failed', 'cancelled'):
                yield event('error', **envelope, content=(snapshot or {}).get('error') or '该请求未完成，请重新发送。')
            yield event('done', **envelope, status=turn['status'], replayed=True)
            return
        started_agent = False
        try:
            remaining = limits.total_seconds-trace.elapsed()/1000
            if remaining<=0:
                raise TimeoutError('本轮处理已达到时间上限。')
            async with asyncio.timeout(remaining):
                history = await store.history(user_id, conversation_id, before_sequence=turn['sequence'])
                await store.assert_active(user_id, conversation_id, turn['run_id'])
            started_agent = True
            async for raw in stream_agent_response(
                user_message=turn['model_query'], knowledge_base_id=conversation['scope_id'], user_id=user_id,
                background_tasks=background_tasks, trace=trace, store=store, session_id=conversation['session_id'],
                **values, skill_id=frozen['skill_id'], skill_version=frozen['skill_version'],
                conversation_history=history, conversation_turn_id=str(turn['turn_id']),
                **({'project_snapshot':frozen['project_snapshot']} if frozen.get('project_snapshot') else {})):
                data = json.loads(raw)
                yield json.dumps({**data, **envelope}, ensure_ascii=False, default=str)+'\n'
        except (asyncio.CancelledError, GeneratorExit):
            if not started_agent:
                trace.finish('', 'cancelled')
                try:
                    await asyncio.wait_for(asyncio.shield(store.save_run(trace.snapshot())),5)
                except Exception:
                    pass  # Lease expiry remains the final backstop.
            raise
        except Exception as error:
            trace.finish('', 'failed', error)
            try:
                await asyncio.wait_for(asyncio.shield(store.save_run(trace.snapshot())),5)
            except Exception:
                pass
            yield event('error', **envelope, content='会话上下文加载失败，请刷新后重试。')
            yield event('done', **envelope, status='failed')

    return StreamingResponse(stream(), media_type='application/x-ndjson',
                             headers={'Cache-Control': 'no-store'})

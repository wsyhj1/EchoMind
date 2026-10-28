"""Personal workspace API. Authentication is inherited from IdentityMiddleware."""
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import ValidationError

from assistant.models import ApplyAction, CommandRequest, CreateProject, ProposalDecision
from assistant.service import build_review, parse_command, resume_workspace
from assistant.store import AssistantConflict
from assistant.reminders import CreateReminder, ReminderDecision, SnoozeReminder, ReadNotification

router = APIRouter(prefix='/assistant', tags=['personal-assistant'])


async def operation(awaitable):
    try:
        return await awaitable
    except LookupError as error:
        raise HTTPException(404, str(error)) from None
    except AssistantConflict as error:
        raise HTTPException(409, str(error)) from None


@router.get('/projects')
async def projects(request:Request, user_id:int=Query(ge=1), archived:bool=False):
    return {'items':await operation(request.app.state.assistant_store.list(user_id, archived=archived))}


@router.post('/projects',status_code=201)
async def create_project(payload:CreateProject, request:Request, user_id:int=Query(ge=1)):
    return await operation(request.app.state.assistant_store.create(user_id, payload))


@router.get('/projects/{project_id}')
async def workspace(project_id:UUID, request:Request, user_id:int=Query(ge=1)):
    return await operation(request.app.state.assistant_store.workspace(user_id, project_id))


@router.post('/projects/{project_id}/actions')
async def apply_action(project_id:UUID, payload:ApplyAction, request:Request, user_id:int=Query(ge=1)):
    return await operation(request.app.state.assistant_store.apply(user_id, project_id, payload.operation_id,
        payload.expected_revision, payload.action))


@router.post('/projects/{project_id}/commands')
async def command(project_id:UUID, payload:CommandRequest, request:Request, user_id:int=Query(ge=1)):
    try:
        action = parse_command(payload.text)
    except (ValueError, ValidationError) as error:
        raise HTTPException(422, str(error)) from None
    return await operation(request.app.state.assistant_store.apply(user_id, project_id, payload.operation_id,
        payload.expected_revision, action, source='user_command'))


@router.post('/projects/{project_id}/proposals/{proposal_id}/{decision}')
async def decide(project_id:UUID, proposal_id:UUID, decision:str, payload:ProposalDecision,
                 request:Request, user_id:int=Query(ge=1)):
    if decision not in ('apply','reject'):
        raise HTTPException(404,'未知候选操作。')
    return await operation(request.app.state.assistant_store.decide(user_id, project_id, proposal_id, decision,
        payload.operation_id, payload.expected_revision))


@router.get('/projects/{project_id}/resume')
async def resume(project_id:UUID, request:Request, user_id:int=Query(ge=1), minutes:int=Query(default=60,ge=5,le=480)):
    workspace = await operation(request.app.state.assistant_store.workspace(user_id, project_id))
    return resume_workspace(workspace, minutes)


@router.get('/projects/{project_id}/events')
async def events(project_id:UUID, request:Request, user_id:int=Query(ge=1),
                 before_revision:int|None=Query(default=None,ge=1), limit:int=Query(default=50,ge=1,le=100)):
    return await operation(request.app.state.assistant_store.events(user_id, project_id, before_revision=before_revision,limit=limit))


@router.get('/projects/{project_id}/review')
async def review(project_id:UUID, request:Request, user_id:int=Query(ge=1), days:int=Query(default=7,ge=1,le=90)):
    store = request.app.state.assistant_store
    # One read snapshot for state and events prevents a concurrent mutation mixing revisions.
    async with store.pool.acquire() as conn, conn.transaction(isolation='repeatable_read',readonly=True):
        from assistant.store import AssistantStore
        # asyncpg connection and pool share fetch APIs; the small bound adapter reuses this transaction.
        bound = BoundConnection(conn)
        snapshot_store = AssistantStore(bound)
        now = await conn.fetchval('SELECT clock_timestamp()')
        since = now-timedelta(days=days)
        workspace = await operation(snapshot_store.workspace(user_id, project_id))
        event_rows = await operation(snapshot_store.events(user_id, project_id, since=since,limit=1000))
    return build_review(workspace,event_rows,since,now=now)


class BoundConnection:
    """Reuse an existing repeatable-read transaction without opening a second connection."""
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        from contextlib import asynccontextmanager
        @asynccontextmanager
        async def current():
            yield self.connection
        return current()


@router.post('/projects/{project_id}/reminders', status_code=201)
async def create_reminder(project_id:UUID, payload:CreateReminder, request:Request, user_id:int=Query(ge=1)):
    return await operation(request.app.state.reminder_store.create(user_id, project_id, payload))


@router.get('/projects/{project_id}/reminders')
async def reminders(project_id:UUID, request:Request, user_id:int=Query(ge=1),
                    offset:int=Query(default=0,ge=0), limit:int=Query(default=30,ge=1,le=100)):
    return await operation(request.app.state.reminder_store.list(user_id, project_id, offset=offset, limit=limit))


@router.post('/projects/{project_id}/reminders/{reminder_id}/cancel')
async def cancel_reminder(project_id:UUID, reminder_id:UUID, payload:ReminderDecision, request:Request, user_id:int=Query(ge=1)):
    return await operation(request.app.state.reminder_store.change(user_id, project_id, reminder_id, 'cancel', payload))


@router.post('/projects/{project_id}/reminders/{reminder_id}/snooze')
async def snooze_reminder(project_id:UUID, reminder_id:UUID, payload:SnoozeReminder, request:Request, user_id:int=Query(ge=1)):
    return await operation(request.app.state.reminder_store.change(user_id, project_id, reminder_id, 'snooze', payload))


@router.get('/notifications')
async def notifications(request:Request, user_id:int=Query(ge=1), unread:bool=True,
                        offset:int=Query(default=0,ge=0), limit:int=Query(default=20,ge=1,le=100)):
    return await operation(request.app.state.reminder_store.inbox(user_id, unread=unread, offset=offset, limit=limit))


@router.post('/notifications/{notification_id}/read')
async def read_notification(notification_id:UUID, payload:ReadNotification, request:Request, user_id:int=Query(ge=1)):
    return await operation(request.app.state.reminder_store.read(user_id, notification_id, payload))


@router.get('/reminders/status')
async def reminder_status(request:Request, user_id:int=Query(ge=1)):
    return await operation(request.app.state.reminder_store.status())

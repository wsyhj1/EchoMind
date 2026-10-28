"""Graph management router. Mounted behind the application's trusted identity middleware."""
import asyncio

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from graph.management import GraphManagement, GraphManagementError

router = APIRouter()


class GraphJobRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    user_id: StrictInt = Field(gt=0)
    knowledge_base_id: str = Field(min_length=1, max_length=128)
    file_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    external_model_consent: StrictBool = False
    consent_target: str = Field(pattern=r'^[0-9a-f]{64}$')
    force: StrictBool = False


class GraphCancelRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    user_id: StrictInt = Field(gt=0)
    knowledge_base_id: str = Field(min_length=1, max_length=128)
    file_hash: str = Field(pattern=r'^[0-9a-f]{64}$')


def manager(request):
    return GraphManagement(request.app.state.postgresql_client.pool)


def identity(request, user_id):
    from auth import require_matching_user
    return require_matching_user(request, user_id)


async def perform(awaitable):
    try:
        return await asyncio.wait_for(awaitable, timeout=15)
    except GraphManagementError as error:
        raise HTTPException(error.status, detail={'code': error.code, 'message': error.message}) from None
    except asyncio.TimeoutError:
        raise HTTPException(504, detail={'code': 'GraphManagementTimeout', 'message': '图谱管理操作超时，请刷新状态核对'}) from None


@router.get('/graph-status')
async def graph_status(request: Request, user_id: int = Query(gt=0), knowledge_base_id: str = Query(min_length=1, max_length=128),
                       limit: int = Query(default=50, ge=1, le=100), offset: int = Query(default=0, ge=0, le=100000)):
    identity(request, user_id)
    return await perform(manager(request).status(user_id, knowledge_base_id, limit=limit, offset=offset))


@router.post('/graph-jobs')
async def graph_submit(request: Request, body: GraphJobRequest):
    identity(request, body.user_id)
    return await perform(manager(request).submit(body.user_id, body.knowledge_base_id, body.file_hash,
                                                consent=body.external_model_consent, force=body.force, consent_target=body.consent_target))


@router.post('/graph-jobs/cancel')
async def graph_cancel(request: Request, body: GraphCancelRequest):
    identity(request, body.user_id)
    return await perform(manager(request).cancel(body.user_id, body.knowledge_base_id, body.file_hash))

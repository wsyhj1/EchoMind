"""IdentityMiddleware must guard this router, as it guards existing run APIs."""
from uuid import UUID
from fastapi import APIRouter, HTTPException, Path, Query, Request
from auth import require_matching_user

router = APIRouter()


async def _validation(request, run_id, user_id, citation_id=None):
    require_matching_user(request, user_id)
    service = getattr(request.app.state, "citation_service", None)
    if service is None:
        raise HTTPException(503, "Citation validation unavailable")
    try:
        return await service.validate(run_id, user_id, include_citation=citation_id)
    except LookupError:
        raise HTTPException(404, "Run or citation not found") from None


@router.get("/runs/{run_id}/citations")
async def validate_citations(run_id: UUID, request: Request, user_id: int = Query(ge=1)):
    return await _validation(request, run_id, user_id)


@router.get("/runs/{run_id}/citations/{citation_id}")
async def citation_source(run_id: UUID, request: Request,
                          citation_id: str = Path(pattern=r"^c[1-9]\d{0,4}$"),
                          user_id: int = Query(ge=1)):
    return await _validation(request, run_id, user_id, citation_id)

"""Process liveness and bounded PostgreSQL readiness."""

from typing import Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from app.db.session import get_database

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


class ReadyResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    database: Literal["ok", "unavailable"]


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse()


@router.get("/ready", response_model=ReadyResponse, responses={503: {"model": ReadyResponse}})
async def ready(request: Request, response: Response) -> ReadyResponse:
    if not request.app.state.ready or not await get_database(request).is_available():
        response.status_code = 503
        return ReadyResponse(status="not_ready", database="unavailable")
    return ReadyResponse(status="ready", database="ok")

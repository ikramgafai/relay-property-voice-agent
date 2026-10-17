"""Internal/demo-only, read-only session trace for the voice demo's system inspector."""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy.exc import SQLAlchemyError

from app.api.turns import internal_only
from app.db.session import get_database
from app.db.trace import SessionTrace, SQLAlchemyTraceReader

router = APIRouter(prefix="/api")


@router.get("/demo/sessions/{session_id}/trace", response_model=SessionTrace)
async def session_trace(session_id: UUID, request: Request) -> SessionTrace:
    internal_only(request)
    try:
        trace = await SQLAlchemyTraceReader(get_database(request)).read(session_id)
    except (SQLAlchemyError, OSError, TimeoutError):
        raise HTTPException(503, "TRACE_STORAGE_UNAVAILABLE") from None
    if trace is None:
        raise HTTPException(404, "SESSION_NOT_FOUND")
    return trace

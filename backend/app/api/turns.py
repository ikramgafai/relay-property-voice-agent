"""Internal/demo-only text transport. Public authentication is deliberately deferred."""

from typing import Literal, cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy.exc import SQLAlchemyError

from app.agent.errors import TurnError
from app.agent.orchestrator import TurnOrchestrator
from app.agent.schemas import AgentTurnRequest, AgentTurnResult, DemoSession, TurnInput
from app.db.agent import SQLAlchemyTurnStore
from app.db.session import get_database
from app.property.errors import PropertyContextError
from app.property.schemas import DomainModel
from app.workflows.errors import WorkflowError

router = APIRouter(prefix="/api")


class DemoInput(DomainModel):
    resident: Literal["Ikram", "Alex"] = "Ikram"


def internal_only(request: Request) -> None:
    if request.app.state.settings.app_env == "production":
        raise HTTPException(403, "Internal demo endpoints are disabled in production.")


@router.post("/demo/sessions", response_model=DemoSession)
async def create_demo_session(body: DemoInput, request: Request) -> DemoSession:
    internal_only(request)
    try:
        return await SQLAlchemyTurnStore(get_database(request)).create_demo(body.resident)
    except TurnError as exc:
        raise HTTPException(exc.status_code, exc.code) from None
    except (SQLAlchemyError, PropertyContextError):
        raise HTTPException(503, "SESSION_STORAGE_UNAVAILABLE") from None


@router.post("/sessions/{session_id}/turns", response_model=AgentTurnResult)
async def text_turn(session_id: UUID, body: TurnInput, request: Request) -> AgentTurnResult:
    internal_only(request)
    orchestrator = cast(TurnOrchestrator, request.app.state.orchestrator)
    try:
        return await orchestrator.process(
            AgentTurnRequest(session_id=session_id, **body.model_dump())
        )
    except TurnError as exc:
        raise HTTPException(exc.status_code, exc.code) from None
    except WorkflowError:
        raise HTTPException(409, "WORKFLOW_STATE_CONFLICT") from None
    except PropertyContextError:
        raise HTTPException(409, "PROPERTY_CONTEXT_UNAVAILABLE") from None
    except (SQLAlchemyError, OSError, TimeoutError):
        raise HTTPException(503, "TURN_STORAGE_UNAVAILABLE") from None

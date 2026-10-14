"""Local execution coordinator: success can leave only after commit and verification."""

from datetime import UTC, datetime
from time import perf_counter
from uuid import UUID, uuid4

import structlog
from pydantic import ValidationError

from app.tools.errors import StorageFailure, ToolFailure
from app.tools.protocol import ToolStore
from app.tools.registry import TOOLS
from app.tools.schemas import OUTPUT_KINDS, ErrorCode, ToolResult, ToolStatus
from app.tools.validation import authorize, request_hash, validate_declaration
from app.workflows.schemas import ActionRequest, ActionType


class ToolExecutor:
    def __init__(self, store: ToolStore) -> None:
        self.store = store

    async def execute(self, session_id: UUID, request: ActionRequest) -> ToolResult:
        tick = perf_counter()
        now = datetime.now(UTC)
        identity = request.action_id if isinstance(request.action_id, UUID) else uuid4()
        action_type = (
            request.action_type.value
            if isinstance(request.action_type, ActionType)
            else "UNSUPPORTED"
        )
        result = ToolResult(
            execution_id=uuid4(),
            action_request_id=identity,
            action_type=action_type,
            status=ToolStatus.PENDING,
            started_at=now,
            duration_ms=0,
        )
        digest: str | None = None
        try:
            if action_type == "UNSUPPORTED":
                raise ToolFailure(ErrorCode.UNSUPPORTED)
            request = ActionRequest.model_validate(request.model_dump())
            digest = request_hash(session_id, request)
            async with self.store.transaction() as transaction:
                source = await transaction.source(session_id, request)
                previous = await transaction.existing(request, digest)
                if previous and previous.status in {
                    ToolStatus.SUCCEEDED,
                    ToolStatus.PENDING,
                    ToolStatus.UNKNOWN_OUTCOME,
                }:
                    result = previous.model_copy(update={"replayed": True})
                else:
                    validate_declaration(source, request)
                    if previous:
                        result.execution_id = previous.execution_id
                    await transaction.record(source, request, digest, result)
                    try:
                        authorize(source, request)
                        tool = TOOLS.get(request.action_type)
                        if tool is None:
                            raise ToolFailure(ErrorCode.UNSUPPORTED)
                        async with transaction.operation():
                            output = await tool(transaction.operations, source, request)
                            if output.kind != OUTPUT_KINDS[request.action_type]:
                                raise ToolFailure(ErrorCode.VALIDATION)
                        result = result.model_copy(
                            update={
                                "status": ToolStatus.SUCCEEDED,
                                "operation_id": output.operation_id,
                                "result": output,
                                "completed_at": datetime.now(UTC),
                                "duration_ms": (perf_counter() - tick) * 1000,
                            }
                        )
                    except ToolFailure as exc:
                        result = self._failure(result, exc.code, tick)
                    await transaction.record(source, request, digest, result)
            # The context manager has committed. The provisional in-transaction success
            # above is private; verify its durable receipt and operation using a fresh transaction.
            verified = await self.store.verify(session_id, request, digest)
            if verified is None:
                raise StorageFailure()
            result = verified.model_copy(update={"replayed": result.replayed})
        except ValidationError:
            result = self._failure(result, ErrorCode.VALIDATION, tick)
        except ToolFailure as exc:
            result = self._failure(result, exc.code, tick)
        except StorageFailure:
            # A commit acknowledgment failure is not proof of rollback. Reconcile once;
            # never re-execute on this path. Unavailable verification means UNKNOWN_OUTCOME.
            try:
                verified = await self.store.verify(session_id, request, digest) if digest else None
                if verified is not None:
                    result = verified.model_copy(update={"replayed": True})
                else:
                    result = self._failure(result, ErrorCode.DATABASE, tick)
            except (StorageFailure, ToolFailure):
                result = self._failure(result, ErrorCode.UNKNOWN, tick)
        self._log(session_id, request, result, (perf_counter() - tick) * 1000)
        return result

    @staticmethod
    def _failure(result: ToolResult, code: ErrorCode, tick: float) -> ToolResult:
        return result.model_copy(
            update={
                "status": ToolStatus.UNKNOWN_OUTCOME
                if code == ErrorCode.UNKNOWN
                else ToolStatus.FAILED,
                "operation_id": None,
                "result": None,
                "error_code": code,
                "safe_error_message": "The operation has not been confirmed successful.",
                "completed_at": datetime.now(UTC),
                "duration_ms": (perf_counter() - tick) * 1000,
                "replayed": False,
            }
        )

    @staticmethod
    def _log(session_id: UUID, request: ActionRequest, result: ToolResult, duration: float) -> None:
        structlog.get_logger().info(
            "TOOL_SUCCEEDED" if result.status == ToolStatus.SUCCEEDED else "TOOL_FAILED",
            execution_id=str(result.execution_id),
            action_request_id=str(result.action_request_id),
            action_type=result.action_type,
            session_id=str(session_id),
            workflow_id=str(request.source_workflow_id),
            property_id=str(request.payload.property_id),
            status=result.status.value,
            replayed=result.replayed,
            duration_ms=round(duration, 3),
            operation_id=str(result.operation_id) if result.operation_id else None,
            error_code=result.error_code.value if result.error_code else None,
        )

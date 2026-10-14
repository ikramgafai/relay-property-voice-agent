"""Single-process connection ownership, independent of durable conversation state."""

import asyncio
from uuid import UUID

import structlog

from app.realtime.errors import RealtimeError
from app.realtime.protocol import SessionLookup, TurnProcessor
from app.realtime.schemas import Code
from app.realtime.session import RealtimeSession


class RealtimeSessionManager:
    def __init__(
        self,
        lookup: SessionLookup,
        processor: TurnProcessor,
        *,
        queue_capacity: int = 32,
        queue_timeout: float = 1,
    ) -> None:
        self.lookup, self.processor = lookup, processor
        self.queue_capacity, self.queue_timeout = queue_capacity, queue_timeout
        self._sessions: dict[UUID, RealtimeSession] = {}
        self._pending: set[UUID] = set()
        self._lock = asyncio.Lock()
        self._closing = False
        self.duplicate_attempts = 0

    @property
    def active_count(self) -> int:
        return len(self._sessions)

    async def bind(self, session_id: UUID) -> RealtimeSession:
        async with self._lock:
            if self._closing:
                raise RealtimeError(Code.SESSION_UNAVAILABLE)
            if session_id in self._sessions or session_id in self._pending:
                self.duplicate_attempts += 1
                structlog.get_logger().info(
                    "REALTIME_DUPLICATE_CONNECTION",
                    session_id=str(session_id),
                    duplicate_attempts=self.duplicate_attempts,
                )
                raise RealtimeError(Code.DUPLICATE_CONNECTION)
            self._pending.add(session_id)
        try:
            binding = await self.lookup(session_id)
            value = RealtimeSession(
                session_id,
                binding,
                self.processor,
                queue_capacity=self.queue_capacity,
                queue_timeout=self.queue_timeout,
                lookup=self.lookup,
            )
            async with self._lock:
                if self._closing:
                    raise RealtimeError(Code.SESSION_UNAVAILABLE)
                self._sessions[session_id] = value
                value.log("REALTIME_SESSION_CONNECTED", active_sessions=self.active_count)
            return value
        finally:
            async with self._lock:
                self._pending.discard(session_id)

    async def release(self, value: RealtimeSession) -> None:
        await value.end()
        async with self._lock:
            if self._sessions.get(value.conversation_session_id) is value:
                del self._sessions[value.conversation_session_id]
            value.log("REALTIME_SESSION_RELEASED", active_sessions=self.active_count)

    async def get(self, session_id: UUID) -> RealtimeSession | None:
        async with self._lock:
            return self._sessions.get(session_id)

    async def terminate(self, session_id: UUID) -> None:
        value = await self.get(session_id)
        if value is not None:
            value.request_stop("SERVER_SHUTDOWN")
            await value.end()

    async def shutdown(self) -> None:
        async with self._lock:
            self._closing = True
            sessions = list(self._sessions.values())
        for value in sessions:
            value.request_stop("SERVER_SHUTDOWN")
        await asyncio.gather(*(self.release(value) for value in sessions))

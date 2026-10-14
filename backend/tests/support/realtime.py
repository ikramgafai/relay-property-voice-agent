"""Deterministic ASGI WebSocket driver; same loop as PostgreSQL and pause seams."""

import asyncio
import json
from uuid import UUID

from app.agent.schemas import AgentTurnRequest
from app.realtime.schemas import ClientEvent, ClientPayload, ServerEvent, Start, TextTurn
from fastapi import FastAPI
from starlette.types import Message, Scope


class Socket:
    def __init__(self, app: FastAPI, session_id: UUID) -> None:
        self.app, self.session_id = app, session_id
        self.incoming: asyncio.Queue[Message] = asyncio.Queue()
        self.outgoing: asyncio.Queue[Message] = asyncio.Queue()
        self.task: asyncio.Task[None] | None = None
        self.events: list[ServerEvent] = []
        self.sequence = 0
        self.block_send: asyncio.Event | None = None
        self.send_entered = asyncio.Event()

    async def _send(self, message: Message) -> None:
        if message["type"] == "websocket.send" and self.block_send is not None:
            self.send_entered.set()
            await self.block_send.wait()
        await self.outgoing.put(message)

    async def connect(self) -> Message:
        scope: Scope = {
            "type": "websocket",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "scheme": "ws",
            "path": f"/api/realtime/{self.session_id}",
            "root_path": "",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("test", 80),
            "subprotocols": [],
            "state": {},
        }
        self.task = asyncio.create_task(
            self.app(scope, self.incoming.get, self._send), name="test-ws-connection"
        )
        await self.incoming.put({"type": "websocket.connect"})
        return await self.message()

    async def message(self) -> Message:
        async with asyncio.timeout(5):
            return await self.outgoing.get()

    async def receive(self) -> ServerEvent:
        message = await self.message()
        assert message["type"] == "websocket.send", message
        event = ServerEvent.model_validate_json(message["text"])
        assert event.session_id == self.session_id
        assert event.sequence > (self.events[-1].sequence if self.events else 0)
        assert event.event_id not in {e.event_id for e in self.events}
        self.events.append(event)
        return event

    async def send(self, payload: ClientPayload) -> None:
        self.sequence += 1
        event = ClientEvent(
            session_id=self.session_id,
            sequence=self.sequence,
            event_type=payload.kind,
            turn_id=getattr(payload, "turn_id", None),
            payload=payload,
        )
        await self.raw(event.model_dump_json())

    async def raw(self, raw: str) -> None:
        await self.incoming.put({"type": "websocket.receive", "text": raw})

    async def start(self) -> None:
        assert (await self.connect())["type"] == "websocket.accept"
        await self.send(Start())
        assert (await self.receive()).event_type == "SESSION_STARTED"
        assert (await self.receive()).payload.model_dump()["state"] == "IDLE"

    async def submit(self, request: AgentTurnRequest) -> None:
        await self.send(TextTurn(**request.model_dump(exclude={"session_id"})))

    async def turn(self, request: AgentTurnRequest) -> list[ServerEvent]:
        await self.submit(request)
        return await self.until_idle()

    async def until_idle(self) -> list[ServerEvent]:
        result = []
        while True:
            event = await self.receive()
            result.append(event)
            if (
                event.event_type == "SESSION_STATE_CHANGED"
                and json.loads(event.model_dump_json())["payload"]["state"] == "IDLE"
            ):
                return result

    async def close(self) -> None:
        await self.incoming.put({"type": "websocket.disconnect", "code": 1000})
        await self.join()

    async def join(self) -> None:
        assert self.task
        async with asyncio.timeout(5):
            await self.task


def live_relay_tasks() -> set[asyncio.Task[object]]:
    return {task for task in asyncio.all_tasks() if task.get_name().startswith("relay-")}

"""Text-only WebSocket adapter. This module owns every connection IO task."""

import asyncio
import json
from typing import cast
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from app.agent.errors import TurnError
from app.realtime.errors import Backpressure, RealtimeError, application_code
from app.realtime.manager import RealtimeSessionManager
from app.realtime.schemas import ClientEvent, Code, Error, ServerEvent
from app.realtime.session import RealtimeSession

router = APIRouter(prefix="/api")
MAX_EVENT_BYTES = 32768
SEND_TIMEOUT = 5.0
DRAIN_TIMEOUT = 2.0


async def receive_events(socket: WebSocket, session: RealtimeSession) -> None:
    while not session.stopped.is_set():
        message = await socket.receive()
        if message["type"] == "websocket.disconnect":
            session.request_stop("DISCONNECTED")
            return
        raw = message.get("text")
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_EVENT_BYTES:
            await session.error(Code.INVALID_EVENT)
            continue
        try:
            event = ClientEvent.model_validate_json(raw)
        except ValidationError:
            try:
                parsed = json.loads(raw)
            except (ValueError, RecursionError):
                parsed = None
            code = Code.INVALID_EVENT
            if isinstance(parsed, dict) and parsed.get("event_type") not in {
                "SESSION_START",
                "CLIENT_TEXT_TURN",
                "CLIENT_CANCEL_TURN",
                "PING",
                "SESSION_END",
            }:
                code = Code.UNSUPPORTED_EVENT
            await session.error(code)
            continue
        await session.handle(event)


async def send_events(socket: WebSocket, session: RealtimeSession) -> None:
    while True:
        event = await session.outbound.items.get()
        try:
            async with asyncio.timeout(SEND_TIMEOUT):
                await socket.send_json(event.model_dump(mode="json"))
            session.log(
                "REALTIME_EVENT_SENT",
                event_id=str(event.event_id),
                sequence=event.sequence,
                event_type=event.event_type,
                turn_id=str(event.turn_id) if event.turn_id else None,
            )
        except TimeoutError:
            session.request_stop("BACKPRESSURE")
            session.log(
                "REALTIME_SEND_TIMEOUT",
                event_id=str(event.event_id),
                sequence=event.sequence,
                event_type=event.event_type,
                replay_required=True,
            )
            return
        except Exception:
            session.request_stop("SEND_FAILED")
            session.log(
                "REALTIME_SEND_FAILED",
                event_id=str(event.event_id),
                sequence=event.sequence,
                event_type=event.event_type,
                replay_required=True,
            )
            return
        finally:
            session.outbound.items.task_done()


@router.websocket("/realtime/{session_id}")
async def realtime(socket: WebSocket, session_id: UUID) -> None:
    if socket.app.state.settings.app_env == "production":
        await socket.close(code=1008)
        return
    await socket.accept()
    manager = cast(RealtimeSessionManager, socket.app.state.realtime_manager)
    try:
        session = await manager.bind(session_id)
    except (RealtimeError, TurnError) as exc:
        code = exc.code if isinstance(exc, RealtimeError) else application_code(exc)
        await reject(socket, session_id, code)
        return
    except Exception:
        await reject(socket, session_id, Code.INTERNAL_ERROR)
        return
    reader = asyncio.create_task(receive_events(socket, session), name="relay-ws-reader")
    sender = asyncio.create_task(send_events(socket, session), name="relay-ws-sender")
    stopper = asyncio.create_task(session.stopped.wait(), name="relay-ws-stop")
    try:
        done, _ = await asyncio.wait({reader, sender, stopper}, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if not task.cancelled() and task.exception() is not None:
                if not isinstance(task.exception(), Backpressure):
                    session.request_stop("INTERNAL_ERROR")
    finally:
        reader.cancel()
        stopper.cancel()
        await asyncio.gather(reader, stopper, return_exceptions=True)
        try:
            graceful = session.reason.reason in {"CLIENT_END", "SESSION_UNAVAILABLE"}
            await session.end(notify=graceful)
            if graceful and not sender.done():
                try:
                    async with asyncio.timeout(DRAIN_TIMEOUT):
                        await session.outbound.items.join()
                except TimeoutError:
                    session.request_stop("BACKPRESSURE")
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
            undelivered = session.outbound.discard_on_close()
            session.log(
                "REALTIME_TRANSPORT_CLOSED",
                undelivered_events=undelivered,
                replay_required=session.reason.reason != "CLIENT_END",
            )
            await manager.release(session)
            close_code = (
                1013
                if session.reason.reason == "BACKPRESSURE"
                else (1011 if session.reason.reason in {"INTERNAL_ERROR", "SEND_FAILED"} else 1000)
            )
            reason = "BACKPRESSURE_REPLAY_REQUIRED" if close_code == 1013 else session.reason.reason
            try:
                async with asyncio.timeout(SEND_TIMEOUT):
                    await socket.close(code=close_code, reason=reason)
            except (RuntimeError, OSError, TimeoutError, WebSocketDisconnect):
                pass


async def reject(socket: WebSocket, session_id: UUID, code: Code) -> None:
    try:
        async with asyncio.timeout(SEND_TIMEOUT):
            await socket.send_json(
                ServerEvent(
                    session_id=session_id, sequence=1, event_type="ERROR", payload=Error(code=code)
                ).model_dump(mode="json")
            )
            await socket.close(code=1008, reason=code.value)
    except (RuntimeError, OSError, TimeoutError, WebSocketDisconnect):
        pass

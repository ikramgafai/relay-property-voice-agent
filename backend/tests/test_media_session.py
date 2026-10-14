import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from uuid import uuid4

import jwt
import pytest
from app.core.config import Settings
from app.realtime.media.livekit import (
    LiveKitAudioSource,
    LiveKitMediaSession,
    MediaSessionUnavailable,
    participant_name,
    server_name,
    token,
)
from livekit import rtc
from pydantic import SecretStr


class FakeRoom:
    def __init__(self) -> None:
        self.handlers: dict[str, list[Callable[..., None]]] = {}
        self.remote_participants: dict[str, FakeParticipant] = {}
        self.connects = self.disconnects = 0
        self.gate: asyncio.Event | None = None
        self.fail = False

    def on(self, event: str, handler: Callable[..., None]) -> Callable[..., None]:
        self.handlers.setdefault(event, []).append(handler)
        return handler

    async def connect(self, url: str, token: str, options: object = None) -> None:
        self.connects += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            raise ConnectionError("refused")

    async def disconnect(self) -> None:
        self.disconnects += 1

    def emit(self, event: str, *args: object) -> None:
        for handler in self.handlers.get(event, []):
            handler(*args)


@dataclass
class FakePublication:
    sid: str
    kind: rtc.TrackKind.ValueType = rtc.TrackKind.KIND_AUDIO
    source: rtc.TrackSource.ValueType = rtc.TrackSource.SOURCE_MICROPHONE
    subscribed: list[bool] = field(default_factory=list)

    def set_subscribed(self, value: bool) -> None:
        self.subscribed.append(value)


@dataclass
class FakeParticipant:
    identity: str
    track_publications: dict[str, FakePublication] = field(default_factory=dict)


@pytest.fixture
def room(monkeypatch: pytest.MonkeyPatch) -> FakeRoom:
    fake = FakeRoom()
    monkeypatch.setattr(rtc, "Room", lambda: fake)
    return fake


def settings() -> Settings:
    return Settings(
        livekit_url="ws://livekit:7880",
        livekit_api_key=SecretStr("devkey"),
        livekit_api_secret=SecretStr("secret"),
    )


async def test_ensure_connected_is_idempotent(room: FakeRoom) -> None:
    media = LiveKitMediaSession(settings(), uuid4())
    await media.ensure_connected()
    await media.ensure_connected()
    assert room.connects == 1 and media.connected and media.identity.startswith("relay-server-")


async def test_concurrent_ensure_connected_makes_one_connection(room: FakeRoom) -> None:
    media = LiveKitMediaSession(settings(), uuid4())
    room.gate = asyncio.Event()
    waiters = [asyncio.create_task(media.ensure_connected()) for _ in range(5)]
    await asyncio.sleep(0)
    room.gate.set()
    await asyncio.gather(*waiters)
    assert room.connects == 1 and media.connected


async def test_connection_failure_is_terminal_and_cleaned_up(room: FakeRoom) -> None:
    media = LiveKitMediaSession(settings(), uuid4())
    room.fail = True
    with pytest.raises(ConnectionError):
        await media.ensure_connected()
    assert media.failed and not media.connected
    with pytest.raises(MediaSessionUnavailable):
        await media.ensure_connected()
    assert room.connects == 1
    await media.close()
    assert room.disconnects == 1 and media.closed


async def test_cancelled_connection_is_terminal(room: FakeRoom) -> None:
    media = LiveKitMediaSession(settings(), uuid4())
    room.gate = asyncio.Event()
    task = asyncio.create_task(media.ensure_connected())
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert media.failed and not media.connected
    await media.close()
    assert room.disconnects == 1


async def test_close_during_connect_disconnects_and_refuses(room: FakeRoom) -> None:
    media = LiveKitMediaSession(settings(), uuid4())
    room.gate = asyncio.Event()
    connecting = asyncio.create_task(media.ensure_connected())
    await asyncio.sleep(0)
    await media.close()
    room.gate.set()
    with pytest.raises(MediaSessionUnavailable):
        await connecting
    assert not media.connected and room.disconnects == 2


async def test_media_session_close_is_idempotent(room: FakeRoom) -> None:
    media = LiveKitMediaSession(settings(), uuid4())
    ended: list[bool] = []
    media.on_end(lambda: ended.append(True))
    await media.ensure_connected()
    await media.close()
    await media.close()
    assert room.disconnects == 1 and media.ended and ended == [True]
    with pytest.raises(MediaSessionUnavailable):
        await media.ensure_connected()


async def test_source_close_disconnects_room(room: FakeRoom) -> None:
    source = LiveKitAudioSource(settings(), uuid4())
    await source.connect()
    await source.close()
    assert room.disconnects == 1 and source.media.closed and source.ended
    assert source.track is None and source.selected_sid is None and source.stream is None


async def test_room_level_events_end_input(room: FakeRoom) -> None:
    session_id = uuid4()
    source = LiveKitAudioSource(settings(), session_id)
    await source.connect()
    room.emit("participant_disconnected", FakeParticipant("someone-else"))
    assert not source.ended and not source.media.ended
    room.emit("participant_disconnected", FakeParticipant(participant_name(session_id)))
    assert source.ended and source.media.ended and source.changed.is_set()

    other = LiveKitAudioSource(settings(), uuid4())
    await other.connect()
    room.emit("disconnected")
    assert other.ended


async def test_track_selection_and_replacement_unchanged(room: FakeRoom) -> None:
    session_id = uuid4()
    caller = FakeParticipant(participant_name(session_id))
    foreign = FakeParticipant("unexpected")
    existing = FakePublication("TR_existing")
    caller.track_publications[existing.sid] = existing
    room.remote_participants = {"caller": caller}
    source = LiveKitAudioSource(settings(), session_id)
    await source.connect()
    # Already-published caller microphone is selected after connecting.
    assert source.selected_sid == "TR_existing" and existing.subscribed == [True]

    foreign_mic = FakePublication("TR_foreign")
    room.emit("track_published", foreign_mic, foreign)
    camera = FakePublication("TR_camera", source=rtc.TrackSource.SOURCE_CAMERA)
    room.emit("track_published", camera, caller)
    assert foreign_mic.subscribed == [] and camera.subscribed == []
    assert source.selected_sid == "TR_existing"

    replacement = FakePublication("TR_replacement")
    caller.track_publications[replacement.sid] = replacement
    room.emit("track_published", replacement, caller)
    assert source.selected_sid == "TR_replacement"
    assert existing.subscribed == [True, False] and replacement.subscribed == [True]

    track = object()
    room.emit("track_subscribed", object(), existing, caller)
    assert source.track is None
    room.emit("track_subscribed", track, replacement, caller)
    assert source.track is track and source.changed.is_set()
    room.emit("track_unsubscribed", track, replacement, caller)
    assert source.track is None


async def test_source_connect_registers_input_handlers_once(room: FakeRoom) -> None:
    source = LiveKitAudioSource(settings(), uuid4())
    await source.connect()
    await source.connect()
    assert room.connects == 1
    assert {event: len(found) for event, found in room.handlers.items()} == {
        "participant_disconnected": 1,
        "disconnected": 1,
        "track_published": 1,
        "track_subscribed": 1,
        "track_unsubscribed": 1,
    }


def test_backend_grants_unchanged() -> None:
    session_id = uuid4()
    claims = jwt.decode(token(settings(), session_id, backend=True), "secret", algorithms=["HS256"])
    assert claims["sub"] == server_name(session_id) == f"relay-server-{session_id}"
    video = claims["video"]
    assert video["canSubscribe"] and not video["canPublish"] and not video["canPublishData"]
    assert not video.get("canPublishSources")
    assert not any(video.get(key) for key in ("roomAdmin", "roomCreate", "roomList", "roomRecord"))

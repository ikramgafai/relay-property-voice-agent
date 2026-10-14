import asyncio
from time import perf_counter
from types import SimpleNamespace
from uuid import uuid4

import jwt
import pytest
from app.realtime.media.livekit import (
    AGENT_TRACK,
    OUTPUT_FORMAT,
    OUTPUT_QUEUE_MS,
    LiveKitAudioSink,
    LiveKitAudioSource,
    LiveKitMediaSession,
    participant_name,
    rtc_frame,
    server_name,
    token,
)
from app.realtime.media.protocol import AudioFormat, AudioFrame, MediaSinkClosed
from app.realtime.output import OutputFailure, OutputStatus, SpeechOutput
from livekit import rtc

from tests.support.speech import FakeTTS
from tests.test_media_session import FakeParticipant, FakePublication, FakeRoom, settings


class FakeLocalParticipant:
    def __init__(self) -> None:
        self.published: list[tuple[object, rtc.TrackPublishOptions]] = []
        self.unpublished: list[str] = []
        self.permissions: list[tuple[bool, list[rtc.ParticipantTrackPermission]]] = []
        self.fail_publish = False

    def set_track_subscription_permissions(
        self,
        *,
        allow_all_participants: bool,
        participant_permissions: list[rtc.ParticipantTrackPermission] | None = None,
    ) -> None:
        self.permissions.append((allow_all_participants, list(participant_permissions or [])))

    async def publish_track(self, track: object, options: rtc.TrackPublishOptions) -> object:
        if self.fail_publish:
            raise RuntimeError("publish refused")
        self.published.append((track, options))
        return SimpleNamespace(sid=f"TR_agent_{len(self.published)}")

    async def unpublish_track(self, track_sid: str) -> None:
        self.unpublished.append(track_sid)


class OutputRoom(FakeRoom):
    def __init__(self) -> None:
        super().__init__()
        self.local_participant = FakeLocalParticipant()


@pytest.fixture
def room(monkeypatch: pytest.MonkeyPatch) -> OutputRoom:
    fake = OutputRoom()
    monkeypatch.setattr(rtc, "Room", lambda: fake)
    return fake


def pcm(samples: int, start: int = 0) -> bytes:
    return b"".join(((start + i) % 65536).to_bytes(2, "little") for i in range(samples))


def frame(index: int = 0) -> AudioFrame:
    return AudioFrame(data=pcm(480, index * 480), format=OUTPUT_FORMAT, sequence=index + 1)


async def fill(sink: LiveKitAudioSink, output_id: object) -> None:
    """Fill the native 200 ms queue so the next capture blocks."""
    for index in range(OUTPUT_QUEUE_MS // 20 + 3):
        await sink.capture(frame(index), output_id)  # type: ignore[arg-type]


async def blocked_capture(sink: LiveKitAudioSink, output_id: object) -> asyncio.Task[None]:
    task = asyncio.create_task(sink.capture(frame(99), output_id))  # type: ignore[arg-type]
    for _ in range(50):
        await asyncio.sleep(0.002)
        if not task.done():
            break
    assert not task.done(), "capture should be blocked by the full native queue"
    return task


# --- A-F: connect, resources, format and conversion -------------------------------------


async def test_connect_idempotent_one_room_one_source_one_publication(room: OutputRoom) -> None:
    media = LiveKitMediaSession(settings(), uuid4())
    sink = LiveKitAudioSink(media)
    await sink.connect()
    source, track = sink.source, sink.track
    await sink.connect()
    await asyncio.gather(sink.connect(), sink.connect())
    assert room.connects == 1 and sink.ready
    assert sink.source is source and sink.track is track and source is not None
    assert len(room.local_participant.published) == 1
    published_track, options = room.local_participant.published[0]
    assert published_track is track and options.source == rtc.TrackSource.SOURCE_MICROPHONE
    assert track is not None and track.name == AGENT_TRACK == "relay-agent-audio"
    assert (source.sample_rate, source.num_channels) == (24000, 1)
    assert sink.format == OUTPUT_FORMAT == AudioFormat(sample_rate=24000)
    await sink.close()


async def test_sink_reuses_room_already_connected_by_input(room: OutputRoom) -> None:
    session_id = uuid4()
    media = LiveKitMediaSession(settings(), session_id)
    source = LiveKitAudioSource(settings(), session_id, media)
    await source.connect()
    sink = LiveKitAudioSink(media)
    await sink.connect()
    assert room.connects == 1 and sink.media is source.media
    await sink.close()
    await source.close()


def test_rtc_frame_conversion_preserves_pcm_exactly() -> None:
    data = pcm(480)
    converted = rtc_frame(AudioFrame(data=data, format=OUTPUT_FORMAT, sequence=1))
    assert bytes(converted.data) == data
    assert (converted.sample_rate, converted.num_channels) == (24000, 1)
    assert converted.samples_per_channel == 480
    short = rtc_frame(AudioFrame(data=pcm(100), format=OUTPUT_FORMAT, sequence=2))
    assert short.samples_per_channel == 100 and bytes(short.data) == pcm(100)


# --- G-I: capture contract ----------------------------------------------------------------


async def test_capture_contract(room: OutputRoom) -> None:
    sink = LiveKitAudioSink(LiveKitMediaSession(settings(), uuid4()))
    with pytest.raises(RuntimeError):
        await sink.capture(frame(), uuid4())  # I. before connect: rejected
    await sink.connect()
    first, second = uuid4(), uuid4()
    await sink.capture(frame(0), first)
    await sink.capture(frame(1), first)
    await sink.capture(frame(2), second)  # G. a newer output identity
    with pytest.raises(ValueError):
        await sink.capture(frame(3), first)  # an earlier output is stale
    mismatched = AudioFrame(data=pcm(320), format=AudioFormat(sample_rate=16000), sequence=1)
    with pytest.raises(ValueError):
        await sink.capture(mismatched, second)  # H. format mismatch
    assert not sink.unusable  # contract errors do not poison the source
    await sink.capture(frame(4), second)
    await sink.close()


async def test_media_end_closes_the_output_path(room: OutputRoom) -> None:
    sink = LiveKitAudioSink(LiveKitMediaSession(settings(), uuid4()))
    await sink.connect()
    room.emit("disconnected")
    with pytest.raises(MediaSinkClosed):
        await sink.capture(frame(), uuid4())
    await sink.close()
    assert room.local_participant.unpublished == []  # Room already gone: nothing to unpublish


# --- J-M: clear, close and independence from input --------------------------------------


async def test_clear_releases_queue_and_track_stays_usable(room: OutputRoom) -> None:
    sink = LiveKitAudioSink(LiveKitMediaSession(settings(), uuid4()))
    await sink.connect()
    output_id = uuid4()
    await fill(sink, output_id)
    blocked = await blocked_capture(sink, output_id)
    tick = perf_counter()
    await sink.clear()
    await asyncio.wait_for(blocked, 1)  # clear frees space; no cancellation needed
    assert perf_counter() - tick < 0.5 and not sink.unusable
    tick = perf_counter()
    await sink.capture(frame(), output_id)
    assert perf_counter() - tick < 0.1
    await sink.close()


async def test_close_is_idempotent_and_keeps_shared_session(room: OutputRoom) -> None:
    media = LiveKitMediaSession(settings(), uuid4())
    sink = LiveKitAudioSink(media)
    await sink.connect()
    await sink.close()
    await sink.close()
    assert room.local_participant.unpublished == ["TR_agent_1"]
    assert sink.source is None and sink.track is None and sink.publication is None
    assert media.connected and not media.closed and room.disconnects == 0
    with pytest.raises(MediaSinkClosed):
        await sink.capture(frame(), uuid4())
    with pytest.raises(MediaSinkClosed):
        await sink.connect()


async def test_output_close_and_failure_do_not_break_input(room: OutputRoom) -> None:
    session_id = uuid4()
    media = LiveKitMediaSession(settings(), session_id)
    source = LiveKitAudioSource(settings(), session_id, media)
    await source.connect()
    sink = LiveKitAudioSink(media)
    await sink.connect()
    output_id = uuid4()
    await fill(sink, output_id)
    blocked = await blocked_capture(sink, output_id)
    blocked.cancel()  # output-side interruption/failure
    await asyncio.gather(blocked, return_exceptions=True)
    await sink.close()
    assert media.connected and not media.ended and not source.ended and room.disconnects == 0
    caller = FakeParticipant(participant_name(session_id))
    microphone = FakePublication("TR_mic")
    caller.track_publications[microphone.sid] = microphone
    track = object()
    room.emit("track_published", microphone, caller)
    room.emit("track_subscribed", track, microphone, caller)
    assert source.selected_sid == "TR_mic" and source.track is track  # input still works
    await source.close()
    assert room.disconnects == 1


# --- N-Q: grants and subscription restriction --------------------------------------------


def test_grants_after_phase_9() -> None:
    session_id = uuid4()
    caller = jwt.decode(token(settings(), session_id), "secret", algorithms=["HS256"])
    server = jwt.decode(token(settings(), session_id, backend=True), "secret", algorithms=["HS256"])
    assert caller["sub"] == participant_name(session_id)
    assert server["sub"] == server_name(session_id)
    for claims in (caller, server):
        video = claims["video"]
        assert video["room"] == f"relay-{session_id}" and video["roomJoin"]
        assert video["canPublish"] and video["canSubscribe"]
        assert video["canPublishSources"] == ["microphone"]
        assert not video["canPublishData"]
        admin = ("roomAdmin", "roomCreate", "roomList", "roomRecord", "ingressAdmin", "agent")
        assert not any(video.get(key) for key in admin)
        assert not video.get("hidden") and not video.get("canUpdateOwnMetadata")
        assert claims["exp"] - claims["nbf"] == 300


async def test_agent_track_subscription_limited_to_caller(room: OutputRoom) -> None:
    session_id = uuid4()
    sink = LiveKitAudioSink(LiveKitMediaSession(settings(), session_id))
    await sink.connect()
    [(allow_all, permissions)] = room.local_participant.permissions
    assert allow_all is False and len(permissions) == 1
    assert permissions[0].participant_identity == participant_name(session_id)
    assert permissions[0].allow_all
    await sink.close()


# --- R-S: duplicate publication and partial failure --------------------------------------


async def test_publish_failure_cleans_output_and_keeps_room(room: OutputRoom) -> None:
    media = LiveKitMediaSession(settings(), uuid4())
    sink = LiveKitAudioSink(media)
    room.local_participant.fail_publish = True
    with pytest.raises(RuntimeError):
        await sink.connect()
    assert sink.failed and not sink.ready
    assert sink.source is None and sink.track is None and sink.publication is None
    assert media.connected and room.disconnects == 0
    with pytest.raises(MediaSinkClosed):
        await sink.connect()  # terminal: never republishes a second track
    assert room.local_participant.published == []
    await sink.close()


# --- T-U: blocked capture_frame cancellation and close -----------------------------------


async def test_cancelling_blocked_capture_is_prompt_and_marks_sink_unusable(
    room: OutputRoom,
) -> None:
    sink = LiveKitAudioSink(LiveKitMediaSession(settings(), uuid4()))
    await sink.connect()
    output_id = uuid4()
    await fill(sink, output_id)
    blocked = await blocked_capture(sink, output_id)
    tick = perf_counter()
    blocked.cancel()
    [result] = await asyncio.gather(blocked, return_exceptions=True)
    assert isinstance(result, asyncio.CancelledError) and perf_counter() - tick < 0.05
    # The native source still holds the in-flight frame, so further captures are refused.
    assert sink.unusable
    with pytest.raises(RuntimeError):
        await sink.capture(frame(), output_id)
    tick = perf_counter()
    await sink.close()
    assert perf_counter() - tick < 1 and sink.source is None
    assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]


async def test_close_while_capture_blocked_completes(room: OutputRoom) -> None:
    sink = LiveKitAudioSink(LiveKitMediaSession(settings(), uuid4()))
    await sink.connect()
    output_id = uuid4()
    await fill(sink, output_id)
    blocked = await blocked_capture(sink, output_id)
    tick = perf_counter()
    await sink.close()
    await asyncio.wait_for(asyncio.gather(blocked, return_exceptions=True), 1)
    assert perf_counter() - tick < 1 and blocked.done()
    assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]


# --- X-Y: SpeechOutput over the real LiveKit sink -----------------------------------------


async def test_speech_output_completes_over_livekit_sink(room: OutputRoom) -> None:
    media = LiveKitMediaSession(settings(), uuid4())
    sink = LiveKitAudioSink(media)
    chunks = [pcm(480 * 3, 0), pcm(480 * 2 + 100, 1440)]
    output = SpeechOutput(FakeTTS(chunks), sink, provider_name="fake", model="deterministic")
    output.start()
    record = output.present(uuid4(), "Hello.")
    assert record is not None
    async with asyncio.timeout(3):
        await record.ended.wait()
    assert record.status == OutputStatus.COMPLETED and record.frames_accepted == 6
    assert record.accepted_audio_ms == pytest.approx((480 * 5 + 100) / 24)
    assert record.first_frame_accepted_by_sink is not None
    await output.close()
    assert room.local_participant.unpublished == ["TR_agent_1"] and media.connected


async def test_livekit_sink_failure_is_presentation_only(room: OutputRoom) -> None:
    session_id = uuid4()
    media = LiveKitMediaSession(settings(), session_id)
    source = LiveKitAudioSource(settings(), session_id, media)
    await source.connect()
    room.local_participant.fail_publish = True
    output = SpeechOutput(FakeTTS([pcm(480)]), LiveKitAudioSink(media))
    output.start()
    record = output.present(uuid4(), "Hello.")
    assert record is not None
    async with asyncio.timeout(3):
        await record.ended.wait()
    assert record.status == OutputStatus.FAILED and record.code == OutputFailure.OUTPUT_UNAVAILABLE
    assert media.connected and not source.ended  # microphone input unaffected
    await output.close()
    await source.close()

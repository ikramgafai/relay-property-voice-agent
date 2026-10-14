import asyncio
import math
import os
from array import array
from contextlib import aclosing
from uuid import uuid4

import jwt
import pytest
from app.core.config import Settings
from app.providers.stt.schemas import SpeechContext
from app.realtime.media.livekit import (
    AGENT_TRACK,
    LiveKitAudioSink,
    LiveKitAudioSource,
    LiveKitMediaSession,
    participant_name,
    room_name,
    server_name,
    token,
)
from app.realtime.output import OutputStatus, SpeechOutput
from livekit import api, rtc
from pydantic import SecretStr

from tests.support.speech import FakeSTT, FakeTTS


def media_settings(url: str = "ws://livekit:7880") -> Settings:
    return Settings(
        livekit_url=url, livekit_api_key=SecretStr("devkey"), livekit_api_secret=SecretStr("secret")
    )


def test_token_scoped_short_lived_and_no_server_authority() -> None:
    session_id = uuid4()
    claims = jwt.decode(token(media_settings(), session_id), "secret", algorithms=["HS256"])
    assert claims["sub"] == participant_name(session_id)
    assert claims["video"]["room"] == room_name(session_id)
    assert claims["video"]["canPublishSources"] == ["microphone"]
    # Phase 9: the caller may subscribe (agent audio) but still never publish data.
    assert claims["video"]["canSubscribe"] and not claims["video"]["canPublishData"]
    assert claims["exp"] - claims["nbf"] == 300
    server = jwt.decode(
        token(media_settings(), session_id, backend=True), "secret", algorithms=["HS256"]
    )
    # Phase 9: the backend publishes only the microphone-source agent track.
    assert server["video"]["canPublish"] and server["video"]["canSubscribe"]
    assert server["video"]["canPublishSources"] == ["microphone"]
    assert not server["video"]["canPublishData"]


async def test_actual_livekit_audio_transport_and_replacement() -> None:
    url = os.environ.get("TEST_LIVEKIT_URL")
    if not url:
        pytest.skip("Run compose.test.yml to exercise the isolated LiveKit server")
    settings, session_id = media_settings(url), uuid4()
    source = LiveKitAudioSource(settings, session_id)
    publisher, foreign = rtc.Room(), rtc.Room()
    audio = rtc.AudioSource(48000, 1)
    stt = FakeSTT()
    await stt.open(SpeechContext())
    consumer: asyncio.Task[None] | None = None

    async def consume() -> None:
        async with aclosing(source.__aiter__()) as frames:
            async for frame in frames:
                await stt.send(frame)

    try:
        await source.connect()
        # Foreign identity may join the same room but never becomes the caller source.
        foreign_token = (
            api.AccessToken("devkey", "secret")
            .with_identity("unexpected")
            .with_grants(api.VideoGrants(room_join=True, room=room_name(session_id)))
            .to_jwt()
        )
        await foreign.connect(url, foreign_token)
        foreign_track = rtc.LocalAudioTrack.create_audio_track("foreign", audio)
        await foreign.local_participant.publish_track(
            foreign_track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        )
        await publisher.connect(url, token(settings, session_id))
        consumer = asyncio.create_task(consume(), name="relay-test-media-consumer")
        for index in range(2):
            track = rtc.LocalAudioTrack.create_audio_track(f"microphone-{index}", audio)
            publication = await publisher.local_participant.publish_track(
                track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
            )
            async with asyncio.timeout(10):
                await publication.wait_for_subscription()
                # Actual WebRTC/Opus transport, SDK resampling from 48 kHz to 16 kHz.
                for _ in range(10):
                    await audio.capture_frame(
                        rtc.AudioFrame(
                            data=b"\x01\x00" * 2400,
                            sample_rate=48000,
                            num_channels=1,
                            samples_per_channel=2400,
                        )
                    )
                frame = await stt.audio.get()
                assert frame.format.sample_rate == 16000 and frame.format.channels == 1
                assert len(frame.data) == 1600
            await publisher.local_participant.unpublish_track(publication.sid)
        await publisher.disconnect()
        async with asyncio.timeout(5):
            await consumer
        assert source.ended
    finally:
        if consumer:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
        await source.close()
        await stt.close()
        await publisher.disconnect()
        await foreign.disconnect()
        await audio.aclose()
    assert source.stream is None and source.track is None and stt.closed


def tone(samples: int, rate: int, hz: int = 440, amplitude: int = 8000) -> bytes:
    return array(
        "h", (int(amplitude * math.sin(2 * math.pi * hz * i / rate)) for i in range(samples))
    ).tobytes()


def rms(data: bytes) -> float:
    values = array("h", data)
    return math.sqrt(sum(v * v for v in values) / len(values)) if values else 0.0


async def test_actual_livekit_bidirectional_shared_participant() -> None:
    url = os.environ.get("TEST_LIVEKIT_URL")
    if not url:
        pytest.skip("Run compose.test.yml to exercise the isolated LiveKit server")
    settings, session_id = media_settings(url), uuid4()
    media = LiveKitMediaSession(settings, session_id)
    source = LiveKitAudioSource(settings, session_id, media)
    sink = LiveKitAudioSink(media)
    caller, foreign = rtc.Room(), rtc.Room()
    microphone = rtc.AudioSource(48000, 1)
    stt = FakeSTT()
    await stt.open(SpeechContext())
    agent: asyncio.Queue[tuple[rtc.Track, rtc.RemoteTrackPublication, rtc.RemoteParticipant]]
    agent = asyncio.Queue()
    foreign_subscriptions: list[str] = []
    caller.on("track_subscribed", lambda *args: agent.put_nowait(args))
    foreign.on("track_subscribed", lambda t, p, who: foreign_subscriptions.append(who.identity))
    received: list[rtc.AudioFrame] = []
    tasks: list[asyncio.Task[None]] = []
    output: SpeechOutput | None = None

    async def consume_input() -> None:
        async with aclosing(source.__aiter__()) as frames:
            async for frame in frames:
                await stt.audio.put(frame)

    async def receive(stream: rtc.AudioStream) -> None:
        async for event in stream:
            received.append(event.frame)

    async def speak_and_hear_input() -> None:
        for _ in range(25):  # 0.5 s caller tone at 48 kHz
            await microphone.capture_frame(rtc.AudioFrame(tone(960, 48000), 48000, 1, 960))
        async with asyncio.timeout(10):
            while True:
                frame = await stt.audio.get()
                assert frame.format.sample_rate == 16000 and frame.format.channels == 1
                if rms(frame.data) > 500:
                    return

    try:
        # One backend participant: input and output share the same Room connection.
        await source.connect()
        await sink.connect()
        assert media.connected and source.room is media.room
        await caller.connect(url, token(settings, session_id))
        foreign_token = (
            api.AccessToken("devkey", "secret")
            .with_identity("unexpected")
            .with_grants(api.VideoGrants(room_join=True, room=room_name(session_id)))
            .to_jwt()
        )
        await foreign.connect(url, foreign_token)
        tasks.append(asyncio.create_task(consume_input(), name="relay-test-input"))

        # INPUT: caller microphone -> Relay LiveKitAudioSource (16 kHz PCM16 mono).
        track = rtc.LocalAudioTrack.create_audio_track("microphone", microphone)
        publication = await caller.local_participant.publish_track(
            track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        )
        async with asyncio.timeout(10):
            await publication.wait_for_subscription()
        await speak_and_hear_input()

        # OUTPUT: FakeTTS -> SpeechOutput -> LiveKitAudioSink -> caller.
        async with asyncio.timeout(10):
            agent_track, agent_publication, publisher = await agent.get()
        assert publisher.identity == server_name(session_id)
        assert agent_publication.name == AGENT_TRACK
        assert agent_publication.kind == rtc.TrackKind.KIND_AUDIO
        assert agent_publication.source == rtc.TrackSource.SOURCE_MICROPHONE
        stream = rtc.AudioStream.from_track(
            track=agent_track, sample_rate=24000, num_channels=1, frame_size_ms=20
        )
        tasks.append(asyncio.create_task(receive(stream), name="relay-test-receive"))
        speech = tone(24000, 24000)  # 1 s at 24 kHz, delivered in unaligned chunks
        chunks = [speech[i : i + 5001] for i in range(0, len(speech), 5001)]
        output = SpeechOutput(FakeTTS(chunks), sink, provider_name="fake", model="tone")
        output.start()
        record = output.present(uuid4(), "Deterministic tone.")
        assert record is not None
        async with asyncio.timeout(10):
            await record.ended.wait()
        assert record.status == OutputStatus.COMPLETED and record.frames_accepted == 50
        await asyncio.sleep(1.5)  # let transported audio arrive at the caller
        assert agent_track is not None and sink.track is not None
        sent: int | None = None
        try:  # optional diagnostic evidence only; never blocks the transport assertions
            async with asyncio.timeout(5):
                stats = await sink.track.get_stats()
            sent = sum(s.outbound_rtp.sent.bytes_sent for s in stats if s.HasField("outbound_rtp"))
        except TimeoutError:
            pass
        assert sent is None or sent > 0
        voiced = [f for f in received if rms(bytes(f.data)) > 1000]
        assert received and all(f.sample_rate == 24000 and f.num_channels == 1 for f in received)
        voiced_samples = sum(f.samples_per_channel for f in voiced)
        # WebRTC/Opus is lossy: evidence is signal energy and duration, not byte equality.
        assert 0.6 * 24000 <= voiced_samples <= 1.4 * 24000, voiced_samples
        print(
            "LIVEKIT_OUTPUT_EVIDENCE",
            f"frames_accepted={record.frames_accepted}",
            f"accepted_audio_ms={record.accepted_audio_ms:.0f}",
            f"received_frames={len(received)}",
            f"voiced_frames={len(voiced)}",
            f"voiced_samples={voiced_samples}",
            f"voiced_rms={sum(rms(bytes(f.data)) for f in voiced) / len(voiced):.0f}",
            f"outbound_bytes_sent={sent}",
        )
        assert server_name(session_id) not in foreign_subscriptions

        # Closing output leaves the shared Room and microphone input working.
        await output.close()
        output = None
        assert sink.closed and media.connected and not media.ended and not source.ended
        while not stt.audio.empty():
            stt.audio.get_nowait()
        await speak_and_hear_input()
    finally:
        if output is not None:
            await output.close()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await sink.close()
        await source.close()
        await stt.close()
        await caller.disconnect()
        await foreign.disconnect()
        await microphone.aclose()
    assert media.closed and not [
        t for t in asyncio.all_tasks() if t.get_name().startswith("relay-")
    ]

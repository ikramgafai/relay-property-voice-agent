import asyncio
import os
from contextlib import aclosing
from uuid import uuid4

import jwt
import pytest
from app.core.config import Settings
from app.providers.stt.schemas import SpeechContext
from app.realtime.media.livekit import LiveKitAudioSource, participant_name, room_name, token
from livekit import api, rtc
from pydantic import SecretStr

from tests.support.speech import FakeSTT


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
    assert not claims["video"]["canSubscribe"] and not claims["video"]["canPublishData"]
    assert claims["exp"] - claims["nbf"] == 300
    server = jwt.decode(
        token(media_settings(), session_id, backend=True), "secret", algorithms=["HS256"]
    )
    assert not server["video"]["canPublish"] and server["video"]["canSubscribe"]


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

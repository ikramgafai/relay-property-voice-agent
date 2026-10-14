"""LiveKit is audio transport only. Room and participant are backend-generated."""

import asyncio
from collections.abc import AsyncGenerator
from datetime import timedelta
from uuid import UUID

from livekit import api, rtc

from app.core.config import Settings
from app.realtime.media.protocol import AudioFormat, AudioFrame


def room_name(session_id: UUID) -> str:
    return f"relay-{session_id}"


def participant_name(session_id: UUID) -> str:
    return f"caller-{session_id}"


def token(settings: Settings, session_id: UUID, *, backend: bool = False) -> str:
    assert settings.livekit_api_key and settings.livekit_api_secret
    grants = api.VideoGrants(
        room_join=True,
        room=room_name(session_id),
        can_publish=not backend,
        can_subscribe=backend,
        can_publish_data=False,
        can_publish_sources=[] if backend else ["microphone"],
    )
    identity = f"relay-server-{session_id}" if backend else participant_name(session_id)
    return (
        api.AccessToken(
            settings.livekit_api_key.get_secret_value(),
            settings.livekit_api_secret.get_secret_value(),
        )
        .with_identity(identity)
        .with_grants(grants)
        .with_ttl(timedelta(minutes=5))
        .to_jwt()
    )


class LiveKitAudioSource:
    def __init__(self, settings: Settings, session_id: UUID) -> None:
        self.settings, self.session_id = settings, session_id
        self.format = AudioFormat(sample_rate=settings.stt_sample_rate)
        self.room = rtc.Room()
        self.changed = asyncio.Event()
        self.ended = False
        self.track: rtc.Track | None = None
        self.stream: rtc.AudioStream | None = None
        self.sequence = 0
        self.connected = False
        self.selected_sid: str | None = None

    def authorized(
        self, participant: rtc.RemoteParticipant, publication: rtc.RemoteTrackPublication
    ) -> bool:
        return (
            participant.identity == participant_name(self.session_id)
            and publication.kind == rtc.TrackKind.KIND_AUDIO
            and publication.source == rtc.TrackSource.SOURCE_MICROPHONE
        )

    async def connect(self) -> None:
        def published(
            publication: rtc.RemoteTrackPublication, participant: rtc.RemoteParticipant
        ) -> None:
            if self.authorized(participant, publication):
                self.selected_sid = publication.sid
                for previous in participant.track_publications.values():
                    if previous.sid != publication.sid:
                        previous.set_subscribed(False)
                publication.set_subscribed(True)

        def subscribed(
            track: rtc.Track,
            publication: rtc.RemoteTrackPublication,
            participant: rtc.RemoteParticipant,
        ) -> None:
            if self.authorized(participant, publication) and publication.sid == self.selected_sid:
                self.track = track
                self.changed.set()

        def unsubscribed(
            track: rtc.Track,
            publication: rtc.RemoteTrackPublication,
            participant: rtc.RemoteParticipant,
        ) -> None:
            if self.track is track:
                self.track = None
                self.changed.set()

        def disconnected(*args: object) -> None:
            self.ended = True
            self.changed.set()

        def participant_left(participant: rtc.RemoteParticipant) -> None:
            if participant.identity == participant_name(self.session_id):
                disconnected()

        self.room.on("track_published", published)
        self.room.on("track_subscribed", subscribed)
        self.room.on("track_unsubscribed", unsubscribed)
        self.room.on("participant_disconnected", participant_left)
        self.room.on("disconnected", disconnected)
        assert self.settings.livekit_url
        async with asyncio.timeout(10):
            await self.room.connect(
                self.settings.livekit_url,
                token(self.settings, self.session_id, backend=True),
                options=rtc.RoomOptions(auto_subscribe=False),
            )
        self.connected = True
        for participant in self.room.remote_participants.values():
            for publication in participant.track_publications.values():
                published(publication, participant)

    async def __aiter__(self) -> AsyncGenerator[AudioFrame, None]:
        if not self.connected:
            await self.connect()
        while not self.ended:
            self.changed.clear()
            current = self.track
            if current is None:
                async with asyncio.timeout(30):
                    await self.changed.wait()
                continue
            self.stream = rtc.AudioStream.from_track(
                track=current,
                sample_rate=self.format.sample_rate,
                num_channels=self.format.channels,
                frame_size_ms=50,
                capacity=20,
            )
            change = asyncio.create_task(self.changed.wait(), name="relay-media-track-change")
            try:
                while not self.ended and self.track is current:
                    frame_task = asyncio.create_task(
                        self.stream.__anext__(), name="relay-media-frame"
                    )
                    try:
                        done, _ = await asyncio.wait(
                            {frame_task, change}, return_when=asyncio.FIRST_COMPLETED
                        )
                        if change in done:
                            break
                        event = frame_task.result()
                        frame = event.frame
                        self.sequence += 1
                        yield AudioFrame(
                            data=bytes(frame.data),
                            sequence=self.sequence,
                            format=AudioFormat(
                                sample_rate=frame.sample_rate, channels=frame.num_channels
                            ),
                        )
                    except StopAsyncIteration:
                        break
                    finally:
                        frame_task.cancel()
                        await asyncio.gather(frame_task, return_exceptions=True)
            finally:
                change.cancel()
                await asyncio.gather(change, return_exceptions=True)
                await self.stream.aclose()
                self.stream = None
            if self.track is current and not self.ended:
                self.track = None

    async def close(self) -> None:
        self.ended = True
        self.changed.set()
        if self.stream:
            await self.stream.aclose()
            self.stream = None
        await self.room.disconnect()
        self.track = None
        self.selected_sid = None

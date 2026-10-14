"""LiveKit is audio transport only. Room and participant are backend-generated."""

import asyncio
from collections import deque
from collections.abc import AsyncGenerator, Callable
from datetime import timedelta
from uuid import UUID

from livekit import api, rtc

from app.core.config import Settings
from app.realtime.media.protocol import AudioFormat, AudioFrame, MediaSinkClosed


def room_name(session_id: UUID) -> str:
    return f"relay-{session_id}"


def participant_name(session_id: UUID) -> str:
    return f"caller-{session_id}"


def server_name(session_id: UUID) -> str:
    return f"relay-server-{session_id}"


AGENT_TRACK = "relay-agent-audio"
OUTPUT_FORMAT = AudioFormat(sample_rate=24000)
OUTPUT_QUEUE_MS = 200  # native LiveKit buffer ahead of WebRTC; Python buffers 500 ms more


def token(settings: Settings, session_id: UUID, *, backend: bool = False) -> str:
    """Caller: publishes its microphone, subscribes to agent audio. Backend: subscribes to the
    caller microphone, publishes the agent audio track. LiveKit's audio publish source is
    "microphone" for both; neither may publish data, video or screen share."""
    assert settings.livekit_api_key and settings.livekit_api_secret
    grants = api.VideoGrants(
        room_join=True,
        room=room_name(session_id),
        can_publish=True,
        can_subscribe=True,
        can_publish_data=False,
        can_publish_sources=["microphone"],
    )
    identity = server_name(session_id) if backend else participant_name(session_id)
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


class MediaSessionUnavailable(Exception):
    """The backend participant is closed or its connection failed; sessions never reconnect."""


class LiveKitMediaSession:
    """Owns the backend participant's Room for one voice attachment.

    Room-level lifecycle lives here: connection, caller departure and disconnect. Track
    selection and streams stay with the media users (today only LiveKitAudioSource).
    """

    def __init__(self, settings: Settings, session_id: UUID) -> None:
        self.settings, self.session_id = settings, session_id
        self.identity = server_name(session_id)
        self.room = rtc.Room()
        self.connected = False
        self.failed = False
        self.closed = False
        self.ended = False
        self._lock = asyncio.Lock()
        self._listeners: list[Callable[[], None]] = []

        def participant_left(participant: rtc.RemoteParticipant) -> None:
            if participant.identity == participant_name(self.session_id):
                self._end()

        def disconnected(*args: object) -> None:
            self._end()

        self.room.on("participant_disconnected", participant_left)
        self.room.on("disconnected", disconnected)

    def on_end(self, listener: Callable[[], None]) -> None:
        self._listeners.append(listener)

    def _end(self) -> None:
        self.ended = True
        for listener in list(self._listeners):
            listener()

    async def ensure_connected(self) -> None:
        async with self._lock:
            if self.connected:
                return
            if self.closed or self.failed:
                raise MediaSessionUnavailable()
            assert self.settings.livekit_url
            try:
                async with asyncio.timeout(10):
                    await self.room.connect(
                        self.settings.livekit_url,
                        token(self.settings, self.session_id, backend=True),
                        options=rtc.RoomOptions(auto_subscribe=False),
                    )
            except BaseException:
                # Cancellation and failure are terminal; close() disconnects the Room.
                self.failed = True
                raise
            if self.closed:
                await self.room.disconnect()
                raise MediaSessionUnavailable()
            self.connected = True

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.connected = False
        self._end()
        await self.room.disconnect()


class LiveKitAudioSource:
    """Caller microphone input. Owns track selection and streams, not the Room lifetime."""

    def __init__(
        self, settings: Settings, session_id: UUID, media: LiveKitMediaSession | None = None
    ) -> None:
        self.settings, self.session_id = settings, session_id
        self.format = AudioFormat(sample_rate=settings.stt_sample_rate)
        # Phase 8 semantics: closing input closes the media session and disconnects the Room.
        self.media = LiveKitMediaSession(settings, session_id) if media is None else media
        self.room = self.media.room
        self.changed = asyncio.Event()
        self.ended = False
        self.track: rtc.Track | None = None
        self.stream: rtc.AudioStream | None = None
        self.sequence = 0
        self.connected = False
        self.selected_sid: str | None = None
        self._observing = False
        self.media.on_end(self._media_ended)

    def _media_ended(self) -> None:
        self.ended = True
        self.changed.set()

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

        # Input-specific handlers; caller departure and Room disconnect belong to the
        # media session, which notifies this source through on_end().
        if not self._observing:
            self.room.on("track_published", published)
            self.room.on("track_subscribed", subscribed)
            self.room.on("track_unsubscribed", unsubscribed)
            self._observing = True
        await self.media.ensure_connected()
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
        await self.media.close()
        self.track = None
        self.selected_sid = None


def rtc_frame(frame: AudioFrame) -> rtc.AudioFrame:
    """Exact PCM16 bytes, rate and channels; no resampling or re-encoding."""
    samples = len(frame.data) // 2 // frame.format.channels
    return rtc.AudioFrame(frame.data, frame.format.sample_rate, frame.format.channels, samples)


class LiveKitAudioSink:
    """Agent audio output on the shared backend participant.

    Owns only its AudioSource, LocalAudioTrack and publication; never the Room. Acceptance by
    capture() means the local LiveKit source took the frame, not that anyone received it.
    """

    def __init__(self, media: LiveKitMediaSession, format: AudioFormat = OUTPUT_FORMAT) -> None:
        self.media, self.format = media, format
        self.source: rtc.AudioSource | None = None
        self.track: rtc.LocalAudioTrack | None = None
        self.publication: rtc.LocalTrackPublication | None = None
        self.ready = False
        self.failed = False
        self.closed = False
        # A cancelled or failed capture leaves the native source busy with the in-flight
        # frame (InvalidState until it drains), so the sink refuses further captures.
        self.unusable = False
        self._lock = asyncio.Lock()
        self._current: UUID | None = None
        self._previous: deque[UUID] = deque(maxlen=64)
        self.media.on_end(self._media_ended)

    def _media_ended(self) -> None:
        self.ready = False

    async def connect(self) -> None:
        async with self._lock:
            if self.ready:
                return
            if self.closed or self.failed or self.media.ended:
                raise MediaSinkClosed()
            try:
                await self.media.ensure_connected()
                self.source = rtc.AudioSource(
                    self.format.sample_rate, self.format.channels, queue_size_ms=OUTPUT_QUEUE_MS
                )
                self.track = rtc.LocalAudioTrack.create_audio_track(AGENT_TRACK, self.source)
                participant = self.media.room.local_participant
                participant.set_track_subscription_permissions(
                    allow_all_participants=False,
                    participant_permissions=[
                        rtc.ParticipantTrackPermission(
                            participant_identity=participant_name(self.media.session_id),
                            allow_all=True,
                        )
                    ],
                )
                async with asyncio.timeout(10):
                    self.publication = await participant.publish_track(
                        self.track,
                        rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE),
                    )
            except BaseException:
                # Partial failure releases output resources only; the Room and input stay up.
                self.failed = True
                await self._release()
                raise
            self.ready = True

    async def capture(self, frame: AudioFrame, speech_output_id: UUID) -> None:
        if self.closed or self.media.ended:
            raise MediaSinkClosed()
        if not self.ready or self.source is None:
            raise RuntimeError("Output transport is not connected")
        if self.unusable:
            raise RuntimeError("Output transport is unusable after an interrupted capture")
        if frame.format != self.format:
            raise ValueError("Frame format does not match the output track")
        if speech_output_id != self._current:
            if speech_output_id in self._previous:
                raise ValueError("Frame belongs to an earlier speech output")
            if self._current is not None:
                self._previous.append(self._current)
            self._current = speech_output_id
        try:
            await self.source.capture_frame(rtc_frame(frame))
        except BaseException:
            self.unusable = True
            raise

    async def clear(self) -> None:
        if self.source is not None and not self.closed:
            self.source.clear_queue()

    async def close(self) -> None:
        async with self._lock:
            if self.closed:
                return
            self.closed = True
            self.ready = False
            await self._release()

    async def _release(self) -> None:
        source, publication = self.source, self.publication
        self.source = self.track = self.publication = None
        if source is not None:
            source.clear_queue()
        if publication is not None and self.media.connected and not self.media.ended:
            try:
                async with asyncio.timeout(5):
                    await self.media.room.local_participant.unpublish_track(publication.sid)
            except Exception:
                pass
        if source is not None:
            await source.aclose()

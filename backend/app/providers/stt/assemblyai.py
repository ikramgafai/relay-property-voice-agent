"""AssemblyAI v3 wire adapter. Bounded async sends; no framework or SDK write queue."""

import asyncio
import json
from collections.abc import Callable
from time import perf_counter
from typing import Literal
from urllib.parse import urlencode
from uuid import UUID

import structlog
from pydantic import BaseModel, ConfigDict, Field, JsonValue
from websockets.asyncio.client import ClientConnection, connect

from app.core.config import Settings
from app.providers.stt.schemas import SpeechContext, STTEvent, STTFailure
from app.realtime.media.protocol import AudioFormat, AudioFrame


class WireEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")
    type: str
    id: UUID | None = None
    turn_order: int = Field(default=0, ge=0)
    end_of_turn: bool = False
    transcript: str = Field(default="", max_length=8000)
    language_code: str | None = None
    timestamp: int | None = None
    configuration: dict[str, JsonValue] = Field(default_factory=dict)


class TurnWire(WireEvent):
    turn_order: int = Field(ge=0)
    end_of_turn: bool = Field(strict=True)
    transcript: str = Field(max_length=8000)


def _word_bounds(wire_json: str | bytes) -> tuple[int | None, int | None]:
    """Diagnostics only: provider audio offsets of the first and last recognized word.

    Never affects parsing; anything unexpected yields (None, None)."""
    try:
        words = json.loads(wire_json).get("words") or []
        starts = [w["start"] for w in words if isinstance(w.get("start"), int)]
        ends = [w["end"] for w in words if isinstance(w.get("end"), int)]
    except Exception:
        return None, None
    return (min(starts) if starts else None), (max(ends) if ends else None)


class AssemblyAIStreaming:
    def __init__(self, settings: Settings, *, log: Callable[..., None] | None = None) -> None:
        self.settings = settings
        self.format = AudioFormat(sample_rate=settings.stt_sample_rate)
        self.socket: ClientConnection | None = None
        self.stream_id: UUID | None = None
        self.pending: STTEvent | None = None
        self.speech_timestamp: int | None = None
        # Diagnostics: server monotonic arrival of the held raw SpeechStarted, and how much
        # audio the provider had been sent. Counts and timings only; never transcript text.
        self.speech_tick: float | None = None
        self.opened_tick: float | None = None
        self.audio_bytes_sent = 0
        self._log = log or structlog.get_logger().info
        self.send_lock = asyncio.Lock()
        self.buffer = bytearray()
        self.closed = False
        self.actual_model: str | None = None

    async def open(self, context: SpeechContext) -> None:
        if not self.settings.assemblyai_api_key:
            raise STTFailure()
        params: dict[str, str | int] = {
            "sample_rate": self.format.sample_rate,
            "encoding": self.format.encoding,
            "speech_model": self.settings.stt_model,
            "language_detection": "true",
            "min_turn_silence": self.settings.stt_min_turn_silence,
            "max_turn_silence": self.settings.stt_max_turn_silence,
            "keyterms_prompt": json.dumps(context.keyterms),
        }
        if self.settings.stt_language_codes:
            params["language_codes"] = json.dumps(self.settings.stt_language_codes)
        if context.agent_response:
            params["agent_context"] = context.agent_response[-1750:]
        try:
            self.socket = await connect(
                "wss://streaming.assemblyai.com/v3/ws?" + urlencode(params),
                additional_headers={
                    "Authorization": self.settings.assemblyai_api_key.get_secret_value()
                },
                open_timeout=10,
                close_timeout=1,
                max_queue=16,
                max_size=65536,
            )
            async with asyncio.timeout(10):
                begin = WireEvent.model_validate_json(await self.socket.recv())
            if begin.type != "Begin" or begin.id is None:
                raise STTFailure()
            self.stream_id = begin.id
            self.opened_tick = perf_counter()
            reported_model = begin.configuration.get("model")
            self.actual_model = reported_model if isinstance(reported_model, str) else None
        except Exception:
            await self.close()
            raise STTFailure() from None

    async def send(self, frame: AudioFrame) -> None:
        if frame.format != self.format or self.socket is None or self.closed:
            raise STTFailure()
        # Packetization only, no resampling: 50 ms PCM chunks, at most one partial chunk.
        size = self.format.sample_rate // 20 * 2
        async with self.send_lock:
            self.buffer.extend(frame.data)
            while len(self.buffer) >= size:
                async with asyncio.timeout(3):
                    await self.socket.send(bytes(self.buffer[:size]))
                del self.buffer[:size]
                self.audio_bytes_sent += size

    async def receive(self) -> STTEvent:
        if self.pending is not None:
            event, self.pending = self.pending, None
            return event
        if self.socket is None or self.stream_id is None:
            raise STTFailure()
        try:
            while True:
                wire_json = await self.socket.recv()
                wire = WireEvent.model_validate_json(wire_json)
                if wire.type == "SpeechStarted":
                    self._log(
                        "ASSEMBLYAI_RAW_SPEECH_STARTED",
                        **self._timing(),
                        audio_timestamp_ms=wire.timestamp,
                        replaced_waiting_speech_start=self.speech_timestamp is not None,
                    )
                    self.speech_timestamp = wire.timestamp
                    self.speech_tick = perf_counter()
                elif wire.type == "Turn":
                    wire = TurnWire.model_validate_json(wire_json)
                    first_word_ms, last_word_ms = _word_bounds(wire_json)
                    self._log(
                        "ASSEMBLYAI_TURN_RECEIVED",
                        **self._timing(),
                        turn_order=wire.turn_order,
                        end_of_turn=wire.end_of_turn,
                        transcript_char_count=len(wire.transcript),
                        transcript_word_count=len(wire.transcript.split()),
                        language=wire.language_code,
                        first_word_start_ms=first_word_ms,
                        last_word_end_ms=last_word_ms,
                        speech_start_waiting=self.speech_timestamp is not None,
                        held_speech_start_ms=self._held_ms(),
                    )
                    kind: Literal["FINAL", "PARTIAL"] = "FINAL" if wire.end_of_turn else "PARTIAL"
                    event = STTEvent(
                        kind=kind,
                        stream_id=self.stream_id,
                        turn_order=wire.turn_order,
                        transcript=wire.transcript,
                        language=wire.language_code,
                        model=self.settings.stt_model,
                    )
                    if self.speech_timestamp is not None:
                        self._log(
                            "ASSEMBLYAI_SPEECH_START_RELEASED",
                            **self._timing(),
                            turn_order=wire.turn_order,
                            audio_timestamp_ms=self.speech_timestamp,
                            held_speech_start_ms=self._held_ms(),
                        )
                        timestamp, self.speech_timestamp = self.speech_timestamp, None
                        self.speech_tick = None
                        self.pending = event
                        return STTEvent(
                            kind="SPEECH_STARTED",
                            stream_id=self.stream_id,
                            turn_order=wire.turn_order,
                            audio_timestamp_ms=timestamp,
                            model=self.settings.stt_model,
                        )
                    return event
                elif wire.type in {"Error", "Termination"}:
                    raise STTFailure()
        except Exception:
            raise STTFailure() from None

    def _timing(self) -> dict[str, object]:
        """Common diagnostic fields: stream identity, server time since the stream opened,
        and how much audio had been sent to the provider at this moment."""
        now = perf_counter()
        return {
            "stream_id": str(self.stream_id) if self.stream_id else None,
            "since_stream_open_ms": (
                None if self.opened_tick is None else (now - self.opened_tick) * 1000
            ),
            "audio_sent_ms": self.audio_bytes_sent / 2 / self.format.sample_rate * 1000,
        }

    def _held_ms(self) -> float | None:
        return None if self.speech_tick is None else (perf_counter() - self.speech_tick) * 1000

    async def update_context(self, response_text: str) -> None:
        if self.socket is None or self.closed:
            raise STTFailure()
        async with self.send_lock, asyncio.timeout(3):
            await self.socket.send(
                json.dumps({"type": "UpdateConfiguration", "agent_context": response_text[-1750:]})
            )

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        socket, self.socket = self.socket, None
        if socket is not None:
            try:
                async with asyncio.timeout(2):
                    await socket.send(json.dumps({"type": "Terminate"}))
                    while WireEvent.model_validate_json(await socket.recv()).type != "Termination":
                        pass  # Teardown finals never create new turns after media/control ended.
            except Exception:
                pass
            finally:
                await socket.close()
        self.buffer.clear()
        self.pending = None

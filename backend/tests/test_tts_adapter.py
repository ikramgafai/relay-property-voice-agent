import ast
import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from app.providers.tts.elevenlabs import (
    OUTPUT_FORMAT,
    READ_CHUNK_BYTES,
    ElevenLabsStreamingTTS,
    language_code,
)
from app.providers.tts.protocol import StreamingTTSProvider
from app.providers.tts.schemas import SpeechRequest, TTSFailure
from app.realtime.media.protocol import AudioFormat
from app.realtime.output import OutputStatus, SpeechOutput
from pydantic import SecretStr

from tests.support.output import FakeSink

KEY = "fake-elevenlabs-key-do-not-use"
VOICE = "Voice123abc"
ERROR_BODY = "PROVIDER-INTERNAL-ERROR-DETAIL"


class Stream(httpx.AsyncByteStream):
    """Deterministic streamed body: counts reads, records close, optional gate or failure."""

    def __init__(
        self,
        chunks: list[bytes],
        *,
        gate: asyncio.Event | None = None,
        fail_after: int | None = None,
        failure: Exception | None = None,
    ) -> None:
        self.chunks, self.gate = chunks, gate
        self.fail_after, self.failure = fail_after, failure
        self.reads = 0
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for index, chunk in enumerate(self.chunks):
            if self.fail_after == index and self.failure is not None:
                raise self.failure
            if self.gate is not None and index > 0:
                await self.gate.wait()
            self.reads += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class Recorder:
    def __init__(self, respond: Callable[[httpx.Request], httpx.Response]) -> None:
        self.respond = respond
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.respond(request)


def adapter(
    respond: Callable[[httpx.Request], httpx.Response],
) -> tuple[ElevenLabsStreamingTTS, Recorder, httpx.AsyncClient]:
    recorder = Recorder(respond)
    client = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    return ElevenLabsStreamingTTS(client, SecretStr(KEY), VOICE), recorder, client


def ok(stream: Stream) -> Callable[[httpx.Request], httpx.Response]:
    return lambda request: httpx.Response(200, stream=stream)


def speech(text: str = "Your request was created.", language: str | None = None) -> SpeechRequest:
    return SpeechRequest(speech_output_id=uuid4(), text=text, language=language)


async def collect(provider: StreamingTTSProvider, request: SpeechRequest) -> list[bytes]:
    async with aclosing(provider.synthesize(request)) as stream:
        return [chunk async for chunk in stream]


# --- A-I: contract and request shape ----------------------------------------------------


async def test_protocol_format_and_request_shape() -> None:
    provider: StreamingTTSProvider
    tts, recorder, client = adapter(ok(Stream([b"\x01\x00\x02\x00"])))
    provider = tts
    assert provider.format == AudioFormat(sample_rate=24000)
    assert (tts.format.encoding, tts.format.sample_rate, tts.format.channels) == (
        "pcm_s16le",
        24000,
        1,
    )
    text = "Your maintenance request was created.  Reference: keep, exactly — as given!"
    await collect(provider, speech(text))
    [request] = recorder.requests
    assert request.method == "POST"
    assert request.url.scheme == "https" and request.url.host == "api.elevenlabs.io"
    assert request.url.path == f"/v1/text-to-speech/{VOICE}/stream"
    assert (
        dict(request.url.params)
        == {"output_format": "pcm_24000"}
        == {"output_format": OUTPUT_FORMAT}
    )
    assert request.headers["xi-api-key"] == KEY
    assert request.headers["content-type"] == "application/json"
    body = json.loads(request.content)
    assert body == {"text": text, "model_id": "eleven_flash_v2_5"}
    assert "optimize_streaming_latency" not in str(request.url) + request.content.decode()
    for unused in ("previous_text", "next_text", "voice_settings", "apply_text_normalization"):
        assert unused not in body
    assert request.extensions["timeout"] == {"connect": 5, "read": 5, "write": 5, "pool": 5}
    assert tts.provider_name == "elevenlabs" and tts.model == "eleven_flash_v2_5"
    await client.aclose()


async def test_voice_id_only_in_its_path_segment() -> None:
    tts, recorder, client = adapter(ok(Stream([b"\x01\x00"])))
    await collect(tts, speech())
    [request] = recorder.requests
    assert request.url.path.split("/") == ["", "v1", "text-to-speech", VOICE, "stream"]
    assert VOICE not in str(request.url.params) and VOICE not in request.content.decode()
    hostile = ElevenLabsStreamingTTS(client, SecretStr(KEY), "a/../b?x=1")
    # Defense in depth (settings already restrict voice IDs): the wire path keeps the value
    # as a single encoded segment. httpx's ``.path`` is the decoded view, so check raw_path.
    assert hostile._url.raw_path == b"/v1/text-to-speech/a%2F..%2Fb%3Fx%3D1/stream"
    await client.aclose()


# --- J-M: language mapping --------------------------------------------------------------


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        (None, None),
        ("en", "en"),
        ("EN", "en"),
        ("en-US", "en"),
        ("ar-MA", "ar"),
        ("fr-FR", "fr"),
        ("zh-Hant-TW", "zh"),
        ("fil", None),
        ("yue-HK", None),
    ],
)
def test_language_code_mapping(tag: str | None, expected: str | None) -> None:
    assert language_code(tag) == expected


@pytest.mark.parametrize(
    ("tag", "expected"),
    [("en", "en"), ("en-US", "en"), ("ar-MA", "ar"), (None, None), ("fil", None)],
)
async def test_language_code_in_request_body(tag: str | None, expected: str | None) -> None:
    tts, recorder, client = adapter(ok(Stream([b"\x01\x00"])))
    request = speech(language=tag)
    await collect(tts, request)
    body = json.loads(recorder.requests[0].content)
    assert body.get("language_code") == expected
    assert ("language_code" in body) is (expected is not None)
    assert request.language == tag  # the provider-neutral request is never mutated
    await client.aclose()


# --- N-Q, W: streaming, early close and cancellation ------------------------------------


async def test_streams_chunks_incrementally_in_order() -> None:
    gate = asyncio.Event()
    body = Stream([b"\x01\x00" * 10, b"\x02\x00" * 10, b"\x03\x00" * 10], gate=gate)
    tts, _, client = adapter(ok(body))
    async with aclosing(tts.synthesize(speech())) as stream:
        first = await anext(stream)
        assert first == b"\x01\x00" * 10 and body.reads == 1  # yielded before the rest arrived
        gate.set()
        rest = [chunk async for chunk in stream]
    assert rest == [b"\x02\x00" * 10, b"\x03\x00" * 10] and body.closed
    await client.aclose()


async def test_arbitrary_boundaries_preserved_and_bounded_chunks() -> None:
    pcm = bytes(range(256)) * 60  # 15360 bytes, odd provider boundaries
    parts = [pcm[:1], pcm[1:7], pcm[7:9000], pcm[9000:]]
    tts, _, client = adapter(ok(Stream(parts)))
    chunks = await collect(tts, speech())
    assert b"".join(chunks) == pcm
    assert all(len(chunk) <= READ_CHUNK_BYTES == 4800 for chunk in chunks)
    await client.aclose()


async def test_early_aclose_releases_response_without_reading_rest() -> None:
    gate = asyncio.Event()
    body = Stream([b"\x01\x00"] * 50, gate=gate)
    tts, _, client = adapter(ok(body))
    async with aclosing(tts.synthesize(speech())) as stream:
        assert await anext(stream) == b"\x01\x00"
    assert body.closed and body.reads == 1
    await client.aclose()


async def test_task_cancellation_closes_response_and_propagates() -> None:
    gate = asyncio.Event()
    body = Stream([b"\x01\x00"] * 50, gate=gate)
    tts, _, client = adapter(ok(body))
    received: list[bytes] = []

    async def consume() -> None:
        async with aclosing(tts.synthesize(speech())) as stream:
            async for chunk in stream:
                received.append(chunk)

    task = asyncio.create_task(consume())
    for _ in range(100):
        if received:
            break
        await asyncio.sleep(0)
    task.cancel()
    result = await asyncio.gather(task, return_exceptions=True)
    assert isinstance(result[0], asyncio.CancelledError)  # not converted into TTSFailure
    assert body.closed and body.reads == 1 and received == [b"\x01\x00"]
    await client.aclose()


async def test_valid_stream_completes_normally() -> None:
    body = Stream([b"\x00\x01" * 2400] * 3)
    tts, _, client = adapter(ok(body))
    chunks = await collect(tts, speech())
    assert sum(len(c) for c in chunks) == 14400 and body.closed
    await client.aclose()


# --- R-V: failures ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectTimeout("connect stalled"),
        httpx.ReadTimeout("no first audio"),
        httpx.PoolTimeout("pool exhausted"),
    ],
)
async def test_timeouts_before_audio_map_to_timeout(error: Exception) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        raise error

    tts, _, client = adapter(respond)
    with pytest.raises(TTSFailure) as failure:
        await collect(tts, speech())
    assert failure.value.reason == "TIMEOUT"
    await client.aclose()


async def test_inter_chunk_stall_maps_to_timeout() -> None:
    body = Stream([b"\x01\x00", b"\x02\x00"], fail_after=1, failure=httpx.ReadTimeout("stall"))
    tts, _, client = adapter(ok(body))
    received: list[bytes] = []
    with pytest.raises(TTSFailure) as failure:
        async with aclosing(tts.synthesize(speech())) as stream:
            async for chunk in stream:
                received.append(chunk)
    assert failure.value.reason == "TIMEOUT" and received == [b"\x01\x00"] and body.closed
    await client.aclose()


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 429, 500, 503])
async def test_http_errors_are_safe_unavailable(
    status: int, capsys: pytest.CaptureFixture[str]
) -> None:
    body = Stream([f'{{"detail": "{ERROR_BODY} {KEY}"}}'.encode()])
    tts, _, client = adapter(lambda request: httpx.Response(status, stream=body))
    with pytest.raises(TTSFailure) as failure:
        await collect(tts, speech())
    exc = failure.value
    assert exc.reason == "UNAVAILABLE" and str(exc) == "UNAVAILABLE"
    assert exc.__cause__ is None
    assert body.reads == 0 and body.closed  # error body never read
    for text in (repr(exc), str(exc.args), capsys.readouterr().out):
        assert ERROR_BODY not in text and KEY not in text
    await client.aclose()


async def test_transport_error_is_unavailable_without_detail() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"refused {KEY}")

    tts, _, client = adapter(respond)
    with pytest.raises(TTSFailure) as failure:
        await collect(tts, speech())
    assert failure.value.reason == "UNAVAILABLE" and KEY not in repr(failure.value)
    assert failure.value.__cause__ is None and failure.value.__suppress_context__
    await client.aclose()


@pytest.mark.parametrize("parts", [[], [b""], [b"", b""]])
async def test_success_without_audio_is_invalid_audio(parts: list[bytes]) -> None:
    tts, _, client = adapter(ok(Stream(parts)))
    with pytest.raises(TTSFailure) as failure:
        await collect(tts, speech())
    assert failure.value.reason == "INVALID_AUDIO"
    await client.aclose()


def test_api_key_never_in_repr() -> None:
    tts = ElevenLabsStreamingTTS(httpx.AsyncClient(), SecretStr(KEY), VOICE)
    assert KEY not in repr(tts) and KEY not in str(vars(tts))
    assert "eleven_flash_v2_5" in repr(tts)


# --- X-Y: boundaries and SpeechOutput compatibility -------------------------------------


def test_adapter_dependency_boundary() -> None:
    import app

    path = Path(app.__file__).parent / "providers" / "tts" / "elevenlabs.py"
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom):
            modules.add(node.module or "")
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    forbidden = (
        "livekit",
        "app.realtime.session",
        "app.realtime.output",
        "app.workflows",
        "app.tools",
        "app.db",
        "app.agent",
        "app.routing",
        "elevenlabs",
    )
    assert not any(module.startswith(forbidden) for module in modules), modules
    # Step 7A: the adapter is constructed only by the composition root.
    for runtime in ("api/voice.py", "realtime/session.py", "realtime/speech.py"):
        assert "elevenlabs" not in (Path(app.__file__).parent / runtime).read_text().lower()


async def test_speech_output_runs_on_the_adapter() -> None:
    pcm = (bytes(range(256)) * 15)[: 480 * 2 * 3 + 100]
    tts, _, client = adapter(ok(Stream([pcm[:1001], pcm[1001:]])))
    sink = FakeSink()
    output = SpeechOutput(tts, sink, provider_name=tts.provider_name, model=tts.model)
    output.start()
    record = output.present(uuid4(), "Hello from the adapter.")
    assert record is not None
    async with asyncio.timeout(3):
        await record.ended.wait()
    assert record.status == OutputStatus.COMPLETED and record.provider == "elevenlabs"
    assert b"".join(frame.data for frame in sink.frames) == pcm
    await output.close()
    await client.aclose()

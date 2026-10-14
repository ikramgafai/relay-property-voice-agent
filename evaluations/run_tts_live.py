"""Explicit, opt-in real TTS smoke. Never imported or executed by pytest, CI or startup.

One request through the production ElevenLabs adapter: no retries, no audio saved unless
--wav names a path outside the repository. Prints measurements only; never the API key,
provider response bodies or the voice ID.
"""

import argparse
import asyncio
import json
import math
import wave
from array import array
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import httpx
from app.core.config import load_settings
from app.providers.tts.elevenlabs import OUTPUT_FORMAT, ElevenLabsStreamingTTS
from app.providers.tts.protocol import StreamingTTSProvider
from app.providers.tts.schemas import SpeechRequest, TTSFailure
from app.realtime.media.pcm import PcmAlignmentError, PcmFramer

DEFAULT_TEXT = "Relay voice output is working."
REPOSITORY = Path(__file__).resolve().parents[1]


def ms(start: float, end: float | None) -> float | None:
    return None if end is None else round((end - start) * 1000, 1)


async def measure(provider: StreamingTTSProvider, text: str) -> tuple[dict[str, object], bytes]:
    request = SpeechRequest(speech_output_id=uuid4(), text=text)
    framer = PcmFramer(provider.format)
    chunks: list[int] = []
    pcm = bytearray()
    frames = []
    first_audio: float | None = None
    stream_ended: float | None = None
    status, alignment = "SUCCESS", "OK"
    request_start = perf_counter()
    synthesis_requested = perf_counter()
    try:
        async for chunk in provider.synthesize(request):
            if chunk and first_audio is None:
                first_audio = perf_counter()
            chunks.append(len(chunk))
            pcm.extend(chunk)
            frames += framer.push(chunk)
        stream_ended = perf_counter()
        frames += framer.finish()
    except TTSFailure as failure:
        stream_ended = stream_ended or perf_counter()
        status = {"TIMEOUT": "TTS_TIMEOUT", "INVALID_AUDIO": "INVALID_AUDIO"}.get(
            failure.reason, "TTS_UNAVAILABLE"
        )
    except PcmAlignmentError:
        status, alignment = "INVALID_AUDIO", "ODD_TRAILING_BYTE"
    framing_completed = perf_counter()
    samples = array("h", pcm[: len(pcm) - len(pcm) % 2])
    peak = max((abs(s) for s in samples), default=0)
    rms = math.sqrt(sum(s * s for s in samples) / len(samples)) if samples else 0.0
    framed = b"".join(f.data for f in frames)
    full = [f for f in frames if len(f.data) == framer.frame_bytes]
    report: dict[str, object] = {
        "status": status,
        "characters": len(text),
        "format_interpreted_as": provider.format.model_dump(),
        "ms_request_to_synthesis_requested": ms(request_start, synthesis_requested),
        "ms_to_first_audio_byte": ms(synthesis_requested, first_audio),
        "ms_stream_duration": ms(synthesis_requested, stream_ended),
        "ms_to_framing_completed": ms(request_start, framing_completed),
        "total_bytes": len(pcm),
        "byte_count_even": len(pcm) % 2 == 0,
        "samples": len(samples),
        "audio_seconds": round(len(samples) / provider.format.sample_rate, 3),
        "chunks": len(chunks),
        "chunk_bytes_min": min(chunks, default=0),
        "chunk_bytes_max": max(chunks, default=0),
        "chunk_bytes_avg": round(sum(chunks) / len(chunks), 1) if chunks else 0,
        "full_20ms_frames": len(full),
        "final_remainder_bytes": len(frames[-1].data) if frames and frames[-1] not in full else 0,
        "framer_alignment": alignment,
        "framed_bytes_equal_input": status == "SUCCESS" and framed == bytes(pcm),
        "pcm_peak": peak,
        "pcm_rms": round(rms, 1),
    }
    return report, bytes(pcm)


def save_wav(path: Path, pcm: bytes, rate: int) -> None:
    target = path.resolve()
    if target.is_relative_to(REPOSITORY):
        raise SystemExit("--wav must point outside the repository; voice output is never tracked")
    with wave.open(str(target), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(pcm)


async def run(text: str, wav: Path | None) -> dict[str, object]:
    settings = load_settings()
    assert settings.elevenlabs_api_key and settings.tts_voice_id
    async with httpx.AsyncClient() as client:
        provider = ElevenLabsStreamingTTS(
            client, settings.elevenlabs_api_key, settings.tts_voice_id, settings.tts_model
        )
        report, pcm = await measure(provider, text)
    report = {
        "provider": provider.provider_name,
        "model": provider.model,
        "output_format": OUTPUT_FORMAT,
        "voice_id": "configured (redacted)",
        "real_requests": 1,
        **report,
    }
    if wav is not None and pcm:
        save_wav(wav, pcm, provider.format.sample_rate)
        report["wav_saved_outside_repository"] = True
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--text", default=DEFAULT_TEXT)
    parser.add_argument("--wav", type=Path, help="opt-in WAV path outside the repository")
    args = parser.parse_args()
    settings = load_settings()
    missing = [
        name
        for name, value in (
            ("ELEVENLABS_API_KEY", settings.elevenlabs_api_key),
            ("TTS_VOICE_ID", settings.tts_voice_id),
        )
        if not value
    ]
    if missing:
        print(json.dumps({"status": "LIVE PROVIDER VALIDATION PENDING", "missing": missing}))
        return
    try:
        report = asyncio.run(run(args.text, args.wav))
    except Exception:
        print(json.dumps({"status": "LIVE_PROVIDER_VALIDATION_FAILED", "detail": "withheld"}))
        raise SystemExit(1) from None
    print(json.dumps(report, indent=2))
    if report["status"] != "SUCCESS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()

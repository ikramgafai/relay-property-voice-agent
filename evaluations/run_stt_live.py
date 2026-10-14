"""Explicit, opt-in real STT smoke. Never imported or executed by pytest."""

import argparse
import asyncio
import json
import wave
from pathlib import Path
from time import perf_counter

from app.core.config import load_settings
from app.providers.stt.assemblyai import AssemblyAIStreaming
from app.providers.stt.schemas import SpeechContext
from app.realtime.media.protocol import AudioFrame


async def evaluate(path: Path, expected: str) -> dict[str, object]:
    settings = load_settings()
    provider = AssemblyAIStreaming(settings)
    with wave.open(str(path), "rb") as audio:
        if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (
            1,
            2,
            settings.stt_sample_rate,
        ):
            raise ValueError("WAV must be mono PCM16 at configured STT_SAMPLE_RATE")
        if audio.getnframes() / audio.getframerate() > 60:
            raise ValueError("Smoke fixture must be at most 60 seconds")
        pcm = audio.readframes(audio.getnframes())
    results: list[str] = []
    partials = 0
    first_audio: float | None = None
    first_partial_ms: float | None = None
    final_ms: list[float] = []
    got_final = asyncio.Event()

    async def receive() -> None:
        nonlocal partials, first_partial_ms
        while True:
            event = await provider.receive()
            if event.kind == "PARTIAL":
                partials += 1
                if first_audio is not None and first_partial_ms is None:
                    first_partial_ms = (perf_counter() - first_audio) * 1000
            elif event.kind == "FINAL":
                results.append(event.transcript)
                if first_audio is not None:
                    final_ms.append((perf_counter() - first_audio) * 1000)
                got_final.set()

    task: asyncio.Task[None] | None = None
    tick = perf_counter()
    try:
        await provider.open(SpeechContext())
        task = asyncio.create_task(receive(), name="relay-live-stt-receiver")
        size = settings.stt_sample_rate // 20 * 2
        # Include explicit trailing silence as audio; provider decides when the turn ends.
        data = pcm + bytes(settings.stt_sample_rate * 2 * 3)
        first_audio = perf_counter()
        for sequence, start in enumerate(range(0, len(data), size)):
            if task.done():
                task.result()
            await provider.send(
                AudioFrame(
                    data=data[start : start + size], format=provider.format, sequence=sequence
                )
            )
            await asyncio.sleep(0.05)
        async with asyncio.timeout(10):
            await got_final.wait()
        return {
            "status": "LIVE_PROVIDER_VALIDATED",
            "provider": "assemblyai",
            "requested_model": settings.stt_model,
            "reported_model": provider.actual_model,
            "expected_text": expected,
            "final_transcripts": results,
            "partial_count": partials,
            "first_audio_to_first_partial_ms": first_partial_ms,
            "first_audio_to_final_ms": final_ms,
            "duration_ms": (perf_counter() - tick) * 1000,
            "language_codes": settings.stt_language_codes,
        }
    finally:
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await provider.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wav", type=Path)
    parser.add_argument("--expected", default="")
    args = parser.parse_args()
    if not load_settings().assemblyai_api_key:
        print(
            json.dumps(
                {
                    "status": "LIVE PROVIDER VALIDATION PENDING",
                    "reason": "ASSEMBLYAI_API_KEY unavailable",
                }
            )
        )
        return
    if not args.wav:
        parser.error("--wav is required for live validation")
    try:
        print(
            json.dumps(asyncio.run(evaluate(args.wav, args.expected)), ensure_ascii=False, indent=2)
        )
    except Exception:
        print(
            json.dumps(
                {
                    "status": "LIVE_PROVIDER_VALIDATION_FAILED",
                    "reason": "Check configuration and approved fixture; provider details withheld",
                }
            )
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

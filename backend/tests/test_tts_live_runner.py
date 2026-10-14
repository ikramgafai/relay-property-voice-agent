"""The live TTS smoke's measurement logic, exercised offline with deterministic providers."""

import math
from array import array
from pathlib import Path

import pytest

from evaluations import run_tts_live
from evaluations.run_tts_live import measure, save_wav
from tests.support.speech import FakeTTS


def tone(samples: int) -> bytes:
    return array("h", (int(8000 * math.sin(i / 10)) for i in range(samples))).tobytes()


async def test_measure_reports_framing_and_signal() -> None:
    pcm = tone(480 * 3 + 100)
    report, audio = await measure(FakeTTS([pcm[:7], pcm[7:2001], pcm[2001:]]), "Hello there.")
    assert audio == pcm and report["status"] == "SUCCESS"
    assert report["characters"] == 12 and report["total_bytes"] == len(pcm)
    assert report["samples"] == 1540 and report["audio_seconds"] == round(1540 / 24000, 3)
    assert report["chunks"] == 3 and report["chunk_bytes_min"] == 7
    assert report["full_20ms_frames"] == 3 and report["final_remainder_bytes"] == 200
    assert report["framer_alignment"] == "OK" and report["framed_bytes_equal_input"] is True
    peak, rms = report["pcm_peak"], report["pcm_rms"]
    assert isinstance(peak, int) and isinstance(rms, float) and peak > 7000 and rms > 1000
    assert report["ms_to_first_audio_byte"] is not None


@pytest.mark.parametrize(
    ("provider", "status"),
    [
        (FakeTTS([b"\x01\x00"], fail_after=0), "TTS_UNAVAILABLE"),
        (FakeTTS([b"\x01\x00\x02"]), "INVALID_AUDIO"),
    ],
)
async def test_measure_reports_safe_failures(provider: FakeTTS, status: str) -> None:
    report, _ = await measure(provider, "Hello.")
    assert report["status"] == status and report["framed_bytes_equal_input"] is False


def test_wav_refused_inside_repository(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        save_wav(run_tts_live.REPOSITORY / "evaluations" / "x.wav", b"\x00\x00", 24000)
    target = tmp_path / "outside.wav"
    save_wav(target, tone(480), 24000)
    assert target.stat().st_size > 960


def test_runner_is_not_collected_or_auto_executed() -> None:
    source = Path(run_tts_live.__file__).read_text()
    assert (
        'if __name__ == "__main__":' in source and "test_" not in Path(run_tts_live.__file__).name
    )

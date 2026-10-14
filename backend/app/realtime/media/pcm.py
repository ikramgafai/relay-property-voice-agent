"""Pure PCM16 framing: arbitrary byte chunks into fixed-duration AudioFrames.

No resampling, padding or provider/transport knowledge. Exact sample order is preserved.
"""

from app.realtime.media.protocol import AudioFormat, AudioFrame

SAMPLE_WIDTH = 2  # pcm_s16le


class PcmAlignmentError(ValueError):
    """Terminal PCM data is not sample-aligned; no audio is invented to repair it."""


class PcmFramer:
    def __init__(self, format: AudioFormat, frame_ms: int = 20) -> None:
        if not 10 <= frame_ms <= 100 or (format.sample_rate * frame_ms) % 1000:
            raise ValueError("Frame duration must give a whole number of samples")
        self.format = format
        self.frame_bytes = format.sample_rate * frame_ms // 1000 * format.channels * SAMPLE_WIDTH
        self.sequence = 0
        self.finished = False
        self._pending = bytearray()

    @property
    def pending_bytes(self) -> int:
        return len(self._pending)

    def push(self, chunk: bytes) -> list[AudioFrame]:
        """Return every complete frame now available; keep the remainder (including a
        trailing odd byte) for the next chunk."""
        if self.finished:
            raise RuntimeError("Framer already finished")
        self._pending.extend(chunk)
        frames: list[AudioFrame] = []
        while len(self._pending) >= self.frame_bytes:
            frames.append(self._frame(bytes(self._pending[: self.frame_bytes])))
            del self._pending[: self.frame_bytes]
        return frames

    def finish(self) -> list[AudioFrame]:
        """Emit the aligned remainder as one shorter final frame, never padded."""
        if self.finished:
            return []
        self.finished = True
        remainder, self._pending = bytes(self._pending), bytearray()
        if len(remainder) % SAMPLE_WIDTH:
            raise PcmAlignmentError("PCM16 stream ended mid-sample")
        return [self._frame(remainder)] if remainder else []

    def _frame(self, data: bytes) -> AudioFrame:
        self.sequence += 1
        return AudioFrame(data=data, format=self.format, sequence=self.sequence)

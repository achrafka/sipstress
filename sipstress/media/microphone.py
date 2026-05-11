"""Optional microphone capture at 8 kHz mono for live RTP (+ duplex WAV).

Capture uses **float32** from PortAudio then scales into int16 with deliberate
headroom. That avoids “loud / harsh / clipped” WAV and PCMU when hardware
delivers hot levels or int16 was previously saturated before G.711 encode.
"""
from __future__ import annotations

import asyncio
import logging
import struct
from collections import deque
from typing import Deque, List, Optional

from .rtp import SAMPLES_PER_PACKET, decode_payload

log = logging.getLogger("sipstress.microphone")

_SILENCE_PCM16: List[int] = decode_payload(
    "pcmu", bytes([0xFF]) * SAMPLES_PER_PACKET
)

# Multiply PortAudio float magnitude (<=1 typical) before mapping to int16.
# ~0.82 keeps a few dBFS headroom so µ-law encode + playback do not sound slammed.
_DEFAULT_FLOAT_HEADROOM = 0.82


class AsyncMicPCM8kMono:
    """Queue of PCM16 mono frames (160 samples ≈ 20 ms at 8 kHz).

    Install: ``pip install 'sipstress[audio]'`` (sounddevice + numpy).

    ``linear_scale`` is applied on top of built-in headroom (default 0.82);
    it matches :class:`CallConfig.mic_gain`.
    """

    FRAME_SAMPLES = SAMPLES_PER_PACKET

    def __init__(self, *, linear_scale: float = 1.0) -> None:
        self._frames: Deque[List[int]] = deque(maxlen=500)
        self._stream: Optional[object] = None
        self.silence_fallbacks = 0
        ls = float(linear_scale)
        if ls <= 0 or ls > 4.0:
            log.warning("mic linear_scale %s out of range; clamping to [0.1, 4]", ls)
            ls = max(0.1, min(4.0, ls))
        self._float_scale = _DEFAULT_FLOAT_HEADROOM * ls

    async def __aenter__(self) -> "AsyncMicPCM8kMono":
        try:
            import sounddevice as sd
            import numpy as np  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "--microphone requires optional deps: pip install 'sipstress[audio]' "
                "(sounddevice)."
            ) from exc

        loop = asyncio.get_running_loop()

        def callback(indata, frames, *_t):  # type: ignore[no-untyped-def]
            try:
                import numpy as np
            except ImportError:
                log.error("numpy missing; install with: pip install numpy")
                return
            x = np.ascontiguousarray(
                indata[:, 0] if getattr(indata, "ndim", 1) > 1 else indata.ravel()
            ).astype(np.float32, copy=False)
            if int(x.shape[0]) != AsyncMicPCM8kMono.FRAME_SAMPLES:
                return
            peak = float(np.max(np.abs(x))) if x.size else 0.0
            sc = self._float_scale
            if peak > 1.0:
                x = x * (sc / peak)
            else:
                x = x * sc
            pcm = np.clip(np.rint(np.clip(x, -1.0, 1.0) * 32767.0), -32768, 32767).astype(
                np.int16
            )
            loop.call_soon_threadsafe(self._enqueue_bytes, pcm.tobytes())

        self._stream = sd.InputStream(
            samplerate=8000,
            channels=1,
            dtype="float32",
            blocksize=self.FRAME_SAMPLES,
            callback=callback,
        )
        self._stream.start()  # type: ignore[union-attr]
        log.info(
            "microphone capture started (float32→int16, %.2f headroom×gain, %s-sample @ 8 kHz)",
            self._float_scale,
            self.FRAME_SAMPLES,
        )
        return self

    def _enqueue_bytes(self, raw16: bytes) -> None:
        self._frames.append(list(struct.unpack(f"<{SAMPLES_PER_PACKET}h", raw16)))

    async def __aexit__(self, *exc_info) -> None:  # type: ignore[no-untyped-def]
        await self.aclose()

    async def aclose(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()  # type: ignore[union-attr]
                self._stream.close()  # type: ignore[union-attr]
            except Exception:
                log.debug("mic stream teardown", exc_info=True)
            self._stream = None
        self._frames.clear()

    async def next_frame_or_silence(self) -> List[int]:
        """Next mic frame after short spin; overrun uses comfort-noise-ish silence."""
        for _ in range(12):
            if self._frames:
                return list(self._frames.popleft())
            await asyncio.sleep(0.003)
        self.silence_fallbacks += 1
        return list(_SILENCE_PCM16)

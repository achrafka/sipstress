"""PCM 16-bit linear to G.711 µ-law (PCMU) for outbound RTP."""
from __future__ import annotations

import struct
from typing import List

_audioop = None
try:
    import audioop as _audioop  # Python 3.9–3.12 (removed in 3.13 stdlib)
except ImportError:
    _audioop = None

if _audioop is None:
    try:
        import audioop_lts as _audioop  # type: ignore
    except ImportError:
        _audioop = None


def encode_pcm16_to_pcmu(samples: List[int]) -> bytes:
    """Encode 160 × int16 PCM samples into 160 octets PCMU."""
    if not samples:
        return b""
    raw = struct.pack(f"<{len(samples)}h", *samples)
    if _audioop is None:
        raise RuntimeError(
            "PCMU microphone encoding requires the 'audioop' module "
            "(Python 3.9–3.12) or the PyPI backport package 'audioop-lts' "
            "(Python 3.13+). Install with: pip install audioop-lts"
        )
    return _audioop.lin2ulaw(raw, 2)  # type: ignore[union-attr]


def pcmu_encoding_available() -> bool:
    return _audioop is not None


def pcma_encoding_available() -> bool:
    return _audioop is not None


def encode_pcm16_to_pcma(samples: List[int]) -> bytes:
    if not samples:
        return b""
    raw = struct.pack(f"<{len(samples)}h", *samples)
    if _audioop is None:
        raise RuntimeError(
            "PCMA microphone encoding requires 'audioop' or 'audioop-lts'; "
            "see encode_pcm16_to_pcmu docstring."
        )
    return _audioop.lin2alaw(raw, 2)  # type: ignore[union-attr]

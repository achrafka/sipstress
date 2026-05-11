"""RTP send/receive with RFC 4733 DTMF.

The audio side emits comfort-noise PCMU/PCMA packets at 20ms intervals and
measures jitter (RFC 3550 §A.8) plus packet loss on the inbound stream.

The DTMF side implements RFC 4733 telephone-event:

  - When `enqueue_dtmf("123")` is called, each digit is encoded as a
    sequence of telephone-event RTP packets that are *interleaved* with
    audio (we replace audio packets in the send loop while the digit is
    active so the RTP timestamp/sequence stay coherent).
  - On receive, telephone-event packets are decoded and surfaced as
    `received_dtmf` entries with timestamps.

The receive side tracks audio energy windows (mean absolute value of
PCM samples after PCMU/PCMA decoding) so we can answer "is the IVR talking
to us?".

Recording options:

* **Mono inbound** / **duplex LEFT**: WAV uses decoded inbound audio; optional mild
  **recording gain** (default below 1.0) reins in loud callee audio after pickup vs quieter ringback.
* **Stereo duplex** + ``record_duplex``: left = received (remote audio),
  right = PCM **decoded from the RTP audio payload we transmitted** each 20 ms tick
  (same as goes on the wire after G.711 encode), not raw soundcard PCM before encode.
  DTMF (telephone-event) ticks write silence on the right for alignment with real audio slots.
* **Microphone** live TX: encode captured 8 kHz mono PCM into PCMU/PCMA per
  20 ms RTP slot (**optional** deps: sounddevice, numpy, and ``audioop`` /
  ``audioop_lts`` for encoding).

Optional WAV recording paths are mutually configured by the scenario."""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import struct
import time
import wave
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, List, Optional, Tuple

PT_PCMU = 0
PT_PCMA = 8
PT_TELEPHONE_EVENT = 101

CODEC_TO_PT = {"pcmu": PT_PCMU, "pcma": PT_PCMA}
SAMPLES_PER_PACKET = 160  # 20 ms at 8000 Hz
PACKET_INTERVAL_S = 0.020
CLOCK_RATE = 8000

log = logging.getLogger("sipstress.rtp")
DTMF_DIGIT_TO_EVENT = {
    "0": 0, "1": 1, "2": 2, "3": 3, "4": 4,
    "5": 5, "6": 6, "7": 7, "8": 8, "9": 9,
    "*": 10, "#": 11,
    "A": 12, "B": 13, "C": 14, "D": 15,
    "a": 12, "b": 13, "c": 14, "d": 15,
}

DTMF_EVENT_TO_DIGIT = {v: k.upper() for k, v in DTMF_DIGIT_TO_EVENT.items()}


# ---------- PCMU / PCMA decoding (small, fast, no numpy) ----------

def _ulaw_decode(byte: int) -> int:
    """Decode a single µ-law byte to a 16-bit linear PCM sample."""
    byte = ~byte & 0xFF
    sign = byte & 0x80
    exponent = (byte >> 4) & 0x07
    mantissa = byte & 0x0F
    sample = ((mantissa << 3) + 0x84) << exponent
    sample -= 0x84
    return -sample if sign else sample


def _alaw_decode(byte: int) -> int:
    byte ^= 0x55
    sign = byte & 0x80
    exponent = (byte >> 4) & 0x07
    mantissa = byte & 0x0F
    if exponent == 0:
        sample = (mantissa << 4) + 8
    else:
        sample = ((mantissa << 4) + 0x108) << (exponent - 1)
    return -sample if sign else sample


_ULAW_TABLE = [_ulaw_decode(i) for i in range(256)]
_ALAW_TABLE = [_alaw_decode(i) for i in range(256)]


def decode_payload(codec: str, payload: bytes) -> List[int]:
    table = _ALAW_TABLE if codec == "pcma" else _ULAW_TABLE
    return [table[b] for b in payload]


def _pcm16_to_bytes(samples: List[int]) -> bytes:
    return struct.pack(f"<{len(samples)}h", *samples)


# ---------- DTMF queue ----------

@dataclass
class _DtmfStep:
    event: int
    duration_packets: int  # how many 20ms packets total (including end packets)
    volume: int = 10  # dBm0


@dataclass
class DtmfRxEvent:
    digit: str
    t_first_seen: float  # monotonic clock when first packet arrived
    duration_ms: int


@dataclass
class AudioEnergySample:
    t: float
    rms: float


class _RtpProto(asyncio.DatagramProtocol):
    def __init__(self, owner: "RtpStream") -> None:
        self._owner = owner

    def datagram_received(self, data, addr):  # type: ignore[override]
        self._owner._on_rtp(data, addr, time.monotonic())

    def error_received(self, exc):  # type: ignore[override]
        log.debug("RTP error: %s", exc)


class RtpStream:
    def __init__(
        self,
        local_ip: str,
        local_port: int,
        remote_ip: str,
        remote_port: int,
        codec: str = "pcmu",
        record_wav_path: Optional[str] = None,
        record_duplex: bool = False,
        microphone_reader: Optional[object] = None,
        mic_peak_abs: int = 30400,
        dtmf_pt: int = PT_TELEPHONE_EVENT,
        *,
        inbound_record_gain: float = 0.72,
    ) -> None:
        self.local_ip = local_ip
        self.local_port = local_port
        self.remote_ip = remote_ip
        self.remote_port = remote_port
        self.codec = codec
        silence_b = self._silence_payload(codec)
        self.audio_payload = silence_b
        self.audio_pt = CODEC_TO_PT.get(codec, PT_PCMU)
        self.dtmf_pt = dtmf_pt

        self._ssrc = int.from_bytes(os.urandom(4), "big") & 0xFFFFFFFF
        self._seq = int.from_bytes(os.urandom(2), "big") & 0xFFFF
        self._timestamp = int.from_bytes(os.urandom(4), "big") & 0xFFFFFFFF

        self._transport: Optional[asyncio.DatagramTransport] = None
        self._send_task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

        # rx stats
        self._packets_sent = 0
        self._packets_recv = 0
        self._first_seq: Optional[int] = None
        self._last_seq: Optional[int] = None
        self._max_seq: Optional[int] = None
        self._cycles = 0
        self._jitter = 0.0
        self._last_transit: Optional[float] = None
        self._last_rtp_ts: Optional[int] = None

        # DTMF
        self._dtmf_queue: Deque[_DtmfStep] = deque()
        self._sent_dtmf: List[Tuple[float, str]] = []  # (t, digit)
        self._received_dtmf: List[DtmfRxEvent] = []
        self._rx_dtmf_in_flight: Optional[Tuple[int, float, int]] = None

        self._energy_window_s = 0.5
        self._energy_window: Deque[Tuple[float, float]] = deque()
        self._energy_log: List[AudioEnergySample] = []
        self._silence_threshold_rms = 200.0

        silence_pcm = decode_payload(codec, silence_b)
        self._silence_pcm_160 = self._pcm_trim_160(silence_pcm)

        self._record_duplex = bool(record_duplex and record_wav_path)
        self._mic_reader = microphone_reader
        self._mic_peak_abs = max(16000, min(32700, int(mic_peak_abs)))
        ig = float(inbound_record_gain)
        if ig <= 0 or ig > 2.0:
            log.warning(
                "inbound_record_gain %.3f out of range; clamping to (0, 2]",
                ig,
            )
            ig = max(0.05, min(2.0, ig))
        self._inbound_record_gain = ig
        self._duplex_rx_160 = list(self._silence_pcm_160)

        self._media_started_mono: Optional[float] = None
        self._tx_rms_sum = 0.0
        self._tx_rms_ticks = 0

        self._record_path = record_wav_path
        self._wav: Optional[wave.Wave_write] = None

    # ---------- helpers ----------
    @staticmethod
    def _pcm_trim_160(samples: List[int]) -> List[int]:
        if len(samples) >= SAMPLES_PER_PACKET:
            return list(samples[:SAMPLES_PER_PACKET])
        pad = [0] * (SAMPLES_PER_PACKET - len(samples))
        return list(samples) + pad

    def _inbound_pcm_for_recording(self, trimmed: List[int]) -> List[int]:
        """Scale inbound PCM written to WAV / duplex-L only (metrics use raw RTP decode)."""
        g = self._inbound_record_gain
        if g == 1.0:
            return list(trimmed)
        return [
            int(max(-32768, min(32767, round(float(s) * g)))) for s in trimmed
        ]

    def _accumulate_tx_energy(self, pcm: List[int]) -> None:
        if not pcm:
            return
        rms = sum(abs(s) for s in pcm) / len(pcm)
        self._tx_rms_sum += rms
        self._tx_rms_ticks += 1

    @staticmethod
    def _limit_mic_tx_pcm(samples: List[int], peak_cap: int) -> List[int]:
        """Attenuate frames that would clip or overload G.711 after hot capture."""
        if not samples:
            return samples
        cap = max(1, int(peak_cap))
        m = max(abs(s) for s in samples)
        if m <= cap:
            return samples
        sc = cap / float(m)
        return [int(round(s * sc)) for s in samples]

    def _pcm_decoded_from_tx_body(self, body: bytes) -> List[int]:
        """Linear PCM trim for one RTP tick from the outbound codec payload."""
        samples = decode_payload(self.codec, body)
        return self._pcm_trim_160(samples)

    def _wav_write_duplex_tick(self, tx_pcm_160: List[int]) -> None:
        if not self._wav or not self._record_duplex:
            return
        rx = self._duplex_rx_160
        inter: List[int] = []
        for i in range(SAMPLES_PER_PACKET):
            inter.append(rx[i])
            inter.append(tx_pcm_160[i])
        try:
            self._wav.writeframes(_pcm16_to_bytes(inter))
        except Exception:  # noqa: BLE001
            pass

    def _rtp_audio_packet(self, body: bytes) -> bytes:
        first = 0x80
        second = self.audio_pt & 0x7F
        header = struct.pack(
            "!BBHII", first, second, self._seq, self._timestamp, self._ssrc
        )
        return header + body

    def _body_for_tx_tick(self, pcm_live: Optional[List[int]] = None) -> bytes:
        """RTP codec body for one 20 ms slot."""
        if pcm_live is None:
            return self.audio_payload
        from .pcmu_encode import encode_pcm16_to_pcma, encode_pcm16_to_pcmu

        pcm = self._pcm_trim_160(pcm_live)
        if self.codec == "pcmu":
            return encode_pcm16_to_pcmu(pcm)
        return encode_pcm16_to_pcma(pcm)

    @staticmethod
    def _silence_payload(codec: str) -> bytes:
        if codec == "pcma":
            return bytes([0xD5]) * SAMPLES_PER_PACKET
        return bytes([0xFF]) * SAMPLES_PER_PACKET

    # ---------- lifecycle ----------
    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _RtpProto(self),
            local_addr=(self.local_ip, self.local_port),
            family=socket.AF_INET,
        )
        self._transport = transport
        self._media_started_mono = time.monotonic()
        if self._record_path:
            try:
                os.makedirs(os.path.dirname(self._record_path) or ".", exist_ok=True)
                self._wav = wave.open(self._record_path, "wb")
                self._wav.setnchannels(2 if self._record_duplex else 1)
                self._wav.setsampwidth(2)
                self._wav.setframerate(CLOCK_RATE)
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not open WAV %s: %s", self._record_path, exc)
                self._wav = None

    async def stop(self) -> None:
        self._stop.set()
        if self._send_task:
            self._send_task.cancel()
            try:
                await self._send_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._transport:
            self._transport.close()
            self._transport = None
        if self._wav:
            try:
                self._wav.close()
            except Exception:  # noqa: BLE001
                pass
            self._wav = None

    async def run_for(self, duration_s: float) -> None:
        self._send_task = asyncio.create_task(self._send_loop())
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=duration_s)
        except asyncio.TimeoutError:
            pass
        await self.stop()

    def request_stop(self) -> None:
        self._stop.set()

    def start_sending(self) -> asyncio.Task:
        """Start the audio send loop as a background task.

        Use this when you want full manual control over when to stop (e.g.
        the IVR scenario which drives DTMF via :meth:`enqueue_dtmf` while
        the loop runs).
        """
        if self._send_task and not self._send_task.done():
            return self._send_task
        self._send_task = asyncio.create_task(self._send_loop())
        return self._send_task

    # ---------- DTMF ----------
    def enqueue_dtmf(self, digits: str, digit_duration_ms: int = 160,
                     gap_ms: int = 80, volume: int = 10) -> None:
        """Queue a sequence of DTMF digits. Call any time the stream is up."""
        packets_per_digit = max(1, digit_duration_ms // 20)
        gap_packets = max(0, gap_ms // 20)
        for d in digits:
            ev = DTMF_DIGIT_TO_EVENT.get(d)
            if ev is None:
                log.warning("Skipping non-DTMF char %r", d)
                continue
            self._dtmf_queue.append(
                _DtmfStep(event=ev, duration_packets=packets_per_digit, volume=volume)
            )
            for _ in range(gap_packets):
                # use event=-1 to mean "audio packet" / silence gap
                self._dtmf_queue.append(_DtmfStep(event=-1, duration_packets=1))

    @property
    def sent_dtmf(self) -> List[Dict]:
        return [{"t": t, "digit": d} for t, d in self._sent_dtmf]

    @property
    def received_dtmf(self) -> List[Dict]:
        return [
            {"t": e.t_first_seen, "digit": e.digit, "duration_ms": e.duration_ms}
            for e in self._received_dtmf
        ]

    @property
    def energy_log(self) -> List[Dict]:
        return [{"t": s.t, "rms": s.rms} for s in self._energy_log]

    async def wait_for_silence(self, min_silence_s: float = 0.6,
                               timeout_s: float = 5.0) -> bool:
        """Block until inbound RTP energy stays below threshold for min_silence_s.

        Returns True if silence was detected, False on timeout.
        """
        deadline = time.monotonic() + timeout_s
        while True:
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.05)
            now = time.monotonic()
            recent = [s for s in self._energy_log if s.t >= now - min_silence_s]
            if len(recent) >= 3:
                if all(s.rms < self._silence_threshold_rms for s in recent):
                    return True

    async def wait_for_audio_onset(
        self, threshold_factor: float = 1.0,
        min_active_s: float = 0.15, timeout_s: float = 5.0,
        from_t: Optional[float] = None,
    ) -> Optional[float]:
        """Block until inbound RTP energy stays *above* threshold for min_active_s.

        Returns the monotonic timestamp at which onset began, or None if the
        timeout elapsed without detecting sustained audio.
        """
        deadline = time.monotonic() + timeout_s
        threshold = self._silence_threshold_rms * threshold_factor
        from_t = from_t if from_t is not None else time.monotonic()
        while True:
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(0.05)
            now = time.monotonic()
            window = [s for s in self._energy_log
                      if from_t <= s.t <= now]
            if not window:
                continue
            # Find first sustained run of "loud" samples lasting min_active_s
            run_start: Optional[float] = None
            for s in window:
                if s.rms >= threshold:
                    if run_start is None:
                        run_start = s.t
                    elif s.t - run_start >= min_active_s:
                        return run_start
                else:
                    run_start = None

    def audio_metrics(
        self, start_t: float, end_t: float
    ) -> Dict[str, Optional[float]]:
        """Compute per-window audio metrics (used by plan executor).

        Returns a dict with:
          rms_avg, rms_max, rms_min, samples,
          silence_ratio (fraction of frames below threshold),
          active_ratio (1 - silence_ratio),
          onset_t (first sustained loud frame, monotonic, or None),
          offset_t (last sustained loud frame),
          dropout_count (count of >=200ms silence gaps inside the active span),
          peak_rms / clip_ratio (proxy for clipping based on RMS near 30000).
        """
        if end_t <= start_t:
            end_t = start_t + 0.001
        samples = [(s.t, s.rms) for s in self._energy_log
                   if start_t <= s.t <= end_t]
        if not samples:
            return {
                "samples": 0, "rms_avg": None, "rms_max": None,
                "rms_min": None, "silence_ratio": None,
                "active_ratio": None, "onset_t": None, "offset_t": None,
                "dropout_count": 0, "clip_ratio": 0.0,
                "duration_s": end_t - start_t,
            }
        rms_values = [r for _, r in samples]
        threshold = self._silence_threshold_rms
        silent = sum(1 for r in rms_values if r < threshold)
        active = len(rms_values) - silent
        silence_ratio = silent / len(rms_values)
        active_ratio = active / len(rms_values)
        onset_t: Optional[float] = None
        offset_t: Optional[float] = None
        for t, r in samples:
            if r >= threshold:
                onset_t = t
                break
        for t, r in reversed(samples):
            if r >= threshold:
                offset_t = t
                break
        # dropouts: consecutive runs of silence >= 0.2s in the active span
        dropouts = 0
        if onset_t is not None and offset_t is not None and offset_t > onset_t:
            run_start: Optional[float] = None
            for t, r in samples:
                if t < onset_t or t > offset_t:
                    continue
                if r < threshold:
                    if run_start is None:
                        run_start = t
                    elif t - run_start >= 0.2:
                        dropouts += 1
                        run_start = None
                else:
                    run_start = None
        peak = max(rms_values)
        clip_ratio = sum(1 for r in rms_values if r >= 28000) / len(rms_values)
        return {
            "samples": len(rms_values),
            "rms_avg": sum(rms_values) / len(rms_values),
            "rms_max": peak,
            "rms_min": min(rms_values),
            "silence_ratio": silence_ratio,
            "active_ratio": active_ratio,
            "onset_t": onset_t,
            "offset_t": offset_t,
            "dropout_count": dropouts,
            "clip_ratio": clip_ratio,
            "duration_s": end_t - start_t,
        }

    def received_dtmf_since(self, t0: float) -> List[Dict]:
        """Return inbound DTMF events whose first packet arrived at/after t0."""
        return [
            {"t": e.t_first_seen, "digit": e.digit, "duration_ms": e.duration_ms}
            for e in self._received_dtmf
            if e.t_first_seen >= t0
        ]

    async def _pull_mic_pcm160(self) -> List[int]:
        if self._mic_reader is None:
            return list(self._silence_pcm_160)
        return await getattr(self._mic_reader, "next_frame_or_silence")()

    # ---------- send loop ----------
    async def _send_loop(self) -> None:
        if not self._transport:
            return
        next_send = time.monotonic()
        # active dtmf state
        active: Optional[_DtmfStep] = None
        active_remaining = 0
        active_start_ts: int = 0
        active_packets_sent = 0
        end_packets_remaining = 0  # extra E-bit packets for redundancy

        while not self._stop.is_set():
            now = time.monotonic()
            if now < next_send:
                try:
                    await asyncio.sleep(next_send - now)
                except asyncio.CancelledError:
                    break

            pcm_mic = await self._pull_mic_pcm160()
            pcm_mic = self._limit_mic_tx_pcm(list(pcm_mic), self._mic_peak_abs)

            duplex_tx_pcm_record = list(self._silence_pcm_160)

            # decide what to send for this 20ms slot
            if active is None and self._dtmf_queue:
                step = self._dtmf_queue.popleft()
                if step.event >= 0:
                    active = step
                    active_remaining = step.duration_packets
                    active_packets_sent = 0
                    active_start_ts = self._timestamp
                    end_packets_remaining = 3
                    self._sent_dtmf.append(
                        (time.monotonic(), DTMF_EVENT_TO_DIGIT.get(step.event, "?"))
                    )

            try:
                if active is not None:
                    # In the middle of a DTMF digit
                    is_first = active_packets_sent == 0
                    duration_units = SAMPLES_PER_PACKET * (active_packets_sent + 1)
                    is_last = active_remaining == 1
                    pkt = self._encode_dtmf(
                        event=active.event,
                        volume=active.volume,
                        duration=min(duration_units, 0xFFFF),
                        end=is_last,
                        marker=is_first,
                        timestamp=active_start_ts,
                    )
                    self._transport.sendto(pkt, (self.remote_ip, self.remote_port))
                    self._packets_sent += 1
                    active_packets_sent += 1
                    active_remaining -= 1
                    if active_remaining <= 0:
                        # send a couple of redundant end packets
                        for _ in range(end_packets_remaining):
                            try:
                                self._transport.sendto(
                                    self._encode_dtmf(
                                        event=active.event,
                                        volume=active.volume,
                                        duration=min(duration_units, 0xFFFF),
                                        end=True,
                                        marker=False,
                                        timestamp=active_start_ts,
                                    ),
                                    (self.remote_ip, self.remote_port),
                                )
                                self._packets_sent += 1
                            except OSError:
                                pass
                        active = None
                        end_packets_remaining = 0
                else:
                    body = (
                        self._body_for_tx_tick(pcm_mic)
                        if self._mic_reader is not None
                        else self._body_for_tx_tick()
                    )
                    pkt = self._rtp_audio_packet(body)
                    self._transport.sendto(pkt, (self.remote_ip, self.remote_port))
                    self._packets_sent += 1
                    if self._mic_reader is not None:
                        self._accumulate_tx_energy(pcm_mic)
                    duplex_tx_pcm_record = self._pcm_decoded_from_tx_body(body)
            except OSError as exc:
                log.debug("RTP send failed: %s", exc)

            if self._record_duplex and self._wav:
                self._wav_write_duplex_tick(duplex_tx_pcm_record)

            self._seq = (self._seq + 1) & 0xFFFF
            self._timestamp = (self._timestamp + SAMPLES_PER_PACKET) & 0xFFFFFFFF
            next_send += PACKET_INTERVAL_S


    def _encode_dtmf(self, event: int, volume: int, duration: int,
                     end: bool, marker: bool, timestamp: int) -> bytes:
        first = 0x80
        second = self.dtmf_pt & 0x7F
        if marker:
            second |= 0x80
        header = struct.pack("!BBHII", first, second, self._seq, timestamp, self._ssrc)
        e_bit = 0x80 if end else 0
        body = struct.pack("!BBH", event & 0xFF, e_bit | (volume & 0x3F), duration & 0xFFFF)
        return header + body

    # ---------- receive ----------
    def _on_rtp(self, data: bytes, addr: Tuple[str, int], arrival: float) -> None:
        if len(data) < 12:
            return
        try:
            first, second, seq, ts, ssrc = struct.unpack("!BBHII", data[:12])
        except struct.error:
            return
        version = (first >> 6) & 0x3
        if version != 2:
            return
        cc = first & 0x0F
        offset = 12 + 4 * cc
        if len(data) < offset:
            return
        payload = data[offset:]
        pt = second & 0x7F

        self._packets_recv += 1
        if self._first_seq is None:
            self._first_seq = seq
            self._max_seq = seq
        else:
            assert self._max_seq is not None
            if seq < self._max_seq and (self._max_seq - seq) > 32768:
                self._cycles += 1
                self._max_seq = seq
            elif seq > self._max_seq:
                self._max_seq = seq

        # Jitter
        transit = arrival - (ts / float(CLOCK_RATE))
        if self._last_transit is not None:
            d = abs(transit - self._last_transit)
            self._jitter += (d - self._jitter) / 16.0
        self._last_transit = transit
        self._last_rtp_ts = ts
        self._last_seq = seq

        if pt == self.dtmf_pt:
            self._handle_inbound_dtmf(payload, arrival)
            return

        # Audio: decode for energy + (optionally) mono WAV record
        if pt in (PT_PCMU, PT_PCMA) and payload:
            codec = "pcmu" if pt == PT_PCMU else "pcma"
            samples = decode_payload(codec, payload)
            trimmed = self._pcm_trim_160(samples)
            if trimmed:
                rms = sum(abs(s) for s in trimmed) / len(trimmed)
                self._energy_log.append(AudioEnergySample(arrival, rms))
                if len(self._energy_log) > 5000:
                    del self._energy_log[: len(self._energy_log) - 5000]
            shaping = self._wav is not None
            rec_inbound = (
                self._inbound_pcm_for_recording(trimmed) if shaping else trimmed
            )
            self._duplex_rx_160 = rec_inbound
            if self._wav and not self._record_duplex:
                try:
                    self._wav.writeframes(_pcm16_to_bytes(rec_inbound))
                except Exception:  # noqa: BLE001
                    pass

    def _handle_inbound_dtmf(self, payload: bytes, arrival: float) -> None:
        if len(payload) < 4:
            return
        event = payload[0]
        flags = payload[1]
        duration = struct.unpack("!H", payload[2:4])[0]
        end = bool(flags & 0x80)
        digit = DTMF_EVENT_TO_DIGIT.get(event, "?")

        if self._rx_dtmf_in_flight is None or self._rx_dtmf_in_flight[0] != event:
            self._rx_dtmf_in_flight = (event, arrival, 1)
        else:
            ev, t0, n = self._rx_dtmf_in_flight
            self._rx_dtmf_in_flight = (ev, t0, n + 1)

        if end:
            ev, t0, _ = self._rx_dtmf_in_flight
            duration_ms = int(duration * 1000 / CLOCK_RATE)
            self._received_dtmf.append(
                DtmfRxEvent(digit=digit, t_first_seen=t0, duration_ms=duration_ms)
            )
            self._rx_dtmf_in_flight = None

    def media_quality_bundle(self) -> Dict[str, object]:
        """Inbound/outbound RTP audio diagnostics for JSON reports."""
        hi = time.monotonic()
        t0 = self._media_started_mono
        if t0 is None:
            if self._energy_log:
                t0 = self._energy_log[0].t
            else:
                t0 = hi - 0.001
        inbound_audio = self.audio_metrics(t0, hi)
        tx_rms_avg: Optional[float]
        if self._tx_rms_ticks > 0:
            tx_rms_avg = self._tx_rms_sum / self._tx_rms_ticks
        else:
            tx_rms_avg = None

        anomalies: List[str] = []

        recv = inbound_audio.get("samples") or 0
        if recv == 0:
            anomalies.append("no_inbound_energy_samples")

        pk_ratio = (
            float(self._packets_recv) / float(max(1, self._packets_sent))
            if self._packets_sent > 50
            else None
        )
        if pk_ratio is not None and pk_ratio < 0.03:
            anomalies.append("severe_inbound_underflow_vs_packets_sent_ratio")

        if self._packets_recv > 400 and inbound_audio.get("active_ratio"):
            ratio = inbound_audio["active_ratio"]
            if isinstance(ratio, (int, float)) and ratio < 0.02:
                anomalies.append("silent_inbound_stream_despite_packets")

        if self._mic_reader is not None:
            mf = getattr(self._mic_reader, "silence_fallbacks", 0)
            if isinstance(mf, int) and mf > 50:
                anomalies.append("microphone_queue_starvation")

        sr = inbound_audio.get("silence_ratio")
        if sr is None:
            duplex_warn = False
        else:
            duplex_warn = sr > 0.95

        asym = pk_ratio if pk_ratio else None

        return {
            "window_monotonic_span_s": hi - (t0 or hi),
            "inbound_from_remote": inbound_audio,
            "outbound_toward_remote": {
                "tx_rms_proxy_avg": tx_rms_avg,
                "live_microphone_encoded": bool(self._mic_reader),
            },
            "duplex_wav_hints": {
                "left_channel": "remote_received",
                "right_channel": "rtp_transmit_audio_decoded",
                "right_channel_note": (
                    "ST stereo R follows the G.711 payload bytes we sent (decoded to PCM); "
                    "not pre-ADC microphone tap when live mic feeds the encoder."
                ),
                "risk_high_silence_on_left_when_remote_talks_but_no_rx_udp": duplex_warn,
            },
            "packet_asymmetry_recv_over_sent_approx": asym,
            "anomalies_observed_here": anomalies,
        }

    def stats(self) -> Dict[str, object]:
        loss_ratio: Optional[float] = None
        expected: Optional[int] = None
        if self._first_seq is not None and self._max_seq is not None:
            expected = (self._cycles * 65536 + self._max_seq) - self._first_seq + 1
            if expected > 0:
                lost = max(0, expected - self._packets_recv)
                loss_ratio = lost / expected
        rms_values = [s.rms for s in self._energy_log]
        rms_max = max(rms_values) if rms_values else None
        rms_avg = (sum(rms_values) / len(rms_values)) if rms_values else None
        active_audio_ratio = None
        if rms_values:
            active_audio_ratio = (
                sum(1 for r in rms_values if r >= self._silence_threshold_rms)
                / len(rms_values)
            )
        return {
            "packets_sent": self._packets_sent,
            "packets_recv": self._packets_recv,
            "expected_recv": expected,
            "loss_ratio": loss_ratio,
            "jitter_ms": self._jitter * 1000.0 if self._packets_recv > 1 else None,
            "rms_avg": rms_avg,
            "rms_max": rms_max,
            "active_audio_ratio": active_audio_ratio,
            "wav_path": self._record_path if self._record_path else None,
            "recording": {
                "requested_path": self._record_path,
                "duplex_stereo_lr": self._record_duplex,
                "microphone_on_rtp_wire": self._mic_reader is not None,
                "wav_layout_hint": (
                    "stereo_L_remote_R_transmit_pcm_decoded_from_rtp"
                    if self._record_duplex
                    else "mono_remote_inbound_only"
                ),
                "inbound_record_gain": (
                    self._inbound_record_gain if self._record_path else None
                ),
            },
            "extended_media_quality": self.media_quality_bundle(),
            "sent_dtmf": self.sent_dtmf,
            "received_dtmf": self.received_dtmf,
        }


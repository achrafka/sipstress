"""Metric collection: counters, histograms, response code distribution."""
from __future__ import annotations

import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional


class Histogram:
    """Lightweight histogram. Keeps raw samples; computes percentiles on demand.

    For very long runs we cap retained samples by reservoir sampling.
    """

    __slots__ = ("_samples", "_count", "_sum", "_max", "_min", "_cap", "_lock")

    def __init__(self, cap: int = 200_000) -> None:
        self._samples: List[float] = []
        self._count = 0
        self._sum = 0.0
        self._max = 0.0
        self._min = float("inf")
        self._cap = cap
        self._lock = threading.Lock()

    def add(self, value: float) -> None:
        with self._lock:
            self._count += 1
            self._sum += value
            if value > self._max:
                self._max = value
            if value < self._min:
                self._min = value
            if len(self._samples) < self._cap:
                self._samples.append(value)
            else:
                # reservoir replacement
                idx = self._count % self._cap
                self._samples[idx] = value

    def percentile(self, p: float) -> Optional[float]:
        with self._lock:
            if not self._samples:
                return None
            s = sorted(self._samples)
            k = max(0, min(len(s) - 1, int(round(p / 100.0 * (len(s) - 1)))))
            return s[k]

    def snapshot(self) -> Dict[str, Optional[float]]:
        with self._lock:
            count = self._count
            if count == 0:
                return {"count": 0, "mean": None, "min": None, "max": None,
                        "p50": None, "p90": None, "p95": None, "p99": None}
            mean = self._sum / count
            s = sorted(self._samples)

            def pct(p: float) -> float:
                k = max(0, min(len(s) - 1, int(round(p / 100.0 * (len(s) - 1)))))
                return s[k]

            return {
                "count": count,
                "mean": mean,
                "min": self._min,
                "max": self._max,
                "p50": pct(50),
                "p90": pct(90),
                "p95": pct(95),
                "p99": pct(99),
            }


@dataclass
class CallEvent:
    """A noteworthy event recorded during a call (used for race detection)."""

    call_id: str
    director: str
    timestamp: float
    kind: str
    detail: str = ""


@dataclass
class Metrics:
    """Aggregated metrics for one director (target) under test."""

    director: str
    started_at: float = field(default_factory=time.time)

    # Counters
    calls_attempted: int = 0
    calls_succeeded: int = 0
    calls_failed: int = 0
    calls_timed_out: int = 0
    calls_inflight: int = 0

    # Transaction-level
    requests_sent: int = 0
    responses_recv: int = 0
    retransmissions: int = 0  # we count duplicate identical responses
    provisional_recv: int = 0
    final_recv: int = 0

    response_codes: Counter = field(default_factory=Counter)
    failure_reasons: Counter = field(default_factory=Counter)

    # Latencies (seconds)
    setup_latency: Histogram = field(default_factory=Histogram)   # INVITE -> first 1xx
    answer_latency: Histogram = field(default_factory=Histogram)  # INVITE -> 200 OK
    register_latency: Histogram = field(default_factory=Histogram)
    options_latency: Histogram = field(default_factory=Histogram)
    bye_latency: Histogram = field(default_factory=Histogram)

    # Media
    rtp_jitter_ms: Histogram = field(default_factory=Histogram)
    rtp_loss_ratio: Histogram = field(default_factory=Histogram)
    rtp_packets_sent: int = 0
    rtp_packets_recv: int = 0

    # CPS sampling
    cps_samples: List[float] = field(default_factory=list)
    cps_targets: List[float] = field(default_factory=list)

    # Race / anomaly events
    anomalies: List[CallEvent] = field(default_factory=list)

    # Per-call records (limited)
    call_records: List[Dict] = field(default_factory=list)
    _max_records: int = 5000

    def record_response_code(self, code: int) -> None:
        self.response_codes[code] += 1
        if 100 <= code < 200:
            self.provisional_recv += 1
        else:
            self.final_recv += 1

    def add_anomaly(self, kind: str, call_id: str, detail: str = "") -> None:
        self.anomalies.append(CallEvent(call_id, self.director, time.time(), kind, detail))

    def add_call_record(self, record: Dict) -> None:
        if len(self.call_records) < self._max_records:
            self.call_records.append(record)

    def cps_sample(self, target: float, actual: float) -> None:
        self.cps_targets.append(target)
        self.cps_samples.append(actual)

    def to_dict(self) -> Dict:
        out = {
            "director": self.director,
            "started_at": self.started_at,
            "duration_s": time.time() - self.started_at,
            "calls": {
                "attempted": self.calls_attempted,
                "succeeded": self.calls_succeeded,
                "failed": self.calls_failed,
                "timed_out": self.calls_timed_out,
                "inflight": self.calls_inflight,
                "success_ratio": (
                    self.calls_succeeded / self.calls_attempted
                    if self.calls_attempted else None
                ),
            },
            "sip": {
                "requests_sent": self.requests_sent,
                "responses_recv": self.responses_recv,
                "retransmissions": self.retransmissions,
                "provisional_recv": self.provisional_recv,
                "final_recv": self.final_recv,
                "response_codes": dict(self.response_codes),
                "failure_reasons": dict(self.failure_reasons),
            },
            "latency_ms": {
                "setup": _scale(self.setup_latency.snapshot(), 1000.0),
                "answer": _scale(self.answer_latency.snapshot(), 1000.0),
                "register": _scale(self.register_latency.snapshot(), 1000.0),
                "options": _scale(self.options_latency.snapshot(), 1000.0),
                "bye": _scale(self.bye_latency.snapshot(), 1000.0),
            },
            "media": {
                "packets_sent": self.rtp_packets_sent,
                "packets_recv": self.rtp_packets_recv,
                "jitter_ms": self.rtp_jitter_ms.snapshot(),
                "loss_ratio": self.rtp_loss_ratio.snapshot(),
            },
            "throughput": {
                "cps_target_avg": _avg(self.cps_targets),
                "cps_actual_avg": _avg(self.cps_samples),
                "cps_actual_max": max(self.cps_samples) if self.cps_samples else None,
            },
            "anomalies": [
                {"kind": e.kind, "call_id": e.call_id, "ts": e.timestamp, "detail": e.detail}
                for e in self.anomalies
            ],
            "call_records": self.call_records,
        }
        return out


def _scale(snap: Dict[str, Optional[float]], factor: float) -> Dict[str, Optional[float]]:
    return {k: (v * factor if isinstance(v, (int, float)) and k != "count" else v) for k, v in snap.items()}


def _avg(seq: List[float]) -> Optional[float]:
    if not seq:
        return None
    return sum(seq) / len(seq)

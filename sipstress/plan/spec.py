"""Data model for sipstress test plans.

A :class:`TestPlan` is a flat ordered list of :class:`StepSpec` describing
*one path* through an IVR / PV3 scenario. The path is what a real caller
would experience when navigating the menus with a specific set of choices.

The step types map directly onto PV3 compos. We keep the type set
deliberately small — most PV3 elements either degenerate to one of these
types (Switch / Dispatcher / SetVariable -> nothing observable from the
caller's side) or extend one of them (SayNumber / SaySentence / Speech ->
Play with a synthesized prompt).
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class StepType(str, enum.Enum):
    PLAY = "play"                # PV3: Play, SayNumber, SaySentence, SpeechSynthesis
    MENU = "menu"                # PV3: Menu (single-digit choice)
    GET_DIGITS = "get_digits"    # PV3: GetDigits, AudioPicker
    SEND_DTMF = "send_dtmf"      # PV3: SendDTMF (no prompt; just emit digits)
    DIAL = "dial"                # PV3: DialSimple/DialMulti/DialDirect/DialWaiting
    QUEUE = "queue"              # PV3: WaitingQueue / VirtualQueue
    RECORD = "record"            # PV3: Voice2Mail / AudioRecord*
    WAIT = "wait"                # plain timed wait (pause / think-time)
    SILENCE = "silence"          # explicit "wait for IVR to fall silent"
    ANSWER = "answer"            # PV3: Answer (expect 200 OK if not yet answered)
    HANGUP = "hangup"            # PV3: Hangup (expect call to end)
    NOTE = "note"                # comment / structural marker


class StepVerdict(str, enum.Enum):
    OK = "OK"
    WARN = "WARN"
    FAIL = "FAIL"
    SKIP = "SKIP"


@dataclass
class StepSpec:
    """Specification of one step in a test plan."""

    id: str
    type: StepType
    name: str = ""               # human-readable label
    description: str = ""

    # ---------- audio expectations ----------
    expect_prompt_within_s: float = 5.0      # max delay before sustained audio
    min_prompt_duration_s: float = 0.5       # prompt must be heard for at least this long
    expect_audible: bool = True              # if False, we don't require any audio
    max_duration_s: float = 30.0             # hard cap on this step's wall time
    silence_after_s: float = 0.0             # listen for this much silence after the step

    # Quality thresholds (defaults are conservative; runtime can override)
    min_rms_avg: float = 200.0               # tag low-volume prompt
    max_dropouts: int = 2                    # mid-prompt silence gaps before WARN

    # ---------- DTMF / input ----------
    send_digit: Optional[str] = None         # menu: a single digit
    send_digits: Optional[str] = None        # get_digits / send_dtmf: multi
    valid_digits: List[str] = field(default_factory=list)  # menu: allowed digits
    terminator: Optional[str] = None         # # or *
    expect_min_digits: int = 0
    interdigit_delay_ms: int = 80

    # ---------- dial / queue ----------
    expect_ringback: bool = False
    expect_answer: bool = False              # 200 OK during this step
    expect_inband_audio: bool = True         # the caller still hears MoH/etc.
    queue_max_wait_s: float = 0.0
    #: Human hint for reports only (sipstress cannot see the real B-leg from SIP).
    expected_transfer_to: Optional[str] = None

    # ---------- record ----------
    record_duration_s: float = 0.0

    # ---------- timing ----------
    wait_s: float = 0.0                      # for type=WAIT/SILENCE

    # ---------- branch (PV3 graph) ----------
    branch_taken: Optional[str] = None       # which branch (PV3 endpoint key)
    next_step_id: Optional[str] = None       # only for visualization

    extra: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "StepSpec":
        if "type" not in d:
            raise ValueError(f"step {d.get('id', '?')!r} is missing 'type'")
        try:
            stype = StepType(d["type"])
        except ValueError as exc:
            raise ValueError(
                f"unknown step type {d['type']!r} (allowed: "
                f"{', '.join(t.value for t in StepType)})"
            ) from exc
        out = cls(id=d.get("id") or d.get("name") or stype.value, type=stype)
        for k, v in d.items():
            if k == "type":
                continue
            if k.endswith("_s") and isinstance(v, str):
                # accept '500ms', '2s', etc.
                v = _parse_duration(v)
            if k == "interdigit_delay_ms" and isinstance(v, str):
                v = int(_parse_duration(v) * 1000)
            if hasattr(out, k):
                setattr(out, k, v)
            else:
                out.extra[k] = v
        return out


@dataclass
class TestPlan:
    """A complete plan to execute in one call."""

    name: str = "test"
    description: str = ""
    steps: List[StepSpec] = field(default_factory=list)

    # Globals (override per-step values when set)
    silence_threshold_rms: Optional[float] = None
    default_step_max_duration_s: Optional[float] = None

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TestPlan":
        plan = cls(
            name=d.get("name", "test"),
            description=d.get("description", ""),
        )
        if "silence_threshold_rms" in d:
            plan.silence_threshold_rms = float(d["silence_threshold_rms"])
        if "default_step_max_duration" in d:
            plan.default_step_max_duration_s = _parse_duration(
                d["default_step_max_duration"]
            )
        for raw in d.get("steps", []):
            plan.steps.append(StepSpec.from_dict(raw))
        if plan.default_step_max_duration_s is not None:
            for s in plan.steps:
                if s.max_duration_s == 30.0:  # default; let plan-wide override win
                    s.max_duration_s = plan.default_step_max_duration_s
        return plan


@dataclass
class StepResult:
    """Outcome of executing one step during a call."""

    step_id: str
    step_type: str
    name: str = ""
    verdict: StepVerdict = StepVerdict.OK
    started_t: float = 0.0
    ended_t: float = 0.0

    # Audio (computed from RtpStream.audio_metrics over the step window)
    rms_avg: Optional[float] = None
    rms_max: Optional[float] = None
    silence_ratio: Optional[float] = None
    active_ratio: Optional[float] = None
    onset_offset_s: Optional[float] = None  # delay between step start and prompt onset
    prompt_duration_s: Optional[float] = None
    dropout_count: int = 0
    clip_ratio: float = 0.0

    # DTMF
    dtmf_sent: List[str] = field(default_factory=list)
    dtmf_received: List[str] = field(default_factory=list)
    dtmf_emit_duration_s: Optional[float] = None

    # SIP-side observations
    sip_status_during_step: Optional[int] = None
    answered_during_step: bool = False
    cancelled_during_step: bool = False

    # Findings
    findings: List[str] = field(default_factory=list)
    recommendations: List[str] = field(default_factory=list)

    # Free-form
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def duration_s(self) -> float:
        return self.ended_t - self.started_t

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_id": self.step_id,
            "step_type": self.step_type,
            "name": self.name,
            "verdict": self.verdict.value,
            "started_t": self.started_t,
            "ended_t": self.ended_t,
            "duration_s": self.duration_s,
            "audio": {
                "rms_avg": self.rms_avg,
                "rms_max": self.rms_max,
                "silence_ratio": self.silence_ratio,
                "active_ratio": self.active_ratio,
                "onset_offset_s": self.onset_offset_s,
                "prompt_duration_s": self.prompt_duration_s,
                "dropout_count": self.dropout_count,
                "clip_ratio": self.clip_ratio,
            },
            "dtmf": {
                "sent": self.dtmf_sent,
                "received": self.dtmf_received,
                "emit_duration_s": self.dtmf_emit_duration_s,
            },
            "sip": {
                "status_during_step": self.sip_status_during_step,
                "answered_during_step": self.answered_during_step,
                "cancelled_during_step": self.cancelled_during_step,
            },
            "expected_transfer_to": self.extra.get("expected_transfer_to"),
            "findings": self.findings,
            "recommendations": self.recommendations,
            "extra": self.extra,
        }


# ---------------------------- helpers ----------------------------

def _parse_duration(s: Any) -> float:
    if isinstance(s, (int, float)):
        return float(s)
    text = str(s).strip().lower()
    if not text:
        return 0.0
    # support '500ms', '2s', '5m'
    if text.endswith("ms"):
        return float(text[:-2]) / 1000.0
    if text.endswith("s"):
        return float(text[:-1])
    if text.endswith("m"):
        return float(text[:-1]) * 60.0
    return float(text)

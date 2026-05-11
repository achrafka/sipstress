"""CallContext: per-call helper that scenarios use to drive SIP exchanges.

This is the heart of the FSM. A CallContext owns:
    * a unique Call-ID
    * a From/To pair with tags
    * an asyncio queue with all incoming SIP messages for that Call-ID
    * a CSeq counter
    * a destination (host, port)
    * a reference to the Metrics aggregator and the SipTransport

Scenarios use methods like `send_request(...)`, `await_response(...)`,
`send_invite(...)`, `await_final(...)`, `send_bye(...)`, etc.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

from ..sip import auth as sip_auth
from ..sip import sdp as sip_sdp
from ..sip.message import (
    CRLF,
    SipMessage,
    build_request,
    build_response,
    parse_uri_host_port,
)
from ..sip.transport import SipTransport
from .metrics import Metrics

log = logging.getLogger("sipstress.call")


def _branch() -> str:
    return f"z9hG4bK-{secrets.token_hex(8)}"


def _tag() -> str:
    return secrets.token_hex(6)


def _call_id(host: str) -> str:
    return f"{secrets.token_hex(10)}@{host}"


@dataclass
class DirectorTarget:
    """A SIP server we are targeting under a given label."""

    label: str
    uri: str  # e.g. sip:director.example.com:5060
    host: str = ""
    port: int = 5060
    scheme: str = "sip"

    @classmethod
    def from_uri(cls, label: str, uri: str) -> "DirectorTarget":
        host, port, scheme = parse_uri_host_port(uri)
        return cls(label=label, uri=uri, host=host, port=port, scheme=scheme)


@dataclass
class IvrStep:
    """One step in an IVR navigation plan.

    Exactly one of `wait_s`, `digits`, or `wait_for_silence_s` should be set.
    """

    wait_s: Optional[float] = None
    digits: Optional[str] = None
    wait_for_silence_s: Optional[float] = None
    silence_min_s: float = 0.6
    note: str = ""


@dataclass
class CallConfig:
    """Per-call configuration computed by the runner."""

    director: DirectorTarget
    from_uri: str
    to_uri: str
    contact_user: str = "sipstress"
    auth_user: Optional[str] = None
    auth_pass: Optional[str] = None
    call_duration_s: float = 0.0
    max_call_duration_s: float = 120.0  # hard cap so we never hang
    media_enabled: bool = False
    codec: str = "pcmu"
    rtp_local_port: int = 0
    transaction_timeout_s: float = 32.0
    invite_timer_b_s: float = 32.0   # SIP Timer B
    non_invite_timer_f_s: float = 32.0
    invite_t1_s: float = 0.5  # initial retransmit
    extra_headers: Dict[str, str] = field(default_factory=dict)
    ivr_plan: List[IvrStep] = field(default_factory=list)
    ivr_post_play_s: float = 0.0   # how long to keep streaming media after the last step
    record_rtp_dir: Optional[str] = None
    record_duplex_wav: bool = False  # stereo: L remote, R = PCM decoded from transmitted RTP audio
    record_microphone: bool = False
    # Microphone linear scale into int16 before PCMU (~0.82 * this is default headroom; CLI --mic-gain)
    mic_gain: float = 1.0
    # WAV only: scales decoded inbound (remote→you) PCM; tames abrupt level after answer
    inbound_record_gain: float = 0.72
    detail_log: bool = False  # capture full per-call audit log
    test_plan: Optional[object] = None  # plan.spec.TestPlan when --plan/--pv3-scenario is used


class CallContext:
    """Per-call state and helpers used by scenarios."""

    def __init__(
        self,
        cfg: CallConfig,
        transport: SipTransport,
        metrics: Metrics,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        self.cfg = cfg
        self.transport = transport
        self.metrics = metrics
        self.loop = loop or asyncio.get_event_loop()

        bound_ip, self.local_port = transport.local_address
        # Use the advertised IP (the outbound interface) for SIP Via/Contact
        # and for SDP origin/connection. `local_ip` is what we publish in
        # SIP/SDP; the bind socket may still be 0.0.0.0.
        self.local_ip = transport.advertised_ip or bound_ip
        host_for_id = self.local_ip
        self.call_id = _call_id(host_for_id)
        self.from_tag = _tag()
        self.to_tag: Optional[str] = None
        self.cseq = 0

        self._queue: asyncio.Queue = transport.register(self.call_id)
        self._closed = False
        self._last_response_signature: Optional[Tuple[int, int, str]] = None

        # high-level state
        self.start_ts = time.monotonic()
        self.invite_sent_ts: Optional[float] = None
        self.first_provisional_ts: Optional[float] = None
        self.answered_ts: Optional[float] = None
        self.bye_sent_ts: Optional[float] = None
        self.terminated_ts: Optional[float] = None
        # captured at INVITE time; needed if we later CANCEL.
        self.invite_branch: Optional[str] = None
        self.invite_cseq_n: Optional[int] = None
        self.invite_ruri: Optional[str] = None
        self.success: bool = False
        self.failure_reason: Optional[str] = None
        self.final_status: Optional[int] = None
        self.remote_sdp: Optional[sip_sdp.SdpSession] = None

        self.events: List[Tuple[float, str, str]] = []
        # full per-call audit (only populated when cfg.detail_log is True)
        self.audit: List[Dict] = []
        self.record: Dict = {
            "call_id": self.call_id,
            "director": cfg.director.label,
            "to": cfg.to_uri,
            "from": cfg.from_uri,
        }

    # ---------- lifecycle ----------
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.transport.unregister(self.call_id)
        self.terminated_ts = time.monotonic()

    def event(self, kind: str, detail: str = "") -> None:
        self.events.append((time.monotonic(), kind, detail))

    def audit_log(self, kind: str, **fields) -> None:
        """Append a structured entry to the per-call audit log.

        Cheap if detail_log is disabled (we still keep events). Use this for
        SIP/RTP/IVR events that need full fidelity in the JSON report.
        """
        if not self.cfg.detail_log:
            return
        entry = {"t": time.monotonic() - self.start_ts, "kind": kind}
        entry.update(fields)
        self.audit.append(entry)

    # ---------- helpers ----------
    def _next_cseq(self) -> int:
        self.cseq += 1
        return self.cseq

    def _via_header(self, branch: str) -> str:
        return (
            f"SIP/2.0/UDP {self.local_ip}:{self.local_port}"
            f";branch={branch};rport"
        )

    def _from_header(self) -> str:
        return f"<{self.cfg.from_uri}>;tag={self.from_tag}"

    def _to_header(self) -> str:
        if self.to_tag:
            return f"<{self.cfg.to_uri}>;tag={self.to_tag}"
        return f"<{self.cfg.to_uri}>"

    def _contact_header(self) -> str:
        return f"<sip:{self.cfg.contact_user}@{self.local_ip}:{self.local_port}>"

    def _build(self, method: str, request_uri: Optional[str] = None) -> SipMessage:
        ruri = request_uri or self.cfg.to_uri
        msg = build_request(method, ruri)
        msg.add("Via", self._via_header(_branch()))
        msg.add("Max-Forwards", "70")
        msg.add("From", self._from_header())
        msg.add("To", self._to_header())
        msg.add("Call-ID", self.call_id)
        msg.add("CSeq", f"{self._next_cseq()} {method}")
        msg.add("Contact", self._contact_header())
        msg.add("User-Agent", "sipstress/0.1")
        for k, v in self.cfg.extra_headers.items():
            msg.add(k, v)
        return msg

    # ---------- send / receive ----------
    def send(self, msg: SipMessage) -> None:
        data = msg.encode()
        self.transport.send(data, (self.cfg.director.host, self.cfg.director.port))
        if msg.is_request:
            self.metrics.requests_sent += 1
        if self.cfg.detail_log:
            self.audit_log(
                "sip_send",
                method=msg.method if msg.is_request else None,
                status=msg.status_code if not msg.is_request else None,
                cseq=msg.cseq,
                via_branch=msg.via_branch,
                bytes=len(data),
            )

    async def recv(self, timeout: float) -> Optional[SipMessage]:
        try:
            msg, addr, raw = await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        if not msg.is_request:
            self.metrics.responses_recv += 1
            self.metrics.record_response_code(msg.status_code or 0)
            sig = (msg.status_code or 0, msg.cseq_number or 0, msg.cseq_method or "")
            if sig == self._last_response_signature:
                self.metrics.retransmissions += 1
            self._last_response_signature = sig
            # capture To-tag from the first response that has one
            if msg.to_tag and not self.to_tag:
                self.to_tag = msg.to_tag
        if self.cfg.detail_log:
            self.audit_log(
                "sip_recv",
                method=msg.method if msg.is_request else None,
                status=msg.status_code if not msg.is_request else None,
                cseq=msg.cseq,
                reason=msg.reason if not msg.is_request else None,
            )
        return msg

    async def await_response(
        self,
        cseq_number: int,
        method: str,
        timer_b: float,
        provisional_handler: Optional[Callable[[SipMessage], None]] = None,
    ) -> Optional[SipMessage]:
        """Wait for a final response to a given transaction, returning it."""
        deadline = time.monotonic() + timer_b
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            msg = await self.recv(timeout=remaining)
            if msg is None:
                return None
            if msg.is_request:
                # Could be a CANCEL/BYE arriving while we wait. Ignore here.
                continue
            if msg.cseq_number != cseq_number or msg.cseq_method != method:
                continue
            code = msg.status_code or 0
            if 100 <= code < 200:
                if not self.first_provisional_ts:
                    self.first_provisional_ts = time.monotonic()
                if provisional_handler:
                    try:
                        provisional_handler(msg)
                    except Exception:  # noqa: BLE001
                        log.exception("provisional handler failed")
                continue
            return msg

    # ---------- common SIP operations ----------
    async def send_options(self) -> Optional[SipMessage]:
        t0 = time.monotonic()
        msg = self._build("OPTIONS")
        msg.add("Accept", "application/sdp")
        cseq_n = self.cseq
        self.send(msg)
        resp = await self.await_response(cseq_n, "OPTIONS", self.cfg.non_invite_timer_f_s)
        latency = time.monotonic() - t0
        if resp is not None:
            self.metrics.options_latency.add(latency)
        return resp

    async def send_register(self) -> Optional[SipMessage]:
        t0 = time.monotonic()
        msg = self._build("REGISTER", request_uri=f"sip:{self.cfg.director.host}")
        msg.add("Expires", "3600")
        cseq_n = self.cseq
        self.send(msg)
        resp = await self.await_response(cseq_n, "REGISTER", self.cfg.non_invite_timer_f_s)
        if resp is None:
            return None
        if resp.status_code in (401, 407) and self.cfg.auth_user and self.cfg.auth_pass:
            challenge_hdr = (
                resp.get("WWW-Authenticate")
                if resp.status_code == 401
                else resp.get("Proxy-Authenticate")
            )
            if challenge_hdr:
                challenge = sip_auth.parse_challenge(challenge_hdr)
                # Resend with credentials
                msg2 = self._build("REGISTER", request_uri=f"sip:{self.cfg.director.host}")
                msg2.add("Expires", "3600")
                auth_uri = f"sip:{self.cfg.director.host}"
                hdr_name = (
                    "Authorization" if resp.status_code == 401 else "Proxy-Authorization"
                )
                msg2.add(
                    hdr_name,
                    sip_auth.build_response(
                        challenge,
                        self.cfg.auth_user,
                        self.cfg.auth_pass,
                        "REGISTER",
                        auth_uri,
                    ),
                )
                cseq_n2 = self.cseq
                self.send(msg2)
                resp = await self.await_response(
                    cseq_n2, "REGISTER", self.cfg.non_invite_timer_f_s
                )
        latency = time.monotonic() - t0
        if resp is not None:
            self.metrics.register_latency.add(latency)
        return resp

    async def send_invite(
        self,
        sdp_body: Optional[str] = None,
        on_provisional: Optional[Callable[[SipMessage], None]] = None,
    ) -> Optional[SipMessage]:
        """Send INVITE and await its final response.

        ``on_provisional`` may be a sync or async callable; it is invoked for
        every 1xx received during the transaction, allowing scenarios to
        react to early media (183 with SDP) without having to spin their own
        transaction loop.
        """
        msg = self._build("INVITE")
        if sdp_body:
            msg.add("Content-Type", "application/sdp")
            msg.body = sdp_body
        # Stash transaction identifiers so we can CANCEL later if needed.
        self.invite_branch = msg.via_branch
        self.invite_cseq_n = self.cseq
        self.invite_ruri = msg.request_uri
        self.invite_sent_ts = time.monotonic()
        cseq_n = self.cseq
        self.send(msg)

        def chained_provisional(prov: SipMessage) -> None:
            if prov.status_code in (180, 183) and not self.first_provisional_ts:
                self.first_provisional_ts = time.monotonic()
            if on_provisional is not None:
                try:
                    result = on_provisional(prov)
                except Exception:  # noqa: BLE001
                    log.exception("provisional handler failed")
                    return
                if asyncio.iscoroutine(result):
                    # Fire-and-forget: scenarios manage their own lifetimes.
                    asyncio.ensure_future(result)

        resp = await self.await_response(
            cseq_n,
            "INVITE",
            self.cfg.invite_timer_b_s,
            provisional_handler=chained_provisional,
        )
        # latencies
        if self.first_provisional_ts and self.invite_sent_ts:
            self.metrics.setup_latency.add(
                self.first_provisional_ts - self.invite_sent_ts
            )
        if resp is not None and resp.status_code and 200 <= resp.status_code < 300:
            self.answered_ts = time.monotonic()
            if self.invite_sent_ts:
                self.metrics.answer_latency.add(self.answered_ts - self.invite_sent_ts)
        return resp

    def send_cancel(self) -> bool:
        """Cancel the in-flight INVITE transaction (RFC 3261 §9).

        CANCEL re-uses the INVITE's branch and CSeq number, with method
        ``CANCEL`` and *no* To-tag (even if a 1xx already brought one in).
        Returns False if there is nothing to cancel.
        """
        if (
            self.invite_branch is None
            or self.invite_cseq_n is None
            or self.invite_ruri is None
        ):
            return False
        msg = build_request("CANCEL", self.invite_ruri)
        msg.add("Via", self._via_header(self.invite_branch))
        msg.add("Max-Forwards", "70")
        msg.add("From", self._from_header())
        msg.add("To", f"<{self.cfg.to_uri}>")  # NO to-tag for CANCEL
        msg.add("Call-ID", self.call_id)
        msg.add("CSeq", f"{self.invite_cseq_n} CANCEL")
        msg.add("User-Agent", "sipstress/0.1")
        for k, v in self.cfg.extra_headers.items():
            msg.add(k, v)
        self.send(msg)
        self.event("cancel_sent")
        if self.cfg.detail_log:
            self.audit_log("cancel_sent")
        return True

    def send_ack(self, response: SipMessage) -> None:
        """ACK a 2xx (or non-2xx) response to INVITE.

        For 2xx, ACK is a separate transaction (RFC 3261 §13.2.2.4):
        new branch, RURI from Contact in 200 OK, route set from Record-Route.
        For non-2xx, ACK is part of the INVITE transaction: same branch as INVITE.
        """
        is_2xx = bool(response.status_code and 200 <= response.status_code < 300)
        # capture To-tag if not yet captured
        if response.to_tag and not self.to_tag:
            self.to_tag = response.to_tag

        ruri = self.cfg.to_uri
        if is_2xx:
            contact = response.get("Contact", "")
            if contact:
                m = contact
                # extract URI between < > if present
                if "<" in m and ">" in m:
                    ruri = m[m.index("<") + 1 : m.index(">")]
                else:
                    ruri = m.split(";", 1)[0].strip()

        ack = build_request("ACK", ruri)
        if is_2xx:
            ack.add("Via", self._via_header(_branch()))
        else:
            via = response.get("Via", self._via_header(_branch()))
            ack.add("Via", via)
        ack.add("Max-Forwards", "70")
        ack.add("From", self._from_header())
        ack.add("To", self._to_header())
        ack.add("Call-ID", self.call_id)
        invite_cseq = response.cseq_number or self.cseq
        ack.add("CSeq", f"{invite_cseq} ACK")
        ack.add("Content-Length", "0")
        self.send(ack)

    async def send_bye(self) -> Optional[SipMessage]:
        msg = self._build("BYE")
        self.bye_sent_ts = time.monotonic()
        cseq_n = self.cseq
        t0 = time.monotonic()
        self.send(msg)
        resp = await self.await_response(cseq_n, "BYE", self.cfg.non_invite_timer_f_s)
        if resp is not None:
            self.metrics.bye_latency.add(time.monotonic() - t0)
        return resp

    # ---------- finalize ----------
    def mark_success(self) -> None:
        self.success = True
        self.metrics.calls_succeeded += 1

    def mark_failure(self, reason: str) -> None:
        self.success = False
        self.failure_reason = reason
        self.metrics.calls_failed += 1
        self.metrics.failure_reasons[reason] += 1

    def mark_timeout(self, where: str = "transaction") -> None:
        self.success = False
        self.failure_reason = f"timeout:{where}"
        self.metrics.calls_timed_out += 1
        self.metrics.failure_reasons[self.failure_reason] += 1

    def finalize(self) -> None:
        self.record.update(
            {
                "started_at": self.start_ts,
                "ended_at": self.terminated_ts or time.monotonic(),
                "duration_s": (self.terminated_ts or time.monotonic()) - self.start_ts,
                "success": self.success,
                "failure_reason": self.failure_reason,
                "final_status": self.final_status,
                "events": [
                    {"t": t - self.start_ts, "kind": k, "detail": d}
                    for t, k, d in self.events
                ],
            }
        )
        if self.cfg.detail_log and self.audit:
            self.record["audit"] = self.audit
        self.metrics.add_call_record(self.record)

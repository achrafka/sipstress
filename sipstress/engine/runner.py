"""Top-level test runner.

Responsibilities:
    * own a single SipTransport per director (we use one transport per local
      bind ip:port; directors share the same transport)
    * fan out scenarios to a CPS scheduler with concurrency cap
    * track per-director metrics
    * surface a callback for live dashboard refresh
    * cleanly shut down on Ctrl-C, run ramp-down etc.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

from ..scenarios.library import get as get_scenario
from ..sip.message import parse_uri_host_port
from ..sip.transport import SipTransport
from .call import CallConfig, CallContext, DirectorTarget, IvrStep
from .metrics import Metrics
from .scheduler import CpsPlan, CpsScheduler

log = logging.getLogger("sipstress.runner")


@dataclass
class RunnerConfig:
    directors: List[DirectorTarget]
    scenario: str
    from_uri: str
    to_uri: str
    contact_user: str = "sipstress"
    auth_user: Optional[str] = None
    auth_pass: Optional[str] = None

    total_calls: int = 0
    duration_s: float = 0.0
    cps: float = 1.0
    ramp_up_s: float = 0.0
    ramp_down_s: float = 0.0
    concurrency: int = 50
    call_delay_s: float = 0.0

    call_duration_s: float = 0.0
    max_call_duration_s: float = 120.0
    media_enabled: bool = False
    codec: str = "pcmu"
    ivr_plan: List[IvrStep] = field(default_factory=list)
    ivr_post_play_s: float = 0.0
    record_rtp_dir: Optional[str] = None
    record_duplex_wav: bool = False
    record_microphone: bool = False
    mic_gain: float = 1.0  # multiplied with built-in capture headroom (see microphone module)
    inbound_record_gain: float = 0.72  # applied only when writing WAV (inbound / duplex L)
    detail_log: bool = False
    test_plan: Optional[object] = None

    bind_ip: str = "0.0.0.0"
    bind_port: int = 0
    advertised_ip: Optional[str] = None
    rtp_port_min: int = 40000
    rtp_port_max: int = 41000
    trace_sip: bool = False

    transaction_timeout_s: float = 32.0
    invite_t1_s: float = 0.5
    invite_timer_b_s: float = 32.0
    non_invite_timer_f_s: float = 32.0

    register_on_start: bool = False
    extra_headers: Dict[str, str] = field(default_factory=dict)

    start_at_epoch: float = 0.0  # 0 = start immediately

    # If non-empty, Request-URI rotates by call index (round-robin). When
    # empty, ``to_uri`` is used for every attempt (legacy single-target).
    callees_rotate: List[str] = field(default_factory=list)


@dataclass
class RunnerStatus:
    started_at: float
    elapsed: float
    target_cps: float
    actual_cps: float
    inflight: int
    metrics: Dict[str, Metrics]


class Runner:
    def __init__(self, config: RunnerConfig) -> None:
        self.cfg = config
        primary_director_host = (
            config.directors[0].host if config.directors else None
        )
        self.transport = SipTransport(
            bind_ip=config.bind_ip,
            bind_port=config.bind_port,
            advertised_ip=config.advertised_ip,
            director_host=primary_director_host,
        )
        if config.trace_sip:
            self.transport.enable_trace(True)
        self.metrics: Dict[str, Metrics] = {
            d.label: Metrics(director=d.label) for d in config.directors
        }
        # CPS plan applies to the *combined* output across directors. We split
        # round-robin across directors when more than one is given.
        self._scheduler = CpsScheduler(
            CpsPlan(
                target_cps=config.cps,
                ramp_up_s=config.ramp_up_s,
                ramp_down_s=config.ramp_down_s,
                duration_s=config.duration_s,
                total_calls=config.total_calls,
            )
        )
        self._sema = asyncio.Semaphore(max(1, config.concurrency))
        self._active_tasks: Dict[asyncio.Task, CallContext] = {}
        self._closed = False
        self._on_status: Optional[Callable[[RunnerStatus], None]] = None
        self._rtp_ports_used: set = set()

        # CPS sampling
        self._cps_sample_window_s = 1.0
        self._last_cps_sample_t = 0.0
        self._calls_at_last_sample = 0

        self._anomaly_lock = asyncio.Lock()
        self._call_id_seen: Dict[str, str] = {}  # call_id -> director label

    def set_status_callback(self, cb: Callable[[RunnerStatus], None]) -> None:
        self._on_status = cb

    # --------------- helpers ---------------
    def _alloc_rtp_port(self) -> int:
        # Find an unused even port in [min, max). We avoid pairs already issued.
        for _ in range(200):
            p = random.randrange(self.cfg.rtp_port_min, self.cfg.rtp_port_max, 2)
            if p in self._rtp_ports_used:
                continue
            self._rtp_ports_used.add(p)
            return p
        raise RuntimeError("Exhausted RTP port range")

    def _release_rtp_port(self, port: int) -> None:
        self._rtp_ports_used.discard(port)

    def _pick_director(self, n: int) -> DirectorTarget:
        return self.cfg.directors[n % len(self.cfg.directors)]

    # --------------- main loop ---------------
    async def run(self) -> RunnerStatus:
        await self.transport.start()
        try:
            self.transport.set_unmatched_handler(self._on_unmatched)

            # Pre-register if requested
            if self.cfg.register_on_start and self.cfg.auth_user and self.cfg.auth_pass:
                await self._preregister_all()

            # Wait until scheduled start
            if self.cfg.start_at_epoch > 0:
                wait = self.cfg.start_at_epoch - time.time()
                if wait > 0:
                    log.info("Sleeping %.1fs until scheduled start", wait)
                    await asyncio.sleep(wait)

            status_task = asyncio.create_task(self._status_loop())
            try:
                await self._produce_loop()
            finally:
                status_task.cancel()
                try:
                    await status_task
                except asyncio.CancelledError:
                    pass

            # Wait for in-flight calls to complete
            await self._drain()
        finally:
            self.transport.stop()

        return self._snapshot_status()

    async def _preregister_all(self) -> None:
        for d in self.cfg.directors:
            cfg = self._call_config_for(d, with_rtp=False, override_to=self.cfg.from_uri)
            ctx = CallContext(cfg, self.transport, self.metrics[d.label])
            try:
                ctx.event("preregister")
                resp = await ctx.send_register()
                if resp is None or resp.status_code is None or resp.status_code >= 300:
                    log.warning(
                        "REGISTER pre-flight to %s failed: status=%s",
                        d.label,
                        getattr(resp, "status_code", None),
                    )
                else:
                    log.info("REGISTER pre-flight to %s OK", d.label)
            finally:
                ctx.close()

    async def _produce_loop(self) -> None:
        scenario_fn = get_scenario(self.cfg.scenario)
        n = 0
        while True:
            try:
                await self._scheduler.acquire()
            except StopAsyncIteration:
                break
            await self._sema.acquire()
            call_index = n
            director = self._pick_director(n)
            n += 1
            cfg = self._call_config_for(
                director,
                with_rtp=self.cfg.media_enabled,
                call_index=call_index,
            )
            metrics = self.metrics[director.label]
            metrics.calls_attempted += 1
            metrics.calls_inflight += 1
            ctx = CallContext(cfg, self.transport, metrics)
            self._call_id_seen[ctx.call_id] = director.label
            task = asyncio.create_task(self._run_one(scenario_fn, ctx))
            self._active_tasks[task] = ctx
            task.add_done_callback(self._on_done)
            if self.cfg.call_delay_s > 0:
                await asyncio.sleep(self.cfg.call_delay_s)

    async def _run_one(self, scenario_fn, ctx: CallContext) -> None:
        try:
            # Hard cap so a chatty IVR (or stuck dialog) can never deadlock
            # the test. The scenario itself also tracks ivr deadlines, but
            # this is the belt-and-suspenders guard.
            cap = max(ctx.cfg.max_call_duration_s, 5.0)
            await asyncio.wait_for(scenario_fn(ctx), timeout=cap + 5.0)
        except asyncio.TimeoutError:
            ctx.mark_failure("max_call_duration_exceeded")
            ctx.event("max_call_duration_exceeded")
        except asyncio.CancelledError:
            ctx.mark_failure("cancelled")
            raise
        except Exception as exc:
            log.exception("Scenario raised: %s", exc)
            ctx.mark_failure(f"exception:{type(exc).__name__}")
        finally:
            ctx.finalize()
            ctx.metrics.calls_inflight -= 1
            if ctx.cfg.rtp_local_port:
                self._release_rtp_port(ctx.cfg.rtp_local_port)
            ctx.close()

    def _on_done(self, task: asyncio.Task) -> None:
        self._active_tasks.pop(task, None)
        self._sema.release()

    async def _drain(self) -> None:
        if not self._active_tasks:
            return
        log.info("Draining %d in-flight calls...", len(self._active_tasks))
        # Generous timeout: max possible call lifetime
        max_wait = (
            self.cfg.call_duration_s
            + self.cfg.invite_timer_b_s
            + self.cfg.non_invite_timer_f_s
            + 5
        )
        await asyncio.wait(list(self._active_tasks.keys()), timeout=max_wait)
        # Force cancel anything still hanging
        for t in list(self._active_tasks.keys()):
            if not t.done():
                t.cancel()
        if self._active_tasks:
            await asyncio.gather(
                *list(self._active_tasks.keys()), return_exceptions=True
            )

    async def _status_loop(self) -> None:
        prev_attempted = 0
        prev_t = time.monotonic()
        try:
            while True:
                await asyncio.sleep(0.5)
                t = time.monotonic()
                attempted_total = sum(m.calls_attempted for m in self.metrics.values())
                dt = max(t - prev_t, 1e-6)
                actual_cps = (attempted_total - prev_attempted) / dt
                prev_attempted, prev_t = attempted_total, t
                target_cps = self._scheduler.current_target_cps()
                # sample per director (proportional split)
                share = 1.0 / max(len(self.metrics), 1)
                for m in self.metrics.values():
                    m.cps_sample(target_cps * share, actual_cps * share)
                if self._on_status:
                    try:
                        self._on_status(self._snapshot_status())
                    except Exception:  # noqa: BLE001
                        log.exception("status callback failed")
        except asyncio.CancelledError:
            return

    def _snapshot_status(self) -> RunnerStatus:
        return RunnerStatus(
            started_at=self._scheduler._start,
            elapsed=self._scheduler.elapsed(),
            target_cps=self._scheduler.current_target_cps(),
            actual_cps=(
                self.metrics_total_attempted() / max(self._scheduler.elapsed(), 1e-3)
            ),
            inflight=sum(m.calls_inflight for m in self.metrics.values()),
            metrics=self.metrics,
        )

    def metrics_total_attempted(self) -> int:
        return sum(m.calls_attempted for m in self.metrics.values())

    def _call_config_for(
        self,
        director: DirectorTarget,
        with_rtp: bool,
        *,
        call_index: int = 0,
        override_to: Optional[str] = None,
    ) -> CallConfig:
        # IVR scenarios always need RTP
        needs_rtp = with_rtp or self.cfg.scenario == "ivr"
        rtp_port = self._alloc_rtp_port() if needs_rtp else 0
        if override_to:
            to_uri = self._render_uri(override_to, director)
        elif self.cfg.callees_rotate:
            tmpl = self.cfg.callees_rotate[
                call_index % len(self.cfg.callees_rotate)
            ]
            to_uri = self._render_uri(tmpl, director)
        else:
            to_uri = self._render_uri(self.cfg.to_uri, director)
        from_uri = self._render_uri(self.cfg.from_uri, director)
        return CallConfig(
            director=director,
            from_uri=from_uri,
            to_uri=to_uri,
            contact_user=self.cfg.contact_user,
            auth_user=self.cfg.auth_user,
            auth_pass=self.cfg.auth_pass,
            call_duration_s=self.cfg.call_duration_s,
            max_call_duration_s=self.cfg.max_call_duration_s,
            media_enabled=self.cfg.media_enabled,
            codec=self.cfg.codec,
            rtp_local_port=rtp_port,
            transaction_timeout_s=self.cfg.transaction_timeout_s,
            invite_t1_s=self.cfg.invite_t1_s,
            invite_timer_b_s=self.cfg.invite_timer_b_s,
            non_invite_timer_f_s=self.cfg.non_invite_timer_f_s,
            extra_headers=dict(self.cfg.extra_headers),
            ivr_plan=list(self.cfg.ivr_plan),
            ivr_post_play_s=self.cfg.ivr_post_play_s,
            record_rtp_dir=self.cfg.record_rtp_dir,
            record_duplex_wav=self.cfg.record_duplex_wav,
            record_microphone=self.cfg.record_microphone,
            mic_gain=self.cfg.mic_gain,
            inbound_record_gain=self.cfg.inbound_record_gain,
            detail_log=self.cfg.detail_log,
            test_plan=self.cfg.test_plan,
        )

    def _render_uri(self, template: str, director: DirectorTarget) -> str:
        return template.format(
            director_label=director.label,
            director_host=director.host,
            director_port=director.port,
            director_uri=director.uri,
        )

    async def _on_unmatched(self, msg, addr, raw):
        # Reply 481 to in-dialog requests we don't know; ignore stray responses.
        if msg.is_request and msg.method not in (None, "ACK"):
            from ..sip.message import build_response

            try:
                resp = build_response(msg, 481, "Call/Transaction Does Not Exist")
                self.transport.send(resp.encode(), addr)
            except Exception:  # noqa: BLE001
                pass
            # Anomaly: we received a request after the call had already ended
            cid = msg.call_id or ""
            if cid in self._call_id_seen:
                # call already finalized -> potential race
                label = self._call_id_seen[cid]
                self.metrics[label].add_anomaly(
                    "late_request_after_call_ended",
                    cid,
                    f"method={msg.method} from={addr}",
                )
        elif not msg.is_request:
            cid = msg.call_id or ""
            if cid in self._call_id_seen:
                label = self._call_id_seen[cid]
                self.metrics[label].add_anomaly(
                    "late_response_after_call_ended",
                    cid,
                    f"status={msg.status_code} cseq={msg.cseq}",
                )

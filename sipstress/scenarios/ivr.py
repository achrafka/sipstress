"""IVR navigation scenario with full early-media support.

Behaves like a human caller: dials a destination, listens to the IVR (no
matter whether the IVR plays the prompt over early media on a `183 Session
Progress` or only after a final `200 OK`), navigates via DTMF according to
the configured plan, then either ACK+BYE (if the IVR answered) or CANCEL
(if it stayed in early media until the end of the plan).

A hard ``max_call_duration_s`` cap guarantees we never get stuck if the IVR
loops forever or never replies. The plan is a list of :class:`IvrStep`:

* ``wait_s``: pure timed wait (think-time)
* ``digits``: send these DTMF digits via RFC 4733 telephone-event
* ``wait_for_silence_s``: wait until inbound RTP energy stays below the
  silence threshold for ``silence_min_s`` (or until the timeout elapses,
  whichever comes first)

After all steps, we keep media open for ``ivr_post_play_s`` extra seconds.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from ..engine.call import CallContext
from ..media.rtp import RtpStream
from ..plan.executor import PlanExecutor
from ..plan.spec import StepResult, StepVerdict, TestPlan
from ..sip import sdp as sip_sdp
from ..sip.message import SipMessage
from .library import register

log = logging.getLogger("sipstress.ivr")


@dataclass
class _PlanResult:
    completed: bool
    steps: List[dict]


@register("ivr")
async def run(call: CallContext) -> None:
    """IVR scenario.

    Two modes:
      1. **Test plan mode** (``call.cfg.test_plan`` is a :class:`TestPlan`):
         drive the call through the structured plan and produce a per-step
         verdict report. This is the rich mode used with ``--plan`` or
         ``--pv3-scenario``.
      2. **Simple DTMF mode** (legacy): play through the flat
         ``call.cfg.ivr_plan`` of :class:`IvrStep`, useful for ad-hoc
         ``--dtmf-plan`` usage.
    """
    if call.cfg.rtp_local_port <= 0:
        call.mark_failure("no_rtp_port_allocated")
        return

    sdp_body = sip_sdp.build_offer(
        call.local_ip, call.cfg.rtp_local_port, call.cfg.codec
    )
    call.event("invite_send")
    call.audit_log("invite_send", to=call.cfg.to_uri)

    # Capture early media (first 18x with SDP) without blocking the INVITE
    # transaction. `send_invite` calls our handler for every 1xx.
    early_media_event = asyncio.Event()
    early_media_holder: List[Tuple[SipMessage, sip_sdp.SdpSession]] = []

    def on_provisional(prov: SipMessage) -> None:
        if prov.status_code in (180, 183) and prov.body and not early_media_event.is_set():
            parsed = sip_sdp.parse(prov.body)
            if parsed and parsed.medias:
                early_media_holder.append((prov, parsed))
                call.event("early_media", str(prov.status_code))
                call.audit_log(
                    "early_media",
                    status=prov.status_code,
                    remote_ip=parsed.connection_address,
                    remote_port=parsed.medias[0].port,
                )
                early_media_event.set()

    invite_task = asyncio.create_task(
        call.send_invite(sdp_body=sdp_body, on_provisional=on_provisional)
    )

    # Race: early media OR final response (or transaction timeout)
    em_wait = asyncio.create_task(early_media_event.wait())
    deadline = call.start_ts + max(call.cfg.max_call_duration_s, 5.0)

    try:
        first_done, _ = await asyncio.wait(
            {invite_task, em_wait},
            return_when=asyncio.FIRST_COMPLETED,
            timeout=max(0.1, deadline - time.monotonic()),
        )
    finally:
        if not em_wait.done():
            em_wait.cancel()

    rtp: Optional[RtpStream] = None
    plan: _PlanResult = _PlanResult(completed=False, steps=[])
    final_resp: Optional[SipMessage] = None
    used_early_media = False
    record_path: Optional[str] = None

    if call.cfg.record_rtp_dir:
        os.makedirs(call.cfg.record_rtp_dir, exist_ok=True)
        record_path = os.path.join(
            call.cfg.record_rtp_dir, f"{call.call_id.replace('@', '_')}.wav"
        )

    try:
        if early_media_event.is_set() and not invite_task.done():
            # ----- Early-media branch: 183 with SDP, INVITE still pending -----
            used_early_media = True
            _, remote_sdp = early_media_holder[0]
            rtp = await _start_rtp(call, remote_sdp, record_path)
            plan = await _drive_call(call, rtp, deadline)

            if not invite_task.done():
                # Plan finished before any 200. Cancel the INVITE.
                call.audit_log("cancel_invite_after_plan")
                call.send_cancel()
                try:
                    final_resp = await asyncio.wait_for(
                        invite_task,
                        timeout=min(10.0, max(1.0, deadline - time.monotonic())),
                    )
                except asyncio.TimeoutError:
                    final_resp = None
            else:
                final_resp = invite_task.result()
        else:
            # ----- Standard branch: wait for the INVITE to finalize -----
            try:
                final_resp = await asyncio.wait_for(
                    invite_task,
                    timeout=max(0.1, deadline - time.monotonic()),
                )
            except asyncio.TimeoutError:
                final_resp = None

        if final_resp is None:
            # Timer B / max-call-duration expired without any final response.
            if not invite_task.done():
                invite_task.cancel()
            if rtp is None:
                call.mark_timeout("invite")
            else:
                # We did get early media but never a final answer. The CANCEL
                # we already sent should have produced a 487; if not, the
                # transaction layer timed out -> classify as timeout.
                call.mark_timeout("invite_no_final_after_cancel")
            return

        call.final_status = final_resp.status_code
        code = final_resp.status_code or 0

        if 200 <= code < 300:
            # The IVR (or downstream) accepted the call. ACK and continue.
            call.send_ack(final_resp)
            call.event("call_established")
            call.audit_log("call_established", status=code)

            answer_sdp = sip_sdp.parse(final_resp.body or "") if final_resp.body else None
            call.remote_sdp = answer_sdp

            if rtp is None:
                # No early media before; start RTP now using the 200's SDP.
                if not answer_sdp or not answer_sdp.medias:
                    call.mark_failure("no_remote_sdp_in_200")
                    await _safe_bye(call)
                    return
                rtp = await _start_rtp(call, answer_sdp, record_path)
                plan = await _drive_call(call, rtp, deadline)
            # If RTP was already running on early media, the plan already ran.

            bye_resp = await _safe_bye(call)
            if bye_resp is None:
                call.mark_failure("bye_timeout")
                return
            if bye_resp.status_code and 200 <= bye_resp.status_code < 300:
                if plan.completed:
                    call.mark_success()
                    call.event("ivr_completed_ok")
                else:
                    call.mark_failure("ivr_plan_truncated")
            else:
                call.mark_failure(f"bye:{bye_resp.status_code}")

        elif code == 487 and used_early_media and plan.completed:
            # Expected outcome: we cancelled after walking the IVR successfully.
            call.send_ack(final_resp)
            call.mark_success()
            call.event("ivr_cancelled_after_completion")
        else:
            # 4xx / 5xx (including 487 when the plan didn't complete).
            call.send_ack(final_resp)
            if used_early_media and plan.completed:
                # We finished the navigation but the IVR rejected at the end.
                call.mark_failure(f"invite_after_ivr:{code}")
            else:
                call.mark_failure(f"invite:{code}")

    except Exception as exc:  # noqa: BLE001
        log.exception("ivr scenario crashed: %s", exc)
        call.mark_failure(f"exception:{type(exc).__name__}")
    finally:
        if rtp is not None:
            await _finalize_rtp(call, rtp, record_path, plan)


# ---------------------------- helpers ----------------------------


async def _start_rtp(
    call: CallContext,
    remote_sdp: sip_sdp.SdpSession,
    record_path: Optional[str],
) -> RtpStream:
    m = remote_sdp.medias[0]
    remote_ip = remote_sdp.connection_address or call.cfg.director.host
    remote_port = m.port
    call.audit_log(
        "media_path",
        remote_ip=remote_ip,
        remote_port=remote_port,
        local_ip=call.local_ip,
        local_port=call.cfg.rtp_local_port,
        codec=call.cfg.codec,
    )
    rtp = RtpStream(
        local_ip=call.local_ip,
        local_port=call.cfg.rtp_local_port,
        remote_ip=remote_ip,
        remote_port=remote_port,
        codec=call.cfg.codec,
        record_wav_path=record_path,
        inbound_record_gain=float(call.cfg.inbound_record_gain),
    )
    await rtp.start()
    rtp.start_sending()
    call.event("rtp_started", f"{remote_ip}:{remote_port}")
    return rtp


async def _drive_call(
    call: CallContext, rtp: RtpStream, deadline: float
) -> _PlanResult:
    """Choose between TestPlan executor and simple DTMF plan."""
    plan_obj = getattr(call.cfg, "test_plan", None)
    if isinstance(plan_obj, TestPlan):
        return await _run_test_plan(call, rtp, deadline, plan_obj)
    return await _run_dtmf_plan(call, rtp, deadline)


async def _run_test_plan(
    call: CallContext, rtp: RtpStream, deadline: float, plan: TestPlan
) -> _PlanResult:
    """Run a structured TestPlan through the PlanExecutor."""

    def on_step_start(step) -> None:
        call.event("plan_step_start", step.id)
        call.audit_log("plan_step_start", step_id=step.id,
                       step_type=step.type.value, name=step.name)

    def on_step_end(res: StepResult) -> None:
        call.event(f"plan_step_end:{res.verdict.value}", res.step_id)
        call.audit_log(
            "plan_step_end",
            step_id=res.step_id,
            verdict=res.verdict.value,
            findings=list(res.findings),
            rms_avg=res.rms_avg,
            onset_offset_s=res.onset_offset_s,
            prompt_duration_s=res.prompt_duration_s,
            dtmf_sent=list(res.dtmf_sent),
        )

    executor = PlanExecutor(
        plan=plan,
        rtp=rtp,
        deadline_t=deadline,
        on_step_start=on_step_start,
        on_step_end=on_step_end,
    )
    results = await executor.run()
    completed = bool(results) and all(
        r.verdict in (StepVerdict.OK, StepVerdict.WARN, StepVerdict.SKIP)
        for r in results
    )
    serialised = [r.to_dict() for r in results]
    call.record["plan"] = {
        "name": plan.name,
        "description": plan.description,
        "completed": completed,
        "steps": serialised,
        "verdict_counts": _verdict_counts(results),
    }
    if call.cfg.ivr_post_play_s > 0:
        tail = min(call.cfg.ivr_post_play_s, max(0, deadline - time.monotonic()))
        if tail > 0:
            await asyncio.sleep(tail)
    # Compatibility shim: also fill the legacy ivr structure
    return _PlanResult(
        completed=completed,
        steps=[
            {
                "step": i,
                "kind": r.step_type,
                "detail": ",".join(r.dtmf_sent) if r.dtmf_sent else "",
                "note": r.name,
                "t_start": r.started_t - call.start_ts,
                "t_end": r.ended_t - call.start_ts,
                "verdict": r.verdict.value,
                "findings": r.findings,
            }
            for i, r in enumerate(results)
        ],
    )


def _verdict_counts(results) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for r in results:
        counts[r.verdict.value] = counts.get(r.verdict.value, 0) + 1
    return counts


async def _run_dtmf_plan(
    call: CallContext, rtp: RtpStream, deadline: float
) -> _PlanResult:
    """Execute the legacy IvrStep plan with a hard wall-clock deadline."""
    steps: List[dict] = []
    plan_completed = False

    plan = call.cfg.ivr_plan
    for idx, step in enumerate(plan):
        if time.monotonic() >= deadline:
            call.audit_log("ivr_aborted_by_max_duration", step=idx)
            break

        step_kind, detail, t0 = "noop", "", time.monotonic()
        if step.digits is not None:
            step_kind = "dtmf"
            detail = step.digits
            rtp.enqueue_dtmf(step.digits)
            estimated = (len(step.digits) * (160 + 80)) / 1000.0 + 0.1
            estimated = min(estimated, max(0.5, deadline - time.monotonic()))
            if estimated > 0:
                await asyncio.sleep(estimated)
        elif step.wait_for_silence_s is not None:
            step_kind = "wait_silence"
            timeout = min(step.wait_for_silence_s, deadline - time.monotonic())
            if timeout < 0:
                timeout = 0
            silent = await rtp.wait_for_silence(
                min_silence_s=step.silence_min_s,
                timeout_s=timeout,
            )
            detail = "silent" if silent else "timeout"
        elif step.wait_s is not None:
            step_kind = "wait"
            detail = f"{step.wait_s:.2f}s"
            sleep_for = min(step.wait_s, max(0, deadline - time.monotonic()))
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)

        steps.append(
            {
                "step": idx,
                "kind": step_kind,
                "detail": detail,
                "note": step.note,
                "t_start": t0 - call.start_ts,
                "t_end": time.monotonic() - call.start_ts,
            }
        )
        call.audit_log(
            "ivr_step",
            step=idx,
            step_kind=step_kind,
            detail=detail,
            note=step.note,
        )
    else:
        plan_completed = True

    if call.cfg.ivr_post_play_s > 0:
        tail = min(call.cfg.ivr_post_play_s, max(0, deadline - time.monotonic()))
        if tail > 0:
            await asyncio.sleep(tail)

    return _PlanResult(completed=plan_completed, steps=steps)


async def _finalize_rtp(
    call: CallContext,
    rtp: RtpStream,
    record_path: Optional[str],
    plan: _PlanResult,
) -> None:
    await rtp.stop()
    stats = rtp.stats()

    call.metrics.rtp_packets_sent += int(stats.get("packets_sent") or 0)
    call.metrics.rtp_packets_recv += int(stats.get("packets_recv") or 0)
    if stats.get("jitter_ms") is not None:
        call.metrics.rtp_jitter_ms.add(stats["jitter_ms"])
    if stats.get("loss_ratio") is not None:
        call.metrics.rtp_loss_ratio.add(stats["loss_ratio"])

    call.record["rtp"] = stats
    call.record["ivr"] = {
        "plan_completed": plan.completed,
        "steps": plan.steps,
        "dtmf_sent": stats.get("sent_dtmf", []),
        "dtmf_received": stats.get("received_dtmf", []),
        "audio_active_ratio": stats.get("active_audio_ratio"),
        "audio_rms_avg": stats.get("rms_avg"),
        "audio_rms_max": stats.get("rms_max"),
        "wav_path": record_path if record_path and os.path.exists(record_path) else None,
    }


async def _safe_bye(call: CallContext) -> Optional[SipMessage]:
    call.event("bye_send")
    call.audit_log("bye_send")
    bye_resp = await call.send_bye()
    if bye_resp is not None:
        call.audit_log("bye_recv", status=bye_resp.status_code)
    return bye_resp

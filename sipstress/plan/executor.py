"""Plan executor: runs StepSpecs against a live RTP stream.

The executor knows nothing about SIP transactions — it only consumes/produces
RTP and DTMF. The PV3-aware scenario wires it up after the call has reached
either early-media or 200 OK.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, List, Optional

from ..media.rtp import RtpStream
from .spec import StepResult, StepSpec, StepType, StepVerdict, TestPlan

log = logging.getLogger("sipstress.plan")


class PlanExecutor:
    def __init__(
        self,
        plan: TestPlan,
        rtp: RtpStream,
        deadline_t: float,
        on_step_start: Optional[Callable[[StepSpec], None]] = None,
        on_step_end: Optional[Callable[[StepResult], None]] = None,
    ) -> None:
        self.plan = plan
        self.rtp = rtp
        self.deadline_t = deadline_t
        self._on_step_start = on_step_start
        self._on_step_end = on_step_end
        if plan.silence_threshold_rms is not None:
            rtp._silence_threshold_rms = plan.silence_threshold_rms  # noqa: SLF001
        self.results: List[StepResult] = []

    async def run(self) -> List[StepResult]:
        for step in self.plan.steps:
            if time.monotonic() >= self.deadline_t:
                # Mark this and all remaining as SKIP/FAIL
                self.results.append(_skip(step, "deadline_exceeded"))
                continue
            if self._on_step_start:
                try:
                    self._on_step_start(step)
                except Exception:  # noqa: BLE001
                    log.exception("on_step_start hook failed")

            t_start = time.monotonic()
            res = await self._dispatch(step, t_start)
            res.started_t = t_start
            if not res.ended_t:
                res.ended_t = time.monotonic()
            # pull audio metrics over [t_start, t_end]
            metrics = self.rtp.audio_metrics(t_start, res.ended_t)
            self._fold_audio_metrics(res, metrics, step)
            # dtmf received during step
            res.dtmf_received = [
                d["digit"] for d in self.rtp.received_dtmf_since(t_start)
                if d["t"] <= res.ended_t
            ]
            self._evaluate(step, res)
            self.results.append(res)
            if self._on_step_end:
                try:
                    self._on_step_end(res)
                except Exception:  # noqa: BLE001
                    log.exception("on_step_end hook failed")
        return self.results

    # ---------------- step dispatch ----------------
    async def _dispatch(self, step: StepSpec, t_start: float) -> StepResult:
        res = StepResult(
            step_id=step.id,
            step_type=step.type.value,
            name=step.name or step.id,
        )
        try:
            handler = {
                StepType.PLAY: self._do_play,
                StepType.MENU: self._do_menu,
                StepType.GET_DIGITS: self._do_get_digits,
                StepType.SEND_DTMF: self._do_send_dtmf,
                StepType.DIAL: self._do_dial,
                StepType.QUEUE: self._do_queue,
                StepType.RECORD: self._do_record,
                StepType.WAIT: self._do_wait,
                StepType.SILENCE: self._do_silence,
                StepType.ANSWER: self._do_answer,
                StepType.HANGUP: self._do_hangup,
                StepType.NOTE: self._do_noop,
            }.get(step.type, self._do_noop)
            await handler(step, res, t_start)
        except asyncio.CancelledError:
            res.verdict = StepVerdict.FAIL
            res.findings.append("cancelled")
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("step %s crashed: %s", step.id, exc)
            res.verdict = StepVerdict.FAIL
            res.findings.append(f"executor_exception:{type(exc).__name__}")
        return res

    # ---------------- handlers ----------------
    async def _do_play(self, step: StepSpec, res: StepResult, t_start: float) -> None:
        await self._wait_for_audible(step, res, t_start)
        # Continue listening up to max_duration to capture full prompt
        remaining = self._budget(step, t_start)
        if remaining > 0:
            await asyncio.sleep(min(remaining, step.max_duration_s))

    async def _do_menu(self, step: StepSpec, res: StepResult, t_start: float) -> None:
        await self._wait_for_audible(step, res, t_start)
        if step.send_digit:
            await self._send_dtmf(step.send_digit, step, res)
        # Brief listen for IVR reaction (if budget allows)
        post = min(self._budget(step, t_start), step.silence_after_s or 0.6)
        if post > 0:
            await asyncio.sleep(post)

    async def _do_get_digits(
        self, step: StepSpec, res: StepResult, t_start: float
    ) -> None:
        if step.expect_audible:
            await self._wait_for_audible(step, res, t_start)
        digits = step.send_digits or ""
        if step.terminator:
            digits = digits + step.terminator
        if digits:
            await self._send_dtmf(digits, step, res)
        post = min(self._budget(step, t_start), step.silence_after_s or 0.8)
        if post > 0:
            await asyncio.sleep(post)

    async def _do_send_dtmf(
        self, step: StepSpec, res: StepResult, t_start: float
    ) -> None:
        digits = step.send_digits or step.send_digit or ""
        if not digits:
            res.verdict = StepVerdict.WARN
            res.findings.append("send_dtmf_no_digits")
            return
        await self._send_dtmf(digits, step, res)

    async def _do_dial(
        self, step: StepSpec, res: StepResult, t_start: float
    ) -> None:
        if step.expected_transfer_to:
            res.extra["expected_transfer_to"] = step.expected_transfer_to
            hint = (
                "sipstress only sees your A-leg. To confirm this number was dialed "
                "on the PBX/B-leg, correlate this step's timestamps with "
                "FreeSWITCH 'show channels' / CDR, OpenSIPS logs, or Homer."
            )
            res.extra["transfer_verification_hint"] = hint
            res.recommendations.append(hint)
        # Listen for ringback/MoH/bridged agent audio for the configured duration.
        await self._listen(step, res, t_start)
        # Note: 'expect_answer' is the SIP scenario's responsibility.
        # We just record SIP state at end of step in the call wrapper.

    async def _do_queue(
        self, step: StepSpec, res: StepResult, t_start: float
    ) -> None:
        if step.expected_transfer_to:
            res.extra["expected_transfer_to"] = step.expected_transfer_to
            hint = (
                "sipstress only sees your A-leg. To confirm this number was dialed "
                "on the PBX/B-leg, correlate this step's timestamps with "
                "FreeSWITCH 'show channels' / CDR, OpenSIPS logs, or Homer."
            )
            res.extra["transfer_verification_hint"] = hint
            res.recommendations.append(hint)
        # Same observation model as dial: we listen for hold music / prompts.
        await self._listen(step, res, t_start)

    async def _do_record(
        self, step: StepSpec, res: StepResult, t_start: float
    ) -> None:
        # Wait for prompt then 'speak' (we just sit on the channel; the audio
        # we send is silence/comfort noise — the IVR's recorder will get that).
        await self._wait_for_audible(step, res, t_start, optional=True)
        dur = step.record_duration_s if step.record_duration_s > 0 else 3.0
        dur = min(dur, max(0.0, self._budget(step, t_start)))
        if dur > 0:
            await asyncio.sleep(dur)

    async def _do_wait(
        self, step: StepSpec, res: StepResult, t_start: float
    ) -> None:
        sleep_for = min(step.wait_s, max(0.0, self._budget(step, t_start)))
        if sleep_for > 0:
            await asyncio.sleep(sleep_for)

    async def _do_silence(
        self, step: StepSpec, res: StepResult, t_start: float
    ) -> None:
        timeout = step.wait_s if step.wait_s > 0 else step.max_duration_s
        timeout = min(timeout, max(0.0, self._budget(step, t_start)))
        ok = await self.rtp.wait_for_silence(min_silence_s=0.6, timeout_s=timeout)
        if not ok:
            res.verdict = StepVerdict.WARN
            res.findings.append("silence_not_detected")

    async def _do_answer(
        self, step: StepSpec, res: StepResult, t_start: float
    ) -> None:
        # The SIP layer handles 200/ACK; here we just record the marker.
        res.extra["expect_answer"] = True

    async def _do_hangup(
        self, step: StepSpec, res: StepResult, t_start: float
    ) -> None:
        res.extra["expect_hangup"] = True

    async def _do_noop(
        self, step: StepSpec, res: StepResult, t_start: float
    ) -> None:
        if step.wait_s > 0:
            await asyncio.sleep(min(step.wait_s, self._budget(step, t_start)))

    # ---------------- shared ----------------
    async def _wait_for_audible(
        self,
        step: StepSpec,
        res: StepResult,
        t_start: float,
        optional: bool = False,
    ) -> None:
        if not step.expect_audible:
            return
        timeout = min(step.expect_prompt_within_s, self._budget(step, t_start))
        if timeout <= 0:
            return
        onset = await self.rtp.wait_for_audio_onset(
            timeout_s=timeout, from_t=t_start
        )
        if onset is not None:
            res.onset_offset_s = onset - t_start

    async def _listen(
        self, step: StepSpec, res: StepResult, t_start: float
    ) -> None:
        timeout = min(step.max_duration_s, self._budget(step, t_start))
        if timeout > 0:
            await asyncio.sleep(timeout)

    async def _send_dtmf(
        self, digits: str, step: StepSpec, res: StepResult
    ) -> None:
        emit_start = time.monotonic()
        self.rtp.enqueue_dtmf(
            digits,
            digit_duration_ms=160,
            gap_ms=max(40, step.interdigit_delay_ms),
        )
        # rough estimate of how long the digits take to emit
        per_digit = 0.16 + max(0.04, step.interdigit_delay_ms / 1000.0)
        wait = min(per_digit * len(digits) + 0.1, max(0.0, self.deadline_t - time.monotonic()))
        if wait > 0:
            await asyncio.sleep(wait)
        res.dtmf_sent = list(digits)
        res.dtmf_emit_duration_s = time.monotonic() - emit_start

    def _budget(self, step: StepSpec, t_start: float) -> float:
        cap_step = max(0.0, step.max_duration_s - (time.monotonic() - t_start))
        cap_global = max(0.0, self.deadline_t - time.monotonic())
        return min(cap_step, cap_global)

    # ---------------- evaluation ----------------
    def _fold_audio_metrics(self, res: StepResult, m: dict, step: StepSpec) -> None:
        res.rms_avg = m.get("rms_avg")
        res.rms_max = m.get("rms_max")
        res.silence_ratio = m.get("silence_ratio")
        res.active_ratio = m.get("active_ratio")
        res.dropout_count = int(m.get("dropout_count") or 0)
        res.clip_ratio = float(m.get("clip_ratio") or 0.0)
        onset_t = m.get("onset_t")
        offset_t = m.get("offset_t")
        if onset_t is not None and offset_t is not None and offset_t >= onset_t:
            res.prompt_duration_s = offset_t - onset_t
            if res.onset_offset_s is None:
                res.onset_offset_s = onset_t - res.started_t

    def _evaluate(self, step: StepSpec, res: StepResult) -> None:
        # Audio expectations
        if step.expect_audible and step.type in (
            StepType.PLAY, StepType.MENU, StepType.GET_DIGITS,
        ):
            if res.onset_offset_s is None:
                res.verdict = StepVerdict.FAIL
                res.findings.append("no_prompt_detected")
                res.recommendations.append(
                    f"Compo {step.id} ({step.type.value}): no audio detected "
                    "within the expected window. Check the sound file is "
                    "provisioned for this scenario / language and that the IVR "
                    "actually entered this compo."
                )
            else:
                if res.onset_offset_s > step.expect_prompt_within_s:
                    self._raise(res, StepVerdict.WARN,
                                f"prompt_late_{res.onset_offset_s:.2f}s")
                if (res.prompt_duration_s is not None
                        and res.prompt_duration_s < step.min_prompt_duration_s):
                    self._raise(res, StepVerdict.WARN,
                                f"prompt_too_short_{res.prompt_duration_s:.2f}s",
                                rec=f"Compo {step.id}: prompt was shorter than "
                                    f"{step.min_prompt_duration_s:.1f}s — "
                                    "either the sound file is wrong or the "
                                    "compo bailed out early.")
        # Volume
        if (res.rms_avg is not None
                and step.expect_audible
                and step.type != StepType.WAIT
                and step.type != StepType.SILENCE
                and res.rms_avg < step.min_rms_avg
                and res.rms_max is not None
                and res.rms_max < step.min_rms_avg * 2):
            self._raise(res, StepVerdict.WARN, "low_volume",
                        rec=f"Compo {step.id}: very low average RMS "
                            f"({res.rms_avg:.0f}). Check codec mismatch, gain "
                            "settings on FreeSWITCH, or whether comfort noise "
                            "is being mistaken for audio.")
        # Dropouts
        if res.dropout_count > step.max_dropouts:
            self._raise(res, StepVerdict.WARN,
                        f"audio_dropouts_{res.dropout_count}",
                        rec=f"Compo {step.id}: {res.dropout_count} mid-prompt "
                            "silence gaps. Likely RTP packet loss or jitter "
                            "spikes — verify network QoS and the ptime setting.")
        if res.clip_ratio > 0.05:
            self._raise(res, StepVerdict.WARN, "audio_clipping",
                        rec=f"Compo {step.id}: audio peaks suggest clipping "
                            f"({res.clip_ratio:.1%} of frames at max RMS).")
        # DTMF
        if step.type == StepType.MENU and step.send_digit and not res.dtmf_sent:
            self._raise(res, StepVerdict.FAIL, "dtmf_not_sent")
        if step.type == StepType.GET_DIGITS and step.send_digits and not res.dtmf_sent:
            self._raise(res, StepVerdict.FAIL, "dtmf_not_sent")
        # Transfer / queue: almost no RTP during a long listen usually means dead path
        if step.type in (StepType.DIAL, StepType.QUEUE):
            self._evaluate_transfer_listen(step, res)

    def _evaluate_transfer_listen(self, step: StepSpec, res: StepResult) -> None:
        if step.max_duration_s < 15.0:
            return
        ar = res.active_ratio
        if (
            step.expect_inband_audio
            and ar is not None
            and ar < 0.02
            and (res.rms_max is None or res.rms_max < 250.0)
        ):
            self._raise(
                res,
                StepVerdict.WARN,
                "very_quiet_during_transfer",
                rec=(
                    f"During {step.id} almost no RTP energy was detected for "
                    f"{step.max_duration_s:.0f}s. If you expected ringing/MoH/bridge audio, "
                    "the transfer may never have progressed or SDP/RTP targets are "
                    "wrong. Check early vs answer SDP, firewall for RTP ports, "
                    "and PV3 Dial* compo gateways."
                ),
            )

    def _raise(self, res: StepResult, level: StepVerdict, finding: str,
               rec: Optional[str] = None) -> None:
        res.findings.append(finding)
        if rec:
            res.recommendations.append(rec)
        order = {StepVerdict.OK: 0, StepVerdict.SKIP: 0,
                 StepVerdict.WARN: 1, StepVerdict.FAIL: 2}
        if order[level] > order[res.verdict]:
            res.verdict = level


def _skip(step: StepSpec, reason: str) -> StepResult:
    return StepResult(
        step_id=step.id,
        step_type=step.type.value,
        name=step.name or step.id,
        verdict=StepVerdict.SKIP,
        findings=[reason],
    )

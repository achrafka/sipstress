"""INVITE with RTP like a normal SIP phone: early media before 200 OK.

Many services (IVR, DialWaiting, ringback) deliver audio on ``183 Session Progress``
(or ``180`` with SDP) *before* ``200 OK``. We start sending/receiving RTP as soon as
we see the first usable provisional SDP, keep the stream through ``200 OK`` (and
update remote host/port if the answer SDP changes), hold for ``--duration`` wall
clock from **first** RTP (or from answer if only 200 had SDP), then ``BYE``.

While media is held, we **poll in-dialog SIP** so **re-INVITE** / **UPDATE** with new
SDP (common after **mise en relation** / transfer on B2BUAs) refresh the RTP
destination and get a proper ``200 OK`` answers — missing this often leaves
symmetric-RTP paths sending only to the pre-bridge anchor (**quiet post-transfer
recording**).

If ``BYE`` arrives first, we acknowledge it and skip our own ``BYE``.

If ``BYE`` returns some non-``2xx`` codes (e.g. ``481``, ``513`` from certain B2BUAs),
we still mark the leg **successful** after a completed media timer so the report
reflects real-world behaviour where the call was fine but teardown signalling is quirky.

Recording helpers:

  * Mono inbound (default ``--record`` path): remote audio only on disk.
  * ``--record-duplex``: stereo WAV (L = received from far end, R = PCM decoded from
    whatever **G.711 RTP payload we transmit** each tick — what actually goes on the wire,
    not raw soundcard PCM before encoding). Use ``--microphone`` if you want live speech
    encoded into that RTP stream; without it, R reflects comfort-noise / silence payloads.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Dict, List, Optional

from ..engine.call import CallContext
from ..media.microphone import AsyncMicPCM8kMono
from ..media.rtp import RtpStream
from ..sip import sdp as sip_sdp
from ..sip.message import SipMessage, build_response
from .library import register

log = logging.getLogger("sipstress.invite_media")

_BYE_OK_EXTRA = frozenset({481, 408, 513})

SCENARIO_ID = "invite_media"


def _bye_teardown_acceptable(code: int | None) -> bool:
    if code is None:
        return False
    if 200 <= code < 300:
        return True
    return code in _BYE_OK_EXTRA


def _mono_event_relative(call: CallContext, kind: str) -> Optional[float]:
    for tmono, k, _d in call.events:
        if k == kind:
            return tmono - call.start_ts
    return None


def _scenario_findings(
    call: CallContext,
    stats: Dict,
    microphone_cm: Optional[AsyncMicPCM8kMono],
) -> List[str]:
    out: List[str] = []
    kinds = [k for _t, k, _d in call.events]
    if "invite_cancel_sent" in kinds and call.record.get("early_media_rtp"):
        out.append(
            "invite_cancel_after_rtp_started: Timer B or manual cancel while media path active"
        )
    if "rtp_remote_updated" in kinds:
        out.append(
            "rtp_remote_changed_mid_call: early-media anchor != 200 SDP target (relay/migration)."
        )
    kinds_detail = [d for _t, k, d in call.events if k == "rtp_remote_updated"]
    if any("invite sdp" in d or "update sdp" in d for d in kinds_detail):
        out.append(
            "mid_call_sdp_refresh: re-INVITE/UPDATE adjusted RTP remote (typical around transfer)."
        )

    aq = stats.get("extended_media_quality") or {}
    aq_an = aq.get("anomalies_observed_here") or []
    if isinstance(aq_an, list):
        out.extend(str(x) for x in aq_an)

    if (
        call.invite_sent_ts
        and call.answered_ts
        and (call.answered_ts - call.invite_sent_ts) < 0.35
        and call.record.get("early_media_rtp")
    ):
        out.append(
            "very_fast_answer_with_early_media: far end answered in <350 ms after INVITE "
            "(typical hosted IVR, not GSM ring-cycle)."
        )
    lr = stats.get("loss_ratio")
    if lr is not None and isinstance(lr, (int, float)) and lr > 0.02:
        out.append(f"elevated_recv_loss_approx={lr:.4f}")
    mic_fb = aq.get("outbound_toward_remote") or {}
    if mic_fb.get("live_microphone_encoded") and microphone_cm is not None:
        fb = microphone_cm.silence_fallbacks
        if fb > 0:
            out.append(f"microphone_silence_fallbacks={fb}")

    ps = stats.get("packets_sent")
    pr = stats.get("packets_recv")
    try:
        if ps and pr and int(ps) > 500 and float(pr) / float(ps) < 0.05:
            out.append("asymmetric_udp_heavy_tx_light_rx")
    except (ZeroDivisionError, TypeError):
        pass
    return out


async def _hold_media_with_in_dialog_sip(
    call: CallContext,
    state: Dict,
    lock: asyncio.Lock,
    sdp_answer_body: str,
    deadline_mono: float,
) -> bool:
    """Sleep until ``deadline_mono`` but consume in-dialog SIP so mid-call SDP works.

    After mise en relation / transfer, many B2BUAs send ``INVITE`` or ``UPDATE`` with
    new SDP (new ``c=``/``m=``). We must answer with ``200 OK`` and point RTP at the
    new remote — otherwise symmetric NAT / pinhole behaviour can leave this UA
    sending comfort noise to the *old* anchor while post-bridge audio uses another
    path (quiet or missing in the recording).

    Returns:
        True if the far end sent ``BYE`` (caller should not send its own ``BYE``).
    """
    while time.monotonic() < deadline_mono:
        rem = deadline_mono - time.monotonic()
        if rem <= 0:
            break
        msg = await call.recv(timeout=min(0.25, rem))
        if msg is None:
            continue
        if not msg.is_request:
            continue
        method = msg.method or ""
        if method == "ACK":
            continue
        if method == "BYE":
            bye_ok = build_response(msg, 200, "OK")
            call.send(bye_ok)
            call.event("bye_recv_before_local_teardown", msg.get("Reason", "") or "")
            return True

        if method in ("INVITE", "UPDATE") and (msg.body or "").strip():
            try:
                parsed = sip_sdp.parse(msg.body)
                async with lock:
                    rtp = state.get("rtp")
                    if parsed and parsed.medias and rtp is not None:
                        m0 = parsed.medias[0]
                        rip = parsed.connection_address or call.cfg.director.host
                        rport = m0.port
                        if rip != rtp.remote_ip or rport != rtp.remote_port:
                            rtp.remote_ip = rip
                            rtp.remote_port = rport
                            call.event(
                                "rtp_remote_updated",
                                f"{rip}:{rport} ({method.lower()} sdp)",
                            )
                ok = build_response(msg, 200, "OK")
                ok.add("Content-Type", "application/sdp")
                ok.body = sdp_answer_body
                call.send(ok)
            except Exception as exc:  # noqa: BLE001
                log.warning("mid-call %s handling failed: %s", method, exc)
            continue

        if method == "PRACK":
            call.send(build_response(msg, 200, "OK"))
            continue
        if method == "CANCEL":
            call.send(build_response(msg, 200, "OK"))
            continue
        if method in ("OPTIONS", "INFO", "NOTIFY"):
            opt = build_response(msg, 200, "OK")
            if method == "OPTIONS":
                opt.add(
                    "Allow",
                    "INVITE, ACK, CANCEL, BYE, OPTIONS, UPDATE, INFO, NOTIFY, PRACK",
                )
            call.send(opt)
            continue

        log.debug("invite_media: unhandled in-dialog %s — replying 200", method)
        call.send(build_response(msg, 200, "OK"))
    return False


@register(SCENARIO_ID)
async def run(call: CallContext) -> None:
    if call.cfg.rtp_local_port <= 0:
        call.mark_failure("no_rtp_port_allocated")
        return

    microphone_cm: Optional[AsyncMicPCM8kMono] = None

    try:
        if call.cfg.record_microphone:
            microphone_cm = AsyncMicPCM8kMono(linear_scale=float(call.cfg.mic_gain))
            await microphone_cm.__aenter__()

        duration = max(call.cfg.call_duration_s, 1.0)
        sdp_body = sip_sdp.build_offer(
            call.local_ip, call.cfg.rtp_local_port, call.cfg.codec
        )

        record_path: Optional[str] = None
        duplex_enabled = False
        if call.cfg.record_rtp_dir:
            os.makedirs(call.cfg.record_rtp_dir, exist_ok=True)
            safe = call.call_id.replace("@", "_").replace(":", "_")
            host = call.cfg.director.host
            duplex_enabled = bool(call.cfg.record_duplex_wav)
            if duplex_enabled:
                record_path = os.path.join(
                    call.cfg.record_rtp_dir, f"{safe}_{host}_duplex.wav"
                )
            else:
                record_path = os.path.join(
                    call.cfg.record_rtp_dir, f"{safe}_{host}.wav"
                )
            call.record["recording_wav"] = record_path
            call.record["recording"] = {
                "layout": (
                    "stereo_l_remote_r_rtp_tx_decoded"
                    if duplex_enabled
                    else "mono_remote_in_only"
                ),
                "duplex_requested": duplex_enabled,
                "microphone_on_wire": bool(microphone_cm),
                "inbound_record_gain": float(call.cfg.inbound_record_gain),
            }

        lock = asyncio.Lock()
        state: dict = {"rtp": None, "deadline": None}

        async def _attach_rtp_locked(sess: sip_sdp.SdpSession, reason: str) -> None:
            """Start RTP; caller must hold ``lock``."""
            if state["rtp"] is not None or not sess.medias:
                return
            m = sess.medias[0]
            remote_ip = sess.connection_address or call.cfg.director.host
            remote_port = m.port
            rtp = RtpStream(
                local_ip=call.local_ip,
                local_port=call.cfg.rtp_local_port,
                remote_ip=remote_ip,
                remote_port=remote_port,
                codec=call.cfg.codec,
                record_wav_path=record_path,
                record_duplex=duplex_enabled,
                microphone_reader=microphone_cm,
                inbound_record_gain=float(call.cfg.inbound_record_gain),
            )
            await rtp.start()
            rtp.start_sending()
            state["rtp"] = rtp
            state["deadline"] = time.monotonic() + duration
            call.event("rtp_started", f"{remote_ip}:{remote_port} ({reason})")
            if reason == "early_media":
                call.record["early_media_rtp"] = True

        async def on_provisional(prov: SipMessage) -> None:
            if prov.status_code not in (180, 183) or not prov.body:
                return
            try:
                parsed = sip_sdp.parse(prov.body)
            except Exception:  # noqa: BLE001
                return
            if not parsed or not parsed.medias:
                return
            try:
                async with lock:
                    await _attach_rtp_locked(parsed, "early_media")
            except Exception as exc:  # noqa: BLE001
                log.warning("early media RTP failed: %s", exc)
                call.event("early_media_rtp_error", str(exc))

        call.event("invite_send")
        resp = await call.send_invite(sdp_body=sdp_body, on_provisional=on_provisional)
        if resp is None:
            await _shutdown_rtp(state)
            try:
                if call.send_cancel():
                    call.event(
                        "invite_cancel_sent",
                        "timeout_waiting_for_invite_final",
                    )
            except Exception:  # noqa: BLE001
                pass
            call.mark_timeout("invite")
            return

        call.final_status = resp.status_code
        if not resp.status_code or resp.status_code >= 300:
            call.send_ack(resp)
            await _shutdown_rtp(state)
            call.mark_failure(f"invite:{resp.status_code}")
            return

        call.send_ack(resp)
        call.event("call_established")
        remote = sip_sdp.parse(resp.body or "")
        call.remote_sdp = remote

        async with lock:
            rtp = state["rtp"]
            if remote and remote.medias:
                m = remote.medias[0]
                rip = remote.connection_address or call.cfg.director.host
                rport = m.port
                if rtp is None:
                    try:
                        await _attach_rtp_locked(remote, "200_ok")
                    except Exception as exc:  # noqa: BLE001
                        log.warning("answer RTP failed: %s", exc)
                elif rip != rtp.remote_ip or rport != rtp.remote_port:
                    rtp.remote_ip = rip
                    rtp.remote_port = rport
                    call.event("rtp_remote_updated", f"{rip}:{rport}")
            elif rtp is None:
                state["deadline"] = time.monotonic() + duration

        remote_bye = False
        rtp = state["rtp"]
        if rtp is not None:
            deadline = state["deadline"]
            if deadline is not None:
                remote_bye = await _hold_media_with_in_dialog_sip(
                    call, state, lock, sdp_body, deadline
                )
            try:
                await rtp.stop()
                stats = rtp.stats()
                call.metrics.rtp_packets_sent += int(stats["packets_sent"])  # type: ignore[arg-type]
                call.metrics.rtp_packets_recv += int(stats["packets_recv"])  # type: ignore[arg-type]
                jm = stats["jitter_ms"]
                if jm is not None:
                    call.metrics.rtp_jitter_ms.add(float(jm))  # type: ignore[arg-type]
                lr = stats["loss_ratio"]
                if lr is not None:
                    call.metrics.rtp_loss_ratio.add(float(lr))  # type: ignore[arg-type]
                call.record["rtp"] = stats
                timings = {
                    "invite_sent_s": _mono_event_relative(call, "invite_send"),
                    "rtp_started_s": _mono_event_relative(call, "rtp_started"),
                    "call_established_s": _mono_event_relative(call, "call_established"),
                }
                if call.invite_sent_ts and call.answered_ts:
                    timings["sip_invite_to_200_wall_s"] = (
                        call.answered_ts - call.invite_sent_ts
                    )

                anomalies = _scenario_findings(call, stats, microphone_cm)
                call.record["scenario_profile"] = {
                    "id": SCENARIO_ID,
                    "summary": (
                        "Single INVITE RTP leg; early media tolerated; SIP timer B aligns "
                        "with max-call cap; teardown quirks 408/481/513 tolerated."
                    ),
                    "timings_relative_call_start": timings,
                    "findings_observed_in_scenario_layer": anomalies,
                }
                for note in anomalies:
                    call.metrics.add_anomaly("invite_media_observation", call.call_id, note)
            except Exception as exc:  # noqa: BLE001
                call.event("rtp_error", str(exc))
        else:
            dl = state["deadline"]
            if dl is not None:
                remote_bye = await _hold_media_with_in_dialog_sip(
                    call, state, lock, sdp_body, dl
                )

        call.record["media_hold_duration_s"] = duration

        if remote_bye:
            call.mark_success()
            call.event("call_terminated_ok")
            return

        call.event("bye_send")
        bye_resp = await call.send_bye()
        if bye_resp is None:
            call.mark_failure("bye_timeout")
            return
        bcode = bye_resp.status_code
        if _bye_teardown_acceptable(bcode):
            call.mark_success()
            call.event("call_terminated_ok")
            if bcode is not None and not (200 <= bcode < 300):
                call.record["bye_non_2xx_accepted"] = bcode
                call.record["bye_note"] = (
                    f"BYE completed with {bcode} (accepted as successful teardown for this test)."
                )
        else:
            call.mark_failure(f"bye:{bcode}")

    finally:
        if microphone_cm is not None:
            await microphone_cm.aclose()


async def _shutdown_rtp(state: dict) -> None:
    rtp = state.get("rtp")
    if rtp is None:
        return
    try:
        await rtp.stop()
    except Exception:  # noqa: BLE001
        pass
    state["rtp"] = None

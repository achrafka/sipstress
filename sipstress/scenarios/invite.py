"""Basic INVITE / 200 / ACK / [hold] / BYE scenario, no media."""
from __future__ import annotations

import asyncio
import time

from ..engine.call import CallContext
from ..sip import sdp as sip_sdp
from .library import register


@register("invite")
async def run(call: CallContext) -> None:
    sdp_body = sip_sdp.build_offer(
        call.local_ip, call.cfg.rtp_local_port or 40000, call.cfg.codec
    )
    call.event("invite_send")
    resp = await call.send_invite(sdp_body=sdp_body)
    if resp is None:
        call.mark_timeout("invite")
        call.event("invite_timeout")
        return

    call.final_status = resp.status_code
    if not resp.status_code or resp.status_code >= 300:
        call.event("invite_reject", str(resp.status_code))
        # ACK non-2xx is part of the same transaction
        call.send_ack(resp)
        call.mark_failure(f"invite:{resp.status_code}")
        return

    # 2xx -> ACK -> hold for call_duration -> BYE
    call.send_ack(resp)
    call.event("call_established")

    if resp.body:
        call.remote_sdp = sip_sdp.parse(resp.body)

    if call.cfg.call_duration_s > 0:
        try:
            await asyncio.sleep(call.cfg.call_duration_s)
        except asyncio.CancelledError:
            pass

    call.event("bye_send")
    bye_resp = await call.send_bye()
    if bye_resp is None:
        call.event("bye_timeout")
        # We still consider the call successful up to BYE
        call.mark_failure("bye_timeout")
        return

    if bye_resp.status_code and 200 <= bye_resp.status_code < 300:
        call.mark_success()
        call.event("call_terminated_ok")
    else:
        call.mark_failure(f"bye:{bye_resp.status_code}")
        call.event("bye_fail", str(bye_resp.status_code))

"""REGISTER scenario (with optional digest auth)."""
from __future__ import annotations

from ..engine.call import CallContext
from .library import register


@register("register")
async def run(call: CallContext) -> None:
    call.event("register_send")
    resp = await call.send_register()
    if resp is None:
        call.mark_timeout("register")
        call.event("register_timeout")
        return
    call.final_status = resp.status_code
    if resp.status_code and 200 <= resp.status_code < 300:
        call.mark_success()
        call.event("register_ok", str(resp.status_code))
    else:
        call.mark_failure(f"register:{resp.status_code}")
        call.event("register_fail", str(resp.status_code))

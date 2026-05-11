"""OPTIONS keep-alive / health-probe scenario."""
from __future__ import annotations

import time

from ..engine.call import CallContext
from .library import register


@register("options")
async def run(call: CallContext) -> None:
    call.event("options_send")
    resp = await call.send_options()
    if resp is None:
        call.mark_timeout("options")
        call.event("options_timeout")
        return
    call.final_status = resp.status_code
    if resp.status_code and 200 <= resp.status_code < 300:
        call.mark_success()
        call.event("options_ok", str(resp.status_code))
    else:
        call.mark_failure(f"options:{resp.status_code}")
        call.event("options_fail", str(resp.status_code))

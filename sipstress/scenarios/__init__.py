"""Built-in SIP scenarios.

Each scenario is a coroutine function:

    async def run(call: CallContext) -> None

The CallContext exposes helpers to send/receive SIP, schedule timers, attach
media, and report metrics.
"""
from .library import REGISTRY, get, list_names  # noqa: F401

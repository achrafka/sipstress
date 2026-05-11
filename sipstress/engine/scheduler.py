"""CPS scheduler with linear ramp-up / ramp-down.

The scheduler decides when the next call should fire. It is fed back into
the runner via the `next_delay()` and `should_continue()` calls. We also
expose `current_target_cps()` so metrics can record target vs actual CPS.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass


@dataclass
class CpsPlan:
    target_cps: float
    ramp_up_s: float = 0.0
    ramp_down_s: float = 0.0
    duration_s: float = 0.0  # total run duration; 0 -> bounded by --calls only
    total_calls: int = 0     # 0 -> bounded by --duration only

    def __post_init__(self) -> None:
        if self.target_cps < 0:
            self.target_cps = 0.0


class CpsScheduler:
    """Issue tickets at a controlled rate.

    Use `await scheduler.acquire()` before launching a new call. The method
    returns the timestamp at which the call actually starts.
    """

    def __init__(self, plan: CpsPlan) -> None:
        self.plan = plan
        self._start = time.monotonic()
        self._issued = 0
        self._closed = False

    @property
    def issued(self) -> int:
        return self._issued

    def elapsed(self) -> float:
        return time.monotonic() - self._start

    def current_target_cps(self) -> float:
        if self._closed:
            return 0.0
        e = self.elapsed()
        cps = self.plan.target_cps
        if self.plan.ramp_up_s > 0 and e < self.plan.ramp_up_s:
            return cps * (e / self.plan.ramp_up_s)
        if self.plan.duration_s > 0 and self.plan.ramp_down_s > 0:
            ramp_start = self.plan.duration_s - self.plan.ramp_down_s
            if e > ramp_start:
                remaining = max(0.0, self.plan.duration_s - e)
                return cps * (remaining / self.plan.ramp_down_s)
        return cps

    def time_budget_exhausted(self) -> bool:
        if self.plan.duration_s <= 0:
            return False
        return self.elapsed() >= self.plan.duration_s

    def call_budget_exhausted(self) -> bool:
        if self.plan.total_calls <= 0:
            return False
        return self._issued >= self.plan.total_calls

    def should_stop(self) -> bool:
        return (
            self._closed
            or self.time_budget_exhausted()
            or self.call_budget_exhausted()
        )

    def close(self) -> None:
        self._closed = True

    async def acquire(self) -> float:
        """Block until the next call slot. Returns the start monotonic time."""
        if self.should_stop():
            raise StopAsyncIteration
        # One-shot diagnostics (sipstress CLI call-test: total_calls=1) must not wait
        # ~1/cps seconds before INVITE—the live dashboard looked empty for that window.
        if self.plan.total_calls == 1 and self._issued == 0:
            self._issued = 1
            return time.monotonic()
        # We schedule by integrating the (variable) target rate. The expected
        # firing time of the n-th call is the moment when the integral of CPS
        # over [0, t] equals n. We approximate by stepping forward in small
        # ticks until enough budget has accrued.
        # For constant rate this collapses to t = n / cps.
        target_index = self._issued + 1
        while not self.should_stop():
            now = self.elapsed()
            accrued = self._integrate_target(now)
            if accrued >= target_index:
                self._issued = target_index
                return time.monotonic()
            # how long until accrual catches up at current cps?
            cps = max(self.current_target_cps(), 1e-3)
            deficit = target_index - accrued
            # bound the sleep to keep the curve smooth during ramps
            sleep_s = min(max(deficit / cps, 0.001), 0.25)
            await asyncio.sleep(sleep_s)
        raise StopAsyncIteration

    def _integrate_target(self, t: float) -> float:
        """Total expected calls at elapsed time t given the ramp profile."""
        cps = self.plan.target_cps
        ramp_up = self.plan.ramp_up_s
        ramp_down = self.plan.ramp_down_s
        duration = self.plan.duration_s

        total = 0.0
        # ramp-up region
        if ramp_up > 0:
            up_end = min(t, ramp_up)
            # Triangle area: (1/2) * up_end * (cps * up_end / ramp_up)
            total += 0.5 * (cps / ramp_up) * up_end * up_end
            if t <= ramp_up:
                return total
            t -= ramp_up
        else:
            up_end = 0.0

        # steady region
        steady_end_global = duration - ramp_down if duration > 0 else None
        steady_len = (steady_end_global - ramp_up) if steady_end_global else None
        if steady_len is None:
            # no fixed duration, steady forever
            total += cps * t
            return total
        steady_consume = min(t, steady_len)
        total += cps * steady_consume
        if t <= steady_len:
            return total
        t -= steady_len

        # ramp-down region
        if ramp_down > 0:
            dd = min(t, ramp_down)
            # cps decreases linearly from cps to 0 over ramp_down
            # area from 0..dd = cps*dd - (cps/(2*ramp_down))*dd*dd
            total += cps * dd - (cps / (2.0 * ramp_down)) * dd * dd
        return total

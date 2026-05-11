"""Live console dashboard.

Uses `rich` if available for a nicely formatted live table; falls back to a
single line of plain text printed every refresh otherwise.
"""
from __future__ import annotations

import sys
import time
from typing import Optional

try:  # pragma: no cover - optional dep
    from rich.console import Console
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table

    _HAS_RICH = True
except Exception:  # pragma: no cover
    _HAS_RICH = False

from ..engine.runner import RunnerStatus


class ConsoleDashboard:
    def __init__(self, scenario: str, enabled: bool = True) -> None:
        self.scenario = scenario
        self.enabled = enabled
        self._live: Optional["Live"] = None
        self._console: Optional["Console"] = None
        if enabled and _HAS_RICH:
            # Live display on stdout; logging stays on stderr (avoids Rich vs logging fighting).
            # IMPORTANT: asyncio calls update() ~2×/s from the event-loop thread while Rich's default
            # auto_refresh spins a separate thread (~4 FPS). Concurrent refreshes reliably corrupt
            # in-place repaint on common IDE terminals → stacked panel tops / stale table. Drive
            # refresh only from our callback instead.
            self._console = Console(file=sys.stdout, force_terminal=sys.stdout.isatty())

    def __enter__(self) -> "ConsoleDashboard":
        if self.enabled and _HAS_RICH:
            self._live = Live(
                self._render(None),
                console=self._console,
                auto_refresh=False,
                refresh_per_second=4,
                transient=True,
                redirect_stdout=False,
                redirect_stderr=False,
            )
            self._live.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._live:
            self._live.__exit__(exc_type, exc, tb)
            self._live = None

    def update(self, status: RunnerStatus) -> None:
        if not self.enabled:
            return
        if _HAS_RICH and self._live:
            self._live.update(self._render(status), refresh=True)
        else:
            self._print_plain(status)

    # -------- rendering --------
    def _render(self, status: Optional[RunnerStatus]):
        if not _HAS_RICH:
            return None
        table = Table(show_header=True, header_style="bold cyan", expand=True)
        table.add_column("director", style="bold")
        table.add_column("attempt", justify="right")
        table.add_column("ok", justify="right", style="green")
        table.add_column("fail", justify="right", style="red")
        table.add_column("timeout", justify="right", style="yellow")
        table.add_column("inflight", justify="right")
        table.add_column("setup p95 (ms)", justify="right")
        table.add_column("setup p99 (ms)", justify="right")
        table.add_column("top codes", justify="left")

        if status is not None:
            for label, m in status.metrics.items():
                snap = m.setup_latency.snapshot()
                p95 = snap.get("p95")
                p99 = snap.get("p99")
                p95_s = f"{p95*1000:.0f}" if p95 is not None else "-"
                p99_s = f"{p99*1000:.0f}" if p99 is not None else "-"
                top = ", ".join(
                    f"{c}:{n}"
                    for c, n in sorted(m.response_codes.items(), key=lambda kv: -kv[1])[:5]
                )
                table.add_row(
                    label,
                    str(m.calls_attempted),
                    str(m.calls_succeeded),
                    str(m.calls_failed),
                    str(m.calls_timed_out),
                    str(m.calls_inflight),
                    p95_s,
                    p99_s,
                    top,
                )
            header = (
                f"sipstress | scenario={self.scenario} | "
                f"elapsed={status.elapsed:6.1f}s  "
                f"target_cps={status.target_cps:6.1f}  "
                f"actual_cps={status.actual_cps:6.1f}  "
                f"inflight={status.inflight}"
            )
        else:
            header = f"sipstress | scenario={self.scenario}"
        return Panel(table, title=header, border_style="cyan")

    def _print_plain(self, status: RunnerStatus) -> None:
        line = (
            f"[{time.strftime('%H:%M:%S')}] elapsed={status.elapsed:6.1f}s "
            f"cps_t={status.target_cps:5.1f} cps_a={status.actual_cps:5.1f} "
            f"inflight={status.inflight}"
        )
        for label, m in status.metrics.items():
            snap = m.setup_latency.snapshot()
            p99 = snap.get("p99")
            p99_s = f"{p99*1000:.0f}" if p99 is not None else "-"
            line += (
                f" | {label}: ok={m.calls_succeeded} fail={m.calls_failed} "
                f"to={m.calls_timed_out} p99={p99_s}ms"
            )
        sys.stdout.write(line + "\n")
        sys.stdout.flush()

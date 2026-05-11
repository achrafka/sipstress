"""HTML dashboard builder — requires optional Plotly."""
from __future__ import annotations

import os
import tempfile
import unittest

from sipstress.analysis.health import analyze_run, HealthThresholds
from sipstress.reports.json_report import build_report


def _has_plotly() -> bool:
    try:
        import plotly.graph_objects  # noqa: F401

        return True
    except ImportError:
        return False


def _minimal_director_metrics() -> dict:
    return {
        "director": "127.0.0.2",
        "calls": {"attempted": 2, "succeeded": 1, "failed": 0, "timed_out": 1, "success_ratio": 0.5},
        "sip": {"response_codes": {"100": 1, "200": 1}, "responses_recv": 2, "retransmissions": 0},
        "latency_ms": {
            "setup": {"mean": 10.0, "p95": 10.0, "p99": 10.0},
            "answer": {"mean": 100.0, "p99": 100.0},
        },
        "media": {
            "packets_sent": 50,
            "packets_recv": 40,
            "jitter_ms": {"mean": 1.5},
            "loss_ratio": {"mean": 0.0},
        },
        "call_records": [
            {
                "call_id": "first-test-id@127.0.0.2",
                "duration_s": 5.2,
                "success": True,
                "final_status": 200,
                "failure_reason": None,
                "rtp": {
                    "jitter_ms": 1.2,
                    "loss_ratio": 0.01,
                    "packets_sent": 50,
                    "packets_recv": 48,
                },
                "events": [{"t": 0.05, "kind": "invite_send", "detail": ""}],
                "scenario_profile": {
                    "id": "invite_media",
                    "timings_relative_call_start": {
                        "invite_sent_s": 0.0,
                        "rtp_started_s": 0.04,
                        "call_established_s": 0.1,
                        "sip_invite_to_200_wall_s": 0.1,
                    },
                },
            },
            {
                "call_id": "second-test-id@127.0.0.2",
                "duration_s": 3.0,
                "success": False,
                "final_status": None,
                "failure_reason": "timeout:invite",
                "events": [],
            },
        ],
    }


@unittest.skipUnless(_has_plotly(), "plotly not installed (pip install 'sipstress[viz]')")
class HtmlDashboardTests(unittest.TestCase):
    def test_write_html_dashboard(self) -> None:
        from sipstress.reports.html_dashboard import write

        dm = [_minimal_director_metrics()]
        health = analyze_run(dm, HealthThresholds.for_single_call())
        report = build_report(
            cli_args={"mode": "call_test", "number_or_to": "sip:+15551212@gw"},
            metrics_dicts=dm,
            health=health,
            transport_stats={"datagrams_recv": 1, "datagrams_sent": 1, "datagrams_bad": 0},
        )
        report["_dashboard"] = {
            "pdf_basename": "stress-run.pdf",
            "pdf_from_this_run": True,
        }
        with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as f:
            path = f.name
        try:
            write(report, path)
            self.assertGreater(os.path.getsize(path), 10_000)
            with open(path, encoding="utf-8") as fh:
                body = fh.read()
            self.assertIn("sipstress call report", body.lower())
            self.assertIn("plotly.newplot", body.lower())
            self.assertIn("destination &amp; sound path", body.lower())
            self.assertIn("<section ", body.lower())
            self.assertIn("report-region", body)
            self.assertIn('<header class="hero">', body)
            self.assertIn("sip:+15551212@gw", body)
            self.assertIn("Diagnostics &amp; call path", body)
            self.assertIn("--bg: #0b1220", body)
            self.assertIn("Structured explanation layer", body)
            self.assertIn('onclick="window.print()"', body)
            self.assertIn("Print / save as PDF", body)
            self.assertIn("Download PDF", body)
            self.assertIn('href="stress-run.pdf"', body)
            self.assertIn('id="sipstress-all-calls"', body)
            self.assertIn("All calls in this run", body)
            self.assertIn("Per-call numbers (2 calls)", body)
            self.assertIn("first-test-id", body)
            self.assertIn("second-test-id", body)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()

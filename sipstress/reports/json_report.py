"""JSON report writer."""
from __future__ import annotations

import json
import time
from typing import Dict, List, Optional


def build_report(
    cli_args: Dict,
    metrics_dicts: List[Dict],
    health: Dict,
    transport_stats: Optional[Dict] = None,
) -> Dict:
    return {
        "tool": "sipstress",
        "version": "0.1.0",
        "report_schema": "sipstress_json_v2",
        "report_notes": (
            "directors[*].media + sip + anomalies + call_records[].rtp hold per-call RTP; "
            "call_records[].scenario_profile summarizes invite_media timing and race hints; "
            "call_records[].rtp.extended_media_quality has asymmetric loss / silence heuristics."
        ),
        "generated_at": time.time(),
        "cli_args": cli_args,
        "transport": transport_stats or {},
        "directors": metrics_dicts,
        "health": health,
    }


def write(report: Dict, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)

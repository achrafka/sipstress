"""CLI: SIP INVITE load/call tests with optional multi-callee rotation."""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .analysis.health import HealthThresholds, analyze_run
from .engine.call import DirectorTarget
from .engine.runner import Runner, RunnerConfig
from .reports.console import ConsoleDashboard
from .reports.json_report import build_report, write as write_json
from .reports.html_dashboard import write as write_html_dashboard
from .reports.html_to_pdf import write_pdf_from_html
from .reports.summary import render as render_summary, write as write_summary

log = logging.getLogger("sipstress.cli")

# Fixed scenario: INVITE + RTP for the whole call; no IVR scripting on the client.


def _html_dashboard_meta(html_path: str, pdf_out: Optional[str]) -> Dict[str, Any]:
    hp = Path(html_path)
    if pdf_out:
        return {"pdf_basename": Path(pdf_out).name, "pdf_from_this_run": True}
    return {"pdf_basename": hp.with_suffix(".pdf").name, "pdf_from_this_run": False}


DEFAULT_SCENARIO = "invite_media"


def parse_duration(s: str) -> float:
    """Parse '500ms' / '30s' / '5m' / plain seconds."""
    import re

    s = str(s).strip().lower()
    if not s:
        return 0.0
    m = re.match(r"^\s*(\d+(?:\.\d+)?)(ms|s|m|h)?\s*$", s, re.I)
    if not m:
        raise argparse.ArgumentTypeError(f"Invalid duration: {s!r}")
    val = float(m.group(1))
    unit = (m.group(2) or "s").lower()
    scale = {"ms": 1 / 1000.0, "s": 1.0, "m": 60.0, "h": 3600.0}[unit]
    return val * scale


def _parse_director(spec: str) -> DirectorTarget:
    if "=" in spec and not spec.startswith("sip"):
        label, _, uri = spec.partition("=")
        return DirectorTarget.from_uri(label.strip(), uri.strip())
    target = DirectorTarget.from_uri("director", spec.strip())
    target.label = target.host or "director"
    return target


def _parse_port_range(spec: str) -> tuple[int, int]:
    if "-" not in spec:
        raise argparse.ArgumentTypeError("RTP port range must be LOW-HIGH")
    lo, hi = spec.split("-", 1)
    return int(lo.strip()), int(hi.strip())


def _normalize_party_uri(
    raw: Optional[str], *, default_user: str, default_host: str
) -> str:
    if not raw:
        return f"sip:{default_user}@{default_host}"
    s = raw.strip()
    low = s.lower()
    if low.startswith("sip:") or low.startswith("sips:") or "<" in s:
        return s
    if "@" in s:
        return f"sip:{s}"
    if "." in s or ":" in s:
        return f"sip:{default_user}@{s}"
    return f"sip:{s}@{default_host}"


def _pai_angle_bracket_form(spec: str, default_host: str) -> str:
    """Value for ``P-Asserted-Identity`` as a SIP name-addr in angle brackets."""
    s = spec.strip()
    if not s:
        raise SystemExit("--pai value is empty.")
    if s.startswith("<") and s.endswith(">"):
        return s
    uri = _normalize_party_uri(s, default_user="caller", default_host=default_host)
    return f"<{uri}>"


def _parse_start_at(s: str) -> float:
    if s.startswith("+"):
        return time.time() + parse_duration(s[1:])
    try:
        return float(s)
    except ValueError:
        pass
    return datetime.fromisoformat(s).timestamp()


def _split_numbers_csv(s: str) -> List[str]:
    return [p.strip() for p in str(s).split(",") if p.strip()]


def _read_numbers_file(path: str) -> List[str]:
    out: List[str] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            out.append(line)
    return out


def _flatten_numbers_config_value(raw: object) -> List[str]:
    """YAML/CLI may pass a string, comma string, or list of tokens."""
    if raw is None:
        return []
    if isinstance(raw, list):
        acc: List[str] = []
        for item in raw:
            acc.extend(_split_numbers_csv(str(item)))
        return acc
    return _split_numbers_csv(str(raw))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sipstress",
        description=(
            "Place a SIP INVITE with RTP through a director, stay up for "
            "--duration while the callee side runs its own IVR/routing "
            "(no client-side scenario scripting). Then hang up and print diagnostics."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Example:\n"
            "  sipstress --director YOUR_SBC 5550100 --duration 120s\n"
            "  sipstress --director YOUR_SBC 5550100 -d 120s "
            "--from sip:+15555550101@realm --pai +15555550101@realm\n\n"
            "Several callees (round-robin), 4 parallel, 30 attempts at 2/s:\n"
            "  sipstress --director YOUR_SBC --numbers "
            "111,222,333 --calls 30 --cps 2 -j 4 -d 30s\n\n"
            "Optional WAV of received RTP:\n"
            "  sipstress --director YOUR_SBC 5550100 -d 60s --record ./rec\n\n"
            "HTML dashboard: uv sync --extra viz (or pip install -e \".[viz]\")\n"
            "  sipstress ... --json-out ./report.json --html-out ./report.html\n"
        ),
    )
    p.add_argument(
        "number",
        nargs="?",
        metavar="NUMBER",
        help="Callee (digits or user part). Also accepted as --to.",
    )
    p.add_argument(
        "--config",
        help="YAML with keys: director, number|to, duration, ...",
    )
    p.add_argument(
        "--director",
        default=None,
        help="SIP director: host, sip:host:port, or label=sip:host:port (required).",
    )
    p.add_argument(
        "--to",
        dest="to_uri",
        default=None,
        help="Override Request-URI / To (if not using positional NUMBER).",
    )
    p.add_argument(
        "--from",
        dest="from_uri",
        default=None,
        help="From URI (default sip:sipstress@<director-host>).",
    )
    p.add_argument(
        "-d",
        "--duration",
        type=parse_duration,
        default="60s",
        metavar="DUR",
        help="Time to keep media active after 200 OK (default 60s).",
    )
    p.add_argument(
        "--max-call-duration",
        type=parse_duration,
        default=None,
        metavar="DUR",
        help="Hard cap for the whole attempt (default: duration + 3 minutes).",
    )
    p.add_argument(
        "--numbers",
        default=None,
        metavar="LIST",
        help=(
            "Comma-separated callees (digits or sip: URIs). Calls rotate round-robin "
            "in order. Do not combine with the positional NUMBER or --to."
        ),
    )
    p.add_argument(
        "--numbers-file",
        default=None,
        metavar="FILE",
        help="One callee per line; blank lines and # comments ignored.",
    )
    p.add_argument(
        "--calls",
        "--total-calls",
        dest="total_calls",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Total INVITE attempts to schedule (default: 1, or one per callee "
            "when multiple targets are set)."
        ),
    )
    p.add_argument(
        "--cps",
        type=float,
        default=1.0,
        metavar="RATE",
        help="Target new-call rate (calls per second) for the scheduler (default 1).",
    )
    p.add_argument(
        "-j",
        "--concurrency",
        type=int,
        default=1,
        metavar="N",
        help="Maximum simultaneous call attempts (default 1).",
    )
    p.add_argument(
        "--call-delay",
        type=parse_duration,
        default="0",
        metavar="DUR",
        help="Extra delay after each attempt is started (stagger; default 0).",
    )
    p.add_argument(
        "--ramp-up",
        type=parse_duration,
        default="0",
        metavar="DUR",
        help="Linear ramp from 0 to full CPS over this time (default 0).",
    )
    p.add_argument(
        "--ramp-down",
        type=parse_duration,
        default="0",
        metavar="DUR",
        help="Linear ramp down before --run-duration ends (default 0).",
    )
    p.add_argument(
        "--run-duration",
        type=parse_duration,
        default="0",
        metavar="DUR",
        help=(
            "Wall-clock limit for scheduling new attempts (0 = no time limit; "
            "use --calls or Ctrl+C). Uses ramp-down near the end when set."
        ),
    )
    p.add_argument(
        "--contact-user",
        default="sipstress",
        help="Username in Contact header.",
    )
    p.add_argument("--auth", help="Digest USER:PASSWORD")
    p.add_argument(
        "--register-on-start",
        action="store_true",
        help="REGISTER before the test call.",
    )
    p.add_argument("--bind-ip", default="0.0.0.0", help="Local SIP bind address.")
    p.add_argument("--bind-port", type=int, default=0, help="Local SIP port (0=OS).")
    p.add_argument(
        "--advertised-ip",
        default=None,
        help="IP in Via/Contact/SDP if different from bind (e.g. NAT).",
    )
    p.add_argument(
        "--trace-sip",
        action="store_true",
        help="Log every SIP datagram (very verbose).",
    )
    p.add_argument(
        "--rtp-port-range",
        default="40000-41000",
        help="RTP UDP range LOW-HIGH (default 40000-41000).",
    )
    p.add_argument(
        "--codec",
        choices=("pcmu", "pcma"),
        default="pcmu",
        help="RTP codec (default pcmu).",
    )
    p.add_argument(
        "--invite-timeout",
        default=None,
        type=parse_duration,
        metavar="DUR",
        help="INVITE wait for final (200–699). Default matches overall call cap; "
             "DialWaiting often needs minutes on 183 only.",
    )
    p.add_argument(
        "--bye-timeout",
        type=parse_duration,
        default=parse_duration("32s"),
        help="BYE response timeout.",
    )
    p.add_argument(
        "--t1",
        type=parse_duration,
        default=parse_duration("500ms"),
        help="SIP T1 retransmit interval.",
    )
    p.add_argument(
        "--extra-header",
        action="append",
        default=[],
        help="Extra SIP header 'Name: value' (repeatable).",
    )
    p.add_argument(
        "--provider",
        default=None,
        metavar="NAME",
        help=(
            "If set, add header X-provider: NAME on INVITE (and REGISTER/OPTIONS) "
            "when the director/OpenSIPS picks gateways for mise en relation / PSTN legs. "
            "Same as --extra-header 'X-provider: NAME' unless you already set "
            "X-provider explicitly."
        ),
    )
    p.add_argument(
        "--pai",
        default=None,
        metavar="URI",
        help=(
            "Set P-Asserted-Identity to <sip:user@domain> using this URI fragment "
            "(e.g. +331234@sbc.example); implies director host where needed. Overrides "
            "same header from earlier --extra-header."
        ),
    )
    p.add_argument(
        "--record",
        dest="record_rtp_dir",
        default=None,
        metavar="DIR",
        help="Write RTP-related WAV(s) under DIR.",
    )
    p.add_argument(
        "--record-duplex",
        dest="record_duplex_wav",
        action="store_true",
        help=(
            "With --record, write stereo 16-bit 8 kHz WAV: LEFT=incoming audio from the call, "
            "RIGHT=what we transmitted on RTP (decoded G.711 from each packet — matches the "
            "wire after encoding; not raw PC microphone PCM). Filename uses suffix _duplex. "
            "Use --microphone to feed live speech into that RTP stream."
        ),
    )
    p.add_argument(
        "--microphone",
        dest="record_microphone",
        action="store_true",
        help=(
            "Encode default capture device into outbound RTP (8 kHz mono). "
            "Requires audio extra (uv sync --extra audio / pip install -e \".[audio]\") "
            "+ audioop or audioop-lts (Python 3.13+). "
            "With --record-duplex, the WAV right channel is still taken from RTP "
            "payloads (decoded G.711), not a separate raw mic tap. "
            "Levels use float capture + headroom; add --mic-gain if still too loud."
        ),
    )
    p.add_argument(
        "--mic-gain",
        type=float,
        default=1.0,
        metavar="G",
        help=(
            "Microphone linear gain after built-in headroom (~0.82×). Default 1.0; "
            "try 0.5–0.75 if duplex WAV/RTP sounds harsh or too loud."
        ),
    )
    p.add_argument(
        "--record-inbound-gain",
        type=float,
        default=0.72,
        metavar="G",
        dest="inbound_record_gain",
        help=(
            "When recording WAV (--record), multiply decoded inbound (far-end→you) PCM by G "
            "before writing to disk. Default 0.72 softens abrupt loudness after the callee answers "
            "versus ringback/early media. Does not affect live RTP or JSON metrics; use 1.0 for full level."
        ),
    )
    p.add_argument(
        "--audit",
        action="store_true",
        help="Include per-call SIP/RTP audit in JSON (verbose).",
    )
    p.add_argument(
        "--start-at",
        default=None,
        help="Delay start: ISO time, epoch, or +30s",
    )
    p.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    p.add_argument("--log-file", default=None)
    p.add_argument("--json-out", metavar="FILE", help="JSON diagnostic report.")
    p.add_argument(
        "--html-out",
        metavar="FILE",
        help=(
            "Write HTML dashboard (Plotly graphs, KPI strip, explanations); "
            "needs viz extra (uv sync --extra viz / pip install -e \".[viz]\")."
        ),
    )
    p.add_argument(
        "--pdf-out",
        metavar="FILE",
        help=(
            "Write PDF of the HTML dashboard (needs --html-out in the same run). "
            "Uses Playwright if installed (uv sync --extra pdf && playwright install chromium; "
            "or pip install -e \".[pdf]\" …), else Chromium/Chrome headless."
        ),
    )
    p.add_argument("--summary-out", metavar="FILE", help="Text summary.")
    p.add_argument(
        "--no-dashboard",
        action="store_true",
        help="No live dashboard (CI / piping).",
    )
    p.add_argument(
        "--print-summary",
        action="store_true",
        help="Always echo text summary (default when no files set).",
    )
    return p


def _apply_yaml_config(args: argparse.Namespace, path: str) -> None:
    try:
        import yaml  # type: ignore
    except ImportError:
        raise SystemExit("PyYAML not installed; cannot use --config.")

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise SystemExit("Config root must be a mapping")

    keymap = {
        "to": "to_uri",
        "from": "from_uri",
        "number": "number",
        "calls": "total_calls",
        "total_calls": "total_calls",
    }
    duration_attrs = {
        "duration",
        "max_call_duration",
        "invite_timeout",
        "bye_timeout",
        "t1",
        "call_delay",
        "ramp_up",
        "ramp_down",
        "run_duration",
    }
    float_attrs = {"mic_gain", "inbound_record_gain", "cps"}

    for k, v in data.items():
        raw_attr = k.replace("-", "_")
        attr = keymap.get(raw_attr, raw_attr)
        if attr == "director":
            args.director = str(v).strip()
            continue
        if not hasattr(args, attr):
            continue
        if attr in duration_attrs and v is not None:
            if isinstance(v, (int, float)):
                setattr(args, attr, float(v))
            else:
                setattr(args, attr, parse_duration(str(v)))
        elif attr in float_attrs and v is not None:
            setattr(args, attr, float(v))
        else:
            setattr(args, attr, v)


def _build_runner_config(args: argparse.Namespace) -> RunnerConfig:
    directors = [_parse_director(args.director)]
    primary = directors[0]

    if args.number and args.to_uri:
        raise SystemExit("Use either the positional NUMBER or --to, not both.")
    multi_from_csv = _flatten_numbers_config_value(getattr(args, "numbers", None))

    multi_from_file: List[str] = []
    nf = getattr(args, "numbers_file", None)
    if nf:
        multi_from_file = _read_numbers_file(str(nf))

    raw_targets = multi_from_csv + multi_from_file
    raw_single = args.to_uri if args.to_uri else args.number

    if raw_targets and raw_single:
        raise SystemExit(
            "Use either positional NUMBER / --to, or --numbers / --numbers-file, not both."
        )
    if not raw_targets and not raw_single:
        raise SystemExit(
            "Missing callee: provide NUMBER, --to, --numbers, or --numbers-file."
        )

    if raw_targets:
        to_uri = _normalize_party_uri(
            raw_targets[0], default_user="test", default_host=primary.host
        )
        callees_rotate = [
            _normalize_party_uri(
                t, default_user="test", default_host=primary.host
            )
            for t in raw_targets
        ]
    else:
        assert raw_single is not None
        to_uri = _normalize_party_uri(
            raw_single, default_user="test", default_host=primary.host
        )
        callees_rotate = []
    from_uri = _normalize_party_uri(
        args.from_uri, default_user="sipstress", default_host=primary.host
    )

    auth_user = auth_pass = None
    if args.auth:
        parts = args.auth.split(":", 1)
        auth_user = parts[0]
        auth_pass = parts[1] if len(parts) > 1 else ""

    extra_headers: Dict[str, str] = {}
    for h in args.extra_header or []:
        if ":" not in h:
            raise SystemExit(f"Bad --extra-header: {h!r}")
        n, _, v = h.partition(":")
        extra_headers[n.strip()] = v.strip()

    provider = getattr(args, "provider", None)
    if provider:
        pname = str(provider).strip()
        if pname:
            extra_headers.setdefault("X-provider", pname)

    if args.pai:
        extra_headers["P-Asserted-Identity"] = _pai_angle_bracket_form(
            str(args.pai), primary.host
        )

    rtp_lo, rtp_hi = _parse_port_range(args.rtp_port_range)
    call_dur = float(args.duration)
    max_dur = args.max_call_duration
    if max_dur is None:
        max_dur = call_dur + 180.0
    max_dur = max(max_dur, call_dur + 30.0)

    invite_tm = (
        float(args.invite_timeout) if args.invite_timeout is not None else max_dur
    )
    invite_tm = max(invite_tm, max_dur)

    if getattr(args, "record_duplex_wav", False) and not args.record_rtp_dir:
        raise SystemExit("--record-duplex requires --record DIR.")
    if getattr(args, "record_microphone", False):
        from .media.pcmu_encode import pcmu_encoding_available

        if not pcmu_encoding_available():
            raise SystemExit(
                "--microphone needs G.711 encoding: use Python≤3.12 with stdlib audioop, "
                "or pip install audioop-lts on Python 3.13+."
            )
    mg = float(getattr(args, "mic_gain", 1.0))
    if mg <= 0 or mg > 4.0:
        raise SystemExit("--mic-gain must be in (0, 4].")
    irg = float(getattr(args, "inbound_record_gain", 0.72))
    if irg <= 0 or irg > 2.0:
        raise SystemExit("--record-inbound-gain must be in (0, 2].")

    explicit_calls = getattr(args, "total_calls", None)
    if explicit_calls is not None and explicit_calls < 1:
        raise SystemExit("--calls must be at least 1")
    if explicit_calls is not None:
        total_calls = int(explicit_calls)
    elif len(callees_rotate) > 1:
        total_calls = len(callees_rotate)
    else:
        total_calls = 1

    cps = float(getattr(args, "cps", 1.0))
    if cps < 0:
        raise SystemExit("--cps must be >= 0")
    conc = int(getattr(args, "concurrency", 1))
    if conc < 1:
        raise SystemExit("--concurrency must be >= 1")

    call_delay_s = float(getattr(args, "call_delay", 0.0))
    ramp_up_s = float(getattr(args, "ramp_up", 0.0))
    ramp_down_s = float(getattr(args, "ramp_down", 0.0))
    duration_s = float(getattr(args, "run_duration", 0.0))
    if duration_s < 0:
        raise SystemExit("--run-duration must be >= 0")

    return RunnerConfig(
        directors=directors,
        scenario=DEFAULT_SCENARIO,
        from_uri=from_uri,
        to_uri=to_uri,
        callees_rotate=callees_rotate,
        contact_user=args.contact_user,
        auth_user=auth_user,
        auth_pass=auth_pass,
        total_calls=total_calls,
        duration_s=duration_s,
        cps=cps,
        ramp_up_s=ramp_up_s,
        ramp_down_s=ramp_down_s,
        concurrency=conc,
        call_delay_s=call_delay_s,
        call_duration_s=call_dur,
        max_call_duration_s=max_dur,
        media_enabled=True,
        codec=args.codec,
        ivr_plan=[],
        ivr_post_play_s=0.0,
        record_rtp_dir=args.record_rtp_dir,
        record_duplex_wav=getattr(args, "record_duplex_wav", False),
        record_microphone=getattr(args, "record_microphone", False),
        mic_gain=mg,
        inbound_record_gain=irg,
        detail_log=args.audit,
        test_plan=None,
        bind_ip=args.bind_ip,
        bind_port=args.bind_port,
        advertised_ip=args.advertised_ip,
        trace_sip=args.trace_sip,
        rtp_port_min=rtp_lo,
        rtp_port_max=rtp_hi,
        invite_timer_b_s=invite_tm,
        non_invite_timer_f_s=float(args.bye_timeout),
        invite_t1_s=float(args.t1),
        register_on_start=args.register_on_start,
        extra_headers=extra_headers,
        start_at_epoch=(
            _parse_start_at(args.start_at) if args.start_at else 0.0
        ),
    )


def _setup_logging(level: str, log_file: Optional[str]) -> None:
    handlers: List[logging.Handler] = []
    if log_file:
        handlers.append(logging.FileHandler(log_file, mode="w", encoding="utf-8"))
    if not handlers:
        handlers.append(logging.StreamHandler(stream=sys.stderr))
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        handlers=handlers,
        format="%(asctime)s [%(levelname)5s] %(name)s: %(message)s",
        force=True,
    )


def _cli_snapshot(args: argparse.Namespace, rcfg: RunnerConfig) -> Dict[str, Any]:
    snap: Dict[str, Any] = {
        "mode": "call_test",
        "director": args.director,
        "number_or_to": rcfg.to_uri,
        "callees_rotate": list(rcfg.callees_rotate) if rcfg.callees_rotate else None,
        "total_calls": rcfg.total_calls,
        "cps": rcfg.cps,
        "concurrency": rcfg.concurrency,
        "call_delay_s": rcfg.call_delay_s,
        "ramp_up_s": rcfg.ramp_up_s,
        "ramp_down_s": rcfg.ramp_down_s,
        "run_duration_s": rcfg.duration_s,
        "duration_s": float(args.duration),
        "scenario": DEFAULT_SCENARIO,
        "media": True,
        "codec": args.codec,
        "audit": args.audit,
        "auth": bool(args.auth),
    }
    prov = getattr(args, "provider", None)
    if prov:
        snap["x_provider_requested"] = str(prov).strip()
    snap["record_duplex_wav"] = bool(getattr(args, "record_duplex_wav", False))
    snap["record_microphone"] = bool(getattr(args, "record_microphone", False))
    snap["mic_gain"] = float(getattr(args, "mic_gain", 1.0))
    snap["inbound_record_gain"] = float(getattr(args, "inbound_record_gain", 0.72))
    return snap


async def _run_async(args: argparse.Namespace) -> int:
    rcfg = _build_runner_config(args)
    runner = Runner(rcfg)
    dashboard = ConsoleDashboard("call-test", enabled=not args.no_dashboard and sys.stdout.isatty())
    runner.set_status_callback(dashboard.update)
    stop_event = asyncio.Event()

    def _on_signal() -> None:
        if not stop_event.is_set():
            stop_event.set()
            runner._scheduler.close()  # noqa: SLF001

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except NotImplementedError:
            pass

    with dashboard:
        await runner.run()

    metrics_dicts = [m.to_dict() for m in runner.metrics.values()]
    thresholds = (
        HealthThresholds.for_single_call()
        if rcfg.total_calls == 1 and rcfg.concurrency == 1
        else HealthThresholds()
    )
    health = analyze_run(metrics_dicts, thresholds)
    report = build_report(
        cli_args=_cli_snapshot(args, rcfg),
        metrics_dicts=metrics_dicts,
        health=health,
        transport_stats=runner.transport.stats,
    )

    pdf_out = getattr(args, "pdf_out", None)

    if args.json_out:
        write_json(report, args.json_out)
        log.info("JSON report written to %s", args.json_out)
    if args.html_out:
        try:
            dash_report = dict(report)
            dash_report["_dashboard"] = _html_dashboard_meta(args.html_out, pdf_out)
            write_html_dashboard(dash_report, args.html_out)
        except ImportError as e:
            raise SystemExit(
                f"{e}\nInstall charts support: uv sync --extra viz "
                '(or pip install plotly / pip install -e ".[viz]")'
            ) from e
        log.info("HTML dashboard written to %s", args.html_out)
    if pdf_out:
        if not args.html_out:
            raise SystemExit("--pdf-out requires --html-out (same run).")
        try:
            write_pdf_from_html(args.html_out, pdf_out)
        except (OSError, RuntimeError) as e:
            raise SystemExit(str(e)) from e
        log.info("PDF report written to %s", pdf_out)
    if args.summary_out:
        write_summary(report, args.summary_out)
        log.info("Summary written to %s", args.summary_out)

    if args.print_summary or (not args.json_out and not args.summary_out):
        sys.stdout.write("\n" + render_summary(report))
        sys.stdout.flush()

    return 0 if health.get("pass_fail") == "PASS" else 2


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.config:
        _apply_yaml_config(args, args.config)
        if getattr(args, "director", None):
            args.director = str(args.director)

    if not args.director:
        raise SystemExit("Missing director: use --director or set director in --config YAML.")

    _setup_logging(args.log_level, args.log_file)

    try:
        return asyncio.run(_run_async(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

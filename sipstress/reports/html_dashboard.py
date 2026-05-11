"""HTML report with interactive Plotly charts (CDN) — requires ``pip install plotly``.

Full dashboard for ``--html-out``; mirrors key JSON findings in the browser."""
from __future__ import annotations

import html as html_std
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

PLOTLY_CDN = "https://cdn.plot.ly/plotly-2.31.1.min.js"


def _banner(health: Dict[str, Any]) -> Tuple[str, str]:
    v = str(health.get("overall_verdict") or "OK").upper()
    pf = str(health.get("pass_fail") or "PASS")
    if v == "FAIL" or pf == "FAIL":
        return f"{v} — pass_fail={pf}", "fail"
    if v == "WARN":
        return v, "warn"
    return v, "ok"


def _cli_cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        import json  # noqa: PLC0415 — local to avoid import if unused

        return json.dumps(v, default=str, separators=(",", ":"))
    return str(v)


FINDING_HINTS: Dict[str, str] = {
    "low_success_ratio": (
        "Many calls failed — check SIP response_codes and failure_reasons in JSON."
    ),
    "excessive_timeouts": "SIP timeouts — UDP path and director timers.",
    "high_retransmissions": (
        "SIP duplicate responses often mean UDP loss; check signalling path capture."
    ),
}

REPORT_GUIDE_EXCERPT = """sipstress_json_v2 — cheat sheet:

• transport.datagram_* = SIP signalling UDP only (not RTP).

• directors[].sip.response_codes — histogram of all SIP statuses seen.

• directors[].latency_ms.setup / answer metrics.

• directors[].media — jitter_ms, loss_ratio, packet counts.

• health.pass_fail = FAIL only if overall_verdict is FAIL.

See REPORT_GUIDE.md for full documentation."""

# Mirrors ``plan/spec.StepType`` — maps sipstress ``step_type`` to typical PV3 compos families.
STEP_TYPE_PV3_BLURB: Dict[str, str] = {
    "play": "PV3 Play, Say*, SpeechSynthesis …",
    "menu": "PV3 Menu (single-digit branching)",
    "get_digits": "PV3 GetDigits, AudioPicker",
    "send_dtmf": "PV3 SendDTMF",
    "dial": "PV3 DialSimple / DialWaiting / DialMulti …",
    "queue": "PV3 WaitingQueue, VirtualQueue",
    "record": "PV3 recording compos",
    "wait": "Timed wait / pacing",
    "silence": "Wait for inbound silence",
    "answer": "PV3 Answer (expect 200 if not answered)",
    "hangup": "PV3 Hangup",
    "note": "Structural / branching marker (minimal audio)",
}


_EMBEDDED_CSS = """
:root {
  --bg: #0b1220;
  --card: #111c2e;
  --text: #e2e8f0;
  --muted: #94a3b8;
  --border: rgba(148,163,184,0.18);
  --ok: #34d399;
  --warn: #fbbf24;
  --fail: #f87171;
}
* { box-sizing: border-box; }
body {
  font-family: Inter, system-ui, sans-serif;
  margin: 0;
  background: radial-gradient(1200px 600px at 10% -10%, rgba(56,189,248,.08), transparent 55%),
              linear-gradient(180deg, #070b14 0%, var(--bg) 45%);
  color: var(--text);
  line-height: 1.55;
}
.wrap { max-width: 1200px; margin: 0 auto; padding: 1.5rem 1.25rem 3rem; }
.hero {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 16px;
  padding: 1.35rem 1.5rem;
  box-shadow: 0 8px 32px rgba(0,0,0,.35);
}
.hero h1 { margin: 0 0 .35rem; font-size: 1.45rem; font-weight: 700; letter-spacing: -0.02em; }
.hero .meta { color: var(--muted); font-size: .9rem; }
.pill {
  display: inline-block;
  padding: .28rem .75rem;
  border-radius: 999px;
  font-weight: 600;
  font-size: .82rem;
  margin-top: .75rem;
}
.pill.ok { background: rgba(16,185,129,.15); color: #6ee7b7; border: 1px solid rgba(52,211,153,.35); }
.pill.warn { background: rgba(251,191,36,.12); color: #fcd34d; border: 1px solid rgba(251,191,36,.35); }
.pill.fail { background: rgba(248,113,113,.12); color: #fca5a5; border: 1px solid rgba(248,113,113,.35); }
.kpi-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: .85rem;
  margin: 1.25rem 0 2rem;
}
.kpi {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 1rem;
  box-shadow: 0 4px 16px rgba(0,0,0,.2);
}
.kpi.good { border-top: 3px solid var(--ok); }
.kpi.warn { border-top: 3px solid var(--warn); }
.kpi.bad { border-top: 3px solid var(--fail); }
.kpi.neutral { border-top: 3px solid #64748b; }
.kpi-label { font-size: .72rem; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); font-weight: 600; }
.kpi-value { font-size: 1.15rem; font-weight: 700; margin: .35rem 0; }
.kpi-hint { font-size: .78rem; color: var(--muted); line-height: 1.35; }
h2 {
  font-size: 1.12rem;
  font-weight: 700;
  margin: 2.25rem 0 .85rem;
  letter-spacing: -0.01em;
  color: #f1f5f9;
}
.section {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 14px;
  padding: 1.15rem 1.25rem 1.5rem;
  margin-bottom: 1.25rem;
  box-shadow: 0 4px 20px rgba(0,0,0,.22);
}
.section h3 { margin: 0 0 .5rem; font-size: 1.05rem; font-weight: 600; color: #f8fafc; }
.chart-host .js-plotly-plot { width: 100% !important; }
table.cli { width: 100%; border-collapse: collapse; font-size: .88rem; margin: .5rem 0; }
table.cli th, table.cli td { border: 1px solid var(--border); padding: .45rem .6rem; text-align: left; }
table.cli tr:nth-child(odd) { background: rgba(30,41,59,.35); }
ul.findings li.sev-fail { color: #fca5a5; }
ul.findings li.sev-warn { color: #fdba74; }
details.summary {
  margin-top: 2rem;
  background: rgba(17,28,46,.9);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 1rem 1.2rem;
  font-size: .88rem;
}
details.summary summary { cursor: pointer; color: #e2e8f0; }
.muted { color: var(--muted); }
.prose { color: #cbd5e1; font-size: .9rem; margin: 0 0 1rem; max-width: 78ch; }
.prose.lead { font-size: .95rem; color: #e2e8f0; }
.prose code {
  background: rgba(15,23,42,.85);
  padding: .1rem .32rem;
  border-radius: 4px;
  font-size: .84em;
  border: 1px solid rgba(148,163,184,.2);
}
.diag-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: .85rem 1rem;
  margin: .5rem 0 1.5rem;
}
.diag-card {
  background: rgba(17,28,46,.65);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: .9rem 1rem 1rem;
}
.diag-card h4 {
  margin: 0 0 .45rem;
  font-size: .78rem;
  text-transform: uppercase;
  letter-spacing: .07em;
  color: #94a3b8;
  font-weight: 600;
}
.diag-body { margin: 0; font-size: .86rem; color: #cbd5e1; line-height: 1.5; }
.destination-panel.section { margin-top: .5rem; margin-bottom: 1.75rem; }
.destination-panel .dest-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
  gap: 1rem 1.25rem;
  margin-top: .75rem;
}
.destination-panel .dest-label {
  font-size: .68rem;
  text-transform: uppercase;
  letter-spacing: .07em;
  color: var(--muted);
  font-weight: 600;
  display: block;
  margin-bottom: .25rem;
}
.destination-panel .dest-value {
  font-size: .98rem;
  font-weight: 600;
  word-break: break-all;
  line-height: 1.35;
  color: #f1f5f9;
}
ul.dest-story { margin: 1rem 0 0; padding-left: 1.15rem; max-width: 72ch; }
ul.dest-story li { margin: .35rem 0; }
section.report-region { display: block; }
.report-actions {
  margin-top: 1rem;
  display: flex;
  flex-wrap: wrap;
  gap: .65rem;
  align-items: center;
}
.report-actions .btn {
  display: inline-block;
  padding: .45rem 1rem;
  border-radius: 10px;
  font-weight: 600;
  font-size: .86rem;
  text-decoration: none;
  cursor: pointer;
  border: 1px solid var(--border);
  font-family: inherit;
}
.report-actions .btn-primary {
  background: rgba(56,189,248,.14);
  color: #7dd3fc;
  border-color: rgba(56,189,248,.42);
}
.report-actions .btn-outline {
  background: rgba(17,28,46,.55);
  color: var(--text);
}
.report-actions .btn:hover { opacity: .92; filter: brightness(1.05); }
.plan-table-wrap { overflow-x: auto; margin-top: .5rem; }
table.plan-steps {
  width: 100%; border-collapse: collapse; font-size: .82rem;
}
table.plan-steps th, table.plan-steps td {
  border: 1px solid var(--border); padding: .42rem .5rem; vertical-align: top;
  text-align: left;
}
table.plan-steps th { font-size: .7rem; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); }
tr.verdict-ok td:first-child { box-shadow: inset 3px 0 0 #34d399; }
tr.verdict-warn td:first-child { box-shadow: inset 3px 0 0 #fbbf24; }
tr.verdict-fail td:first-child { box-shadow: inset 3px 0 0 #f87171; }
.details-block { margin: .85rem 0 0; padding: .85rem 1rem; border-radius: 12px; background: rgba(17,28,46,.65); border: 1px solid var(--border); }
@media print {
  .report-actions { display: none !important; }
  .wrap { max-width: 100%; padding: 0; }
  details.summary summary { cursor: default; }
  body { background: #fff !important; color: #111 !important; }
  .section, .hero, details.summary { border-color: #ccc !important; box-shadow: none !important; background: #f9fafb !important; }
}
@media (max-width: 640px) {
  .wrap { padding: 1rem .85rem 2rem; }
}
"""


def _require_plotly() -> Tuple[Any, Any, Any]:
    try:
        import plotly.graph_objects as go  # noqa: PLC0415
        import plotly.io as pio  # noqa: PLC0415
        from plotly.subplots import make_subplots  # noqa: PLC0415
    except ImportError as e:  # pragma: no cover
        raise ImportError(
            "plotly required for HTML reports. pip install plotly "
            "(or pip install 'sipstress[viz]')"
        ) from e
    return go, pio, make_subplots


def write(report: Dict, path: str) -> None:
    doc = build_html_dashboard(report)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(doc)


def _slug(title: str) -> str:
    out: List[str] = []
    for ch in title.lower():
        if ch.isalnum():
            out.append(ch)
        elif out and out[-1] != "-":
            out.append("-")
    s = "".join(out).strip("-")
    return s[:72] or "chart"


def _section(title: str, body_html: str, chart_html: str) -> str:
    hid = f"sipstress-chart-{_slug(title)}"
    te = html_std.escape(title)
    return (
        f'<section class="section chart-panel" id="{hid}" '
        f'aria-labelledby="{hid}-h">\n'
        f'  <h3 id="{hid}-h">{te}</h3>\n'
        f"  <p class=\"prose\">{body_html}</p>\n"
        f'  <div class="chart-host">{chart_html}</div>\n</section>'
    )


def _fig_div(fig: Any, pio: Any) -> str:
    return pio.to_html(fig, include_plotlyjs=False, full_html=False)


def _layout(title: str) -> Dict[str, Any]:
    return dict(
        title=dict(text=f"<b>{html_std.escape(title)}</b>", font=dict(color="#e2e8f0", size=16)),
        paper_bgcolor="#111c2e",
        plot_bgcolor="#0b1220",
        font=dict(color="#cbd5e1", family="Inter, sans-serif"),
        margin=dict(l=52, r=28, t=76, b=48),
        height=360,
        showlegend=True,
        legend=dict(orientation="h", yanchor="bottom", y=1.06),
    )


def _sip_codes(go: Any, pio: Any, codes: Dict[str, int]) -> str:
    if not codes:
        codes = {"(none)": 0}
    xs = sorted(codes.keys(), key=lambda k: (-codes[k], str(k)))
    ys = [int(codes[k]) for k in xs]
    fig = go.Figure(go.Bar(x=xs, y=ys, marker_color="#38bdf8"))
    fig.update_layout(**_layout("SIP responses"))
    fig.update_layout(xaxis_title="response code")
    fig.update_layout(yaxis_title="count")
    return _fig_div(fig, pio)


def _call_outcomes(go: Any, pio: Any, calls: Dict[str, Any]) -> str:
    labels = ["succeeded", "failed", "timed_out", "still open (inflight)"]
    ys = [
        int(calls.get("succeeded") or 0),
        int(calls.get("failed") or 0),
        int(calls.get("timed_out") or 0),
        max(0, int(calls.get("attempted") or 0) - int(calls.get("succeeded") or 0) - int(calls.get("failed") or 0) - int(calls.get("timed_out") or 0)),
    ]
    colors = ["#34d399", "#f87171", "#fbbf24", "#64748b"]
    fig = go.Figure(go.Bar(x=labels, y=ys, marker_color=colors))
    fig.update_layout(**_layout("Call outcomes"))
    fig.update_layout(yaxis_title="count")
    return _fig_div(fig, pio)


def _latency(go: Any, pio: Any, lat_ms: Dict[str, Any], thresh: Dict[str, Any]) -> str:
    setup = lat_ms.get("setup") or {}
    ans = lat_ms.get("answer") or {}
    labels = []
    ys = []
    for name, blk in ("setup→first provisional", setup), ("INVITE→200 OK", ans):
        m = blk.get("mean") if blk else None
        if isinstance(m, (int, float)):
            labels.append(name)
            ys.append(max(0.0, float(m)))

    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            x=labels or ["(none)"],
            y=ys if ys else [0],
            marker_color="#a78bfa",
        )
    )
    warn = float(thresh.get("answer_p99_warn_ms") or 1000)
    fig.add_shape(
        type="line",
        x0=-0.5,
        x1=len(labels) - 0.5 if labels else 0.5,
        y0=warn,
        y1=warn,
        line=dict(color="#fbbf24", dash="dash"),
    )
    fig.update_layout(**_layout("Latency (milliseconds)"))
    fig.update_layout(yaxis_title="ms (approx. mean)")
    return _fig_div(fig, pio)


def _media(
    go: Any, pio: Any, make_subplots_fn: Any, media: Dict[str, Any], thresh: Dict[str, Any]
) -> str:
    jm = media.get("jitter_ms") or {}
    lf = media.get("loss_ratio") or {}
    j = jm.get("mean")
    l = lf.get("mean")
    jitter = float(j) if isinstance(j, (int, float)) else 0.0
    loss_pct = float(l) * 100.0 if isinstance(l, (int, float)) else 0.0
    jf = float(thresh.get("jitter_avg_fail_ms") or 80)
    lff = float(thresh.get("rtp_loss_fail") or 0.05) * 100.0
    titles = ["Jitter avg (ms)", "Packet loss avg (%)"]
    fig = make_subplots_fn(rows=1, cols=2, subplot_titles=titles, horizontal_spacing=0.12)

    jc = "#f87171" if jitter >= jf else "#34d399"
    lc = "#f87171" if loss_pct >= lff else "#34d399"

    fig.add_trace(go.Bar(x=["this run"], y=[jitter], marker_color=[jc]), row=1, col=1)
    fig.add_trace(go.Bar(x=["this run"], y=[loss_pct], marker_color=[lc]), row=1, col=2)

    shared = dict(
        paper_bgcolor="#111c2e",
        plot_bgcolor="#0b1220",
        font=dict(color="#cbd5e1"),
    )
    fig.update_layout(
        title=dict(text="<b>RTP stability (aggregate)</b>", font=dict(color="#e2e8f0", size=16)),
        margin=dict(l=52, r=36, t=94, b=52),
        height=340,
        showlegend=False,
        **shared,
    )
    return _fig_div(fig, pio)


def _rtp_packets(go: Any, pio: Any, media: Dict[str, Any]) -> str:
    sent = int(media.get("packets_sent") or 0)
    recv = int(media.get("packets_recv") or 0)
    fig = go.Figure(go.Bar(x=["sent toward peer", "recv from peer"], y=[sent, recv], marker_color=["#6366f1", "#06b6d4"]))
    fig.update_layout(**_layout("Voice packets counted"))
    return _fig_div(fig, pio)


def _figure_first_call_packet_balance(go: Any, pio: Any, rec0: Dict) -> str:
    rtp = rec0.get("rtp") if isinstance(rec0.get("rtp"), dict) else {}
    sent = int(rtp.get("packets_sent") or 0)
    recv = int(rtp.get("packets_recv") or 0)
    if sent <= 0 and recv <= 0:
        return ""
    emq = rtp.get("extended_media_quality") if isinstance(rtp.get("extended_media_quality"), dict) else {}
    asym = emq.get("packet_asymmetry_recv_over_sent_approx")
    asym_note = ""
    if isinstance(asym, (int, float)):
        asym_note = f"recv÷sent ratio ≈ {float(asym):.2f} (see below)."

    fig = go.Figure(
        go.Bar(
            x=["packets sent<br>(toward peers)", "packets recv<br>(from peers)"],
            y=[sent, recv],
            marker_color=["#6366f1", "#06b6d4"],
            text=[sent, recv],
            textposition="outside",
        )
    )
    fig.update_layout(**_layout("This dialog — RTP packet totals"))
    fig.update_layout(
        annotations=[
            dict(
                xref="paper",
                yref="paper",
                x=0,
                y=1.1,
                showarrow=False,
                text=(
                    "<i>Only the first call in the report (<code>call_records[0]</code>). "
                    "Unequal bars are normal — one side may send less audio or stop RTP sooner.</i>"
                ),
                font=dict(color="#94a3b8", size=11),
                align="left",
            ),
        ]
    )
    if asym_note:
        fig.add_annotation(
            xref="paper",
            yref="paper",
            x=0.5,
            y=-0.18,
            showarrow=False,
            text=f"<i>{html_std.escape(asym_note)}</i>",
            font=dict(color="#94a3b8", size=11),
            align="center",
        )

    fig.update_layout(yaxis_title="packet count")
    return _fig_div(fig, pio)


def _figure_per_call_rtp_health(go: Any, pio: Any, rec0: Dict) -> str:
    rtp = rec0.get("rtp") if isinstance(rec0.get("rtp"), dict) else {}
    jitter = rtp.get("jitter_ms")
    lr = rtp.get("loss_ratio")
    if not isinstance(jitter, (int, float)) and not isinstance(lr, (int, float)):
        return ""
    jv = float(jitter) if isinstance(jitter, (int, float)) else 0.0
    lv = float(lr) * 100.0 if isinstance(lr, (int, float)) else 0.0

    fig = go.Figure(
        data=[
            go.Bar(
                x=["jitter (ms)", "loss (% of stream)"],
                y=[jv, lv],
                marker_color=["#a78bfa", "#fb7185"],
                text=[f"{jv:.2f}", f"{lv:.4f}"],
                textposition="outside",
            ),
        ]
    )
    fig.update_layout(**_layout("This call — RTP averages"))
    fig.update_layout(
        yaxis_title="see axis labels",
        annotations=[
            dict(
                xref="paper",
                yref="paper",
                x=0,
                y=1.1,
                showarrow=False,
                text=(
                    "<i>For this call only — from <code>call_records[0].rtp</code> "
                    "(director totals can differ slightly).</i>"
                ),
                font=dict(color="#94a3b8", size=11),
                align="left",
            ),
        ],
    )
    return _fig_div(fig, pio)


def _figure_scenario_milestone_timeline(go: Any, pio: Any, rec0: Dict) -> str:
    sp = rec0.get("scenario_profile") if isinstance(rec0.get("scenario_profile"), dict) else {}
    tm = sp.get("timings_relative_call_start") if isinstance(sp.get("timings_relative_call_start"), dict) else {}
    if not tm:
        return ""
    labels: List[str] = []
    ts: List[float] = []

    seq = (
        ("invite_sent_s", "INVITE sent"),
        ("rtp_started_s", "RTP starts (often early media)"),
        ("call_established_s", "Call established"),
    )
    for key, human in seq:
        v = tm.get(key)
        if isinstance(v, (int, float)):
            ts.append(float(v))
            labels.append(human)
    wall = tm.get("sip_invite_to_200_wall_s")
    ce_raw = tm.get("call_established_s")
    if isinstance(wall, (int, float)):
        redundant = isinstance(ce_raw, (int, float)) and abs(float(wall) - float(ce_raw)) < 0.01
        if not redundant:
            ts.append(float(wall))
            labels.append("INVITE → 200 wall clock")

    if len(ts) < 2:
        return ""

    pairs = sorted(zip(ts, labels), key=lambda p: (p[0], p[1]))
    ts_s = [p[0] for p in pairs]
    lbl_s = [p[1] for p in pairs]
    yi = list(range(len(ts_s)))

    fig = go.Figure(
        go.Scatter(
            x=ts_s,
            y=yi,
            mode="lines+markers",
            line=dict(color="#64748b", width=2),
            marker=dict(size=14, color="#38bdf8", line=dict(color="#0ea5e9", width=1)),
            customdata=lbl_s,
            hovertemplate="%{customdata}<br>t = %{x:.3f}s (from call start)<extra></extra>",
        )
    )
    fig.update_layout(**_layout("Call milestones (seconds from leg start)"))
    fig.update_layout(
        xaxis=dict(title="seconds"),
        yaxis=dict(
            tickmode="array",
            tickvals=yi,
            ticktext=lbl_s,
            title=None,
            autorange="reversed",
        ),
        annotations=[
            dict(
                xref="paper",
                yref="paper",
                x=0,
                y=1.08,
                showarrow=False,
                text=(
                    "<i>Times from <code>scenario_profile.timings_relative_call_start</code> "
                    "(seconds after leg start; earlier is left).</i>"
                ),
                font=dict(color="#94a3b8", size=11),
                align="left",
            ),
        ],
    )
    fig.update_layout(height=max(340, len(ts_s) * 54 + 120))
    return _fig_div(fig, pio)


def _timeline(go: Any, pio: Any, events: Sequence[Dict[str, Any]]) -> str:
    kinds = []
    ts = []
    for ev in sorted(events or [], key=lambda e: float(e.get("t") or 0)):
        k = str(ev.get("kind") or "?")
        t = float(ev.get("t") or 0)
        kinds.append(k)
        ts.append(t)

    fig = go.Figure(
        go.Scatter(
            x=ts,
            y=list(range(len(ts))),
            mode="markers+lines",
            line=dict(color="#94a3b8"),
            marker=dict(color="#38bdf8", size=11),
            customdata=list(kinds),
            hovertemplate="t=%{x:.2f}s · %{customdata}<extra></extra>",
        )
    )
    fig.update_layout(**_layout("First-call events timeline"))
    fig.update_layout(yaxis=dict(showticklabels=False, title=None), xaxis=dict(title="seconds"))
    fig.update_layout(height=max(320, len(ts) * 18 + 100))
    return _fig_div(fig, pio)


def _extended_audio(go: Any, pio: Any, call_recs: List[Dict]) -> Optional[str]:
    if not call_recs:
        return None
    emq = _first_extended_media_quality(call_recs)
    inl = emq.get("inbound_from_remote") or {}
    sr = inl.get("silence_ratio")
    if not isinstance(sr, (int, float)):
        return None
    ap = inl.get("active_ratio")
    act = float(ap) if isinstance(ap, (int, float)) else max(0.0, min(1.0, 1.0 - float(sr)))
    fig = go.Figure(
        data=[
            go.Bar(
                x=["silent / calm", "lively-ish"],
                y=[float(sr) * 100.0, act * 100.0],
                marker_color=["#94a3b8", "#16a34a"],
            ),
        ]
    )
    fig.update_layout(**_layout("Inbound direction heuristic mix"))
    fig.update_layout(yaxis=dict(title="% of window"))
    fig.update_layout(
        annotations=[
            dict(
                xref="paper",
                yref="paper",
                x=0,
                y=1.2,
                showarrow=False,
                text=(
                    "<i>incoming RTP only — prompts and hold can look quiet</i>"
                ),
                font=dict(color="#94a3b8", size=11),
                align="left",
            ),
        ]
    )
    return _fig_div(fig, pio)


def _hero_pdf_actions(report: Dict) -> str:
    dash = report.get("_dashboard") or {}
    bn = dash.get("pdf_basename") or ""
    pdf_run = bool(dash.get("pdf_from_this_run"))
    chunks: List[str] = []
    if bn:
        bn_e = html_std.escape(str(bn))
        tip = html_std.escape(
            "Exported in the same sipstress invocation (--pdf-out)."
            if pdf_run
            else (
                "Needs a real file beside this HTML, or regenerate with sipstress-html2pdf / --pdf-out."
            )
        )
        chunks.append(
            f'<a class="btn btn-primary" href="{bn_e}" download="{bn_e}" title="{tip}">'
            f"Download PDF ({bn_e})</a>"
        )
    chunks.append(
        '<button type="button" class="btn btn-outline" onclick="window.print()">'
        "Print / save as PDF</button>"
    )
    return '<div class="report-actions">\n      ' + "\n      ".join(chunks) + "\n    </div>"


def _answer_pickup_line(rec: Dict) -> str:
    evs = rec.get("events") or []
    kinds = [str(e.get("kind") or "") for e in evs]
    if any(k.startswith("invite_reject") for k in kinds) or "invite_reject" in kinds:
        return "INVITE was rejected (<code>invite_reject</code>) — callee side did not complete a successful answer SIP-wise."
    if "invite_timeout" in kinds:
        return (
            "<code>invite_timeout</code> — signalling never reached a steady answered state "
            "within SIP timers."
        )
    if "call_established" in kinds:
        au = ""
        sip_steps = [(s.get("sip") or {}) for s in (rec.get("plan") or {}).get("steps") or []]
        if any(part.get("answered_during_step") for part in sip_steps):
            au = " A plan step explicitly saw <strong>answered_during_step</strong>."
        return (
            "Far-end signalling reached an answered / established checkpoint "
            "(<code>call_established</code> event in the SIP scenario)."
            + au
        )
    if rec.get("early_media_rtp"):
        return (
            "Early media RTP was seen, but no <code>call_established</code> event was recorded — "
            "confirm with PCAP / platform whether a 200 OK was sent."
        )
    if rec.get("success") is True:
        return "Scenario finished with <code>success=true</code> — treat as soft OK if events look thin."
    if rec.get("success") is False:
        return "<code>success=false</code> on the call record — review failures and health findings."
    return "Not enough events to classify pickup — open JSON <code>call_records[0].events</code>."


def _final_status_line(rec: Dict) -> str:
    fst = rec.get("final_status")
    if fst is None:
        return "—"
    if isinstance(fst, (int, float)):
        return str(int(fst))
    return html_std.escape(str(fst))


def _plan_step_label(s: Dict) -> str:
    n = str(s.get("name") or s.get("step_id") or "?")
    return n if len(n) <= 44 else n[:41] + "…"


def _figure_plan_durations(go: Any, pio: Any, steps: Sequence[Dict]) -> str:
    if not steps:
        return ""
    labels = [_plan_step_label(s) for s in steps]
    xs = [max(0.0, float(s.get("duration_s") or 0.0)) for s in steps]
    colors = []
    pallet = {"OK": "#34d399", "WARN": "#fbbf24", "FAIL": "#f87171", "SKIP": "#64748b"}
    for s in steps:
        v = str(s.get("verdict") or "SKIP").upper()
        colors.append(pallet.get(v, "#94a3b8"))
    fig = go.Figure(go.Bar(y=labels, x=xs, orientation="h", marker_color=colors))
    fig.update_layout(**_layout("PV3 compos — wall clock per step"))
    fig.update_layout(xaxis_title="seconds", yaxis=dict(autorange="reversed"))
    fig.update_layout(height=max(360, 48 + len(steps) * 36), showlegend=False)
    return _fig_div(fig, pio)


def _figure_plan_rms(go: Any, pio: Any, steps: Sequence[Dict]) -> str:
    labels: List[str] = []
    rms: List[float] = []
    colors: List[str] = []
    pallet = {"OK": "#34d399", "WARN": "#fbbf24", "FAIL": "#f87171", "SKIP": "#64748b"}
    for s in steps:
        audio = s.get("audio") or {}
        raw = audio.get("rms_avg")
        if not isinstance(raw, (int, float)):
            continue
        labels.append(_plan_step_label(s))
        rms.append(float(raw))
        v = str(s.get("verdict") or "SKIP").upper()
        colors.append(pallet.get(v, "#94a3b8"))
    if not labels:
        return ""
    fig = go.Figure(go.Bar(x=labels, y=rms, marker_color=colors))
    fig.update_layout(**_layout("PV3 compos — prompt RMS (heuristic)"))
    fig.update_layout(yaxis_title="avg RMS (codec-dependent scale)")
    fig.update_layout(xaxis=dict(tickangle=-28))
    fig.update_layout(height=max(320, 120 + len(labels) * 14), showlegend=False)
    return _fig_div(fig, pio)


def _figure_compos_counts(go: Any, pio: Any, steps: Sequence[Dict]) -> str:
    from collections import Counter  # noqa: PLC0415

    c = Counter(str(s.get("step_type") or "?") for s in steps)
    if not c:
        return ""
    pairs = sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    fig = go.Figure(go.Bar(x=xs, y=ys, marker_color="#38bdf8"))
    fig.update_layout(**_layout("PV3 step types in this call"))
    fig.update_layout(yaxis_title="count", xaxis=dict(tickangle=-24))
    return _fig_div(fig, pio)


def _html_plan_table(steps: Sequence[Dict]) -> str:
    rows: List[str] = []
    for s in steps:
        st = str(s.get("step_type") or "")
        pv3 = html_std.escape(STEP_TYPE_PV3_BLURB.get(st, "Custom / extended compos"))
        verdict = str(s.get("verdict") or "")
        vcls = f"verdict-{verdict.lower()}" if verdict else ""
        xfer = s.get("expected_transfer_to")
        xfer_c = html_std.escape(str(xfer)) if xfer else "—"
        ans = (s.get("sip") or {}).get("answered_during_step")
        ans_s = "yes" if ans else ("no" if ans is False else "—")
        audio = s.get("audio") or {}
        onset = audio.get("onset_offset_s")
        onset_s = f"{float(onset):.2f}s" if isinstance(onset, (int, float)) else "—"
        rms = audio.get("rms_avg")
        rms_s = f"{float(rms):.0f}" if isinstance(rms, (int, float)) else "—"
        dtmf = ", ".join((s.get("dtmf") or {}).get("sent") or []) or "—"
        rows.append(
            "<tr class=\"{vc}\">"
            "<td><code>{sid}</code></td>"
            "<td>{name}</td>"
            "<td><code>{st}</code><br><span class=\"muted\" style=\"font-size:.78rem\">{pv3}</span></td>"
            "<td><strong>{ver}</strong></td>"
            "<td>{dur}</td>"
            "<td>{xfer}</td>"
            "<td>{ans}</td>"
            "<td>{onset}</td>"
            "<td>{rms}</td>"
            "<td><code>{dtmf}</code></td>"
            "</tr>".format(
                vc=vcls,
                sid=html_std.escape(str(s.get("step_id") or "")),
                name=html_std.escape(_plan_step_label(s)),
                st=html_std.escape(st),
                pv3=pv3,
                ver=html_std.escape(verdict),
                dur=f"{float(s.get('duration_s') or 0):.2f}s",
                xfer=xfer_c,
                ans=html_std.escape(ans_s),
                onset=html_std.escape(onset_s),
                rms=html_std.escape(rms_s),
                dtmf=html_std.escape(dtmf),
            )
        )
    body = "\n".join(rows)
    return (
        '<div class="plan-table-wrap">\n'
        '  <table class="plan-steps">\n'
        "    <thead><tr>"
        "<th>id</th><th>name</th><th>type</th><th>verdict</th><th>duration</th>"
        "<th>mise en relation →</th><th>answered in step</th>"
        "<th>prompt onset</th><th>RMS</th><th>DTMF sent</th>"
        "</tr></thead>\n"
        f"    <tbody>\n{body}\n    </tbody>\n"
        "  </table>\n"
        "</div>"
    )


def _html_scenario_timings_only(rec0: Dict) -> str:
    """Milestone table only; empty when no ``scenario_profile`` timings."""
    sp = rec0.get("scenario_profile") if isinstance(rec0.get("scenario_profile"), dict) else None
    if not sp:
        return ""
    tm = (
        sp.get("timings_relative_call_start")
        if isinstance(sp.get("timings_relative_call_start"), dict)
        else {}
    )
    if not tm:
        return ""
    nice = (
        ("invite_sent_s", "INVITE sent"),
        ("rtp_started_s", "RTP starts (often 183 early media)"),
        ("call_established_s", "Call established"),
        ("sip_invite_to_200_wall_s", "INVITE → 200 wall clock"),
    )
    rows: List[str] = []
    for k, cap in nice:
        v = tm.get(k)
        if isinstance(v, (int, float)):
            rows.append(
                "<tr><td>{}</td><td><code>{:.3f}s</code></td></tr>".format(
                    html_std.escape(cap),
                    float(v),
                )
            )
    if not rows:
        return ""

    intro = (
        '      <p class="prose muted" style="margin:0 0 .5rem">\n'
        "        Seconds from the start of this call leg "
        "(<code>scenario_profile.timings_relative_call_start</code> in JSON).\n"
        "      </p>\n"
    )
    tbl = (
        '      <table class="cli" style="max-width:40rem"><thead>'
        '<tr><th>Milestone</th><th>Time</th></tr></thead><tbody>\n        '
        + "\n        ".join(rows)
        + "\n      </tbody></table>\n"
    )
    return intro + tbl


def _html_session_pv3_section(rec0: Dict) -> str:
    plan = rec0.get("plan") if isinstance(rec0.get("plan"), dict) else None

    if not plan:
        inner = _html_scenario_timings_only(rec0)
        if not inner:
            return ""
        return (
            '    <section id="sipstress-pv3" class="report-region" aria-labelledby="sipstress-pv3-heading">\n'
            '      <h2 id="sipstress-pv3-heading">Scenario timings</h2>\n'
            f"{inner}"
            "    </section>\n"
        )

    vc = plan.get("verdict_counts") or {}
    vline = ", ".join(f"{html_std.escape(str(k))}: {int(v)}" for k, v in sorted(vc.items()))
    completed = bool(plan.get("completed"))
    title = html_std.escape(str(plan.get("name") or "unnamed plan"))
    desc = html_std.escape(str(plan.get("description") or ""))
    tbl = _html_plan_table(plan.get("steps") or [])

    return (
        '    <section id="sipstress-pv3" class="report-region" aria-labelledby="sipstress-pv3-heading">\n'
        '      <h2 id="sipstress-pv3-heading">IVR / test plan</h2>\n'
        f'      <p class="prose"><strong>Plan</strong>: <code>{title}</code> — '
        f'{"all steps reached a non-FAIL verdict" if completed else "plan did not fully complete"}.</p>\n'
        f'      <p class="prose muted" style="margin-top:-.5rem">{desc}</p>\n'
        f'      <p class="prose">Step verdict tally: {vline or "—"}</p>\n'
        f"      {tbl}\n"
        "    </section>\n"
    )


def _outbound_audio_note(rec: Dict) -> Optional[str]:
    """One line about our transmit path when JSON has real outbound fields (no fake defaults)."""
    emq = (rec.get("rtp") or {}).get("extended_media_quality") if rec else None
    if not isinstance(emq, dict):
        return None
    outbound = emq.get("outbound_toward_remote") or {}
    if not isinstance(outbound, dict):
        return None
    avg = outbound.get("tx_rms_proxy_avg")
    noise = outbound.get("tx_noise_floor_approx")
    if isinstance(avg, (int, float)) and isinstance(noise, (int, float)):
        aq, nq = float(avg), float(noise)
        if aq <= nq * 1.02:
            return "Toward the network, our send path looked very quiet next to the estimated noise floor."
        if aq < nq * 1.4:
            return "Toward the network, our send path mixed pauses and speech-like levels."
        return "Toward the network, our send path mostly looked like active audio."

    sr = outbound.get("silence_ratio_share")
    if isinstance(sr, (int, float)):
        return f"Outbound silence share in the report: {float(sr) * 100:.0f}%."
    return None


def _first_extended_media_quality(call_recs: Sequence[Dict]) -> Dict[str, Any]:
    for r in call_recs:
        rtp = r.get("rtp") or {}
        emq = rtp.get("extended_media_quality")
        if isinstance(emq, dict):
            return emq
    return {}


def _kpi_cards(
    media: Dict,
    lat_ms: Dict,
    calls: Dict,
    thresh: Dict,
    call_records: Sequence[Dict],
) -> str:
    j_hist = media.get("jitter_ms") or {}
    lr_hist = media.get("loss_ratio") or {}
    jm = j_hist.get("mean")
    j_ms_f = float(jm) if isinstance(jm, (int, float)) else None
    lm = lr_hist.get("mean")
    lr_f = float(lm) if isinstance(lm, (int, float)) else None
    jw = float(thresh.get("jitter_avg_warn_ms") or 30)
    jf = float(thresh.get("jitter_avg_fail_ms") or 80)
    lrw = float(thresh.get("rtp_loss_warn") or 0.01)
    lrf = float(thresh.get("rtp_loss_fail") or 0.05)

    j_cls = "neutral"
    j_val = "—"
    if j_ms_f is not None:
        if j_ms_f >= jf:
            j_cls = "bad"
            j_val = f"{j_ms_f:.2f} ms (≥ fail {jf:.0f})"
        elif j_ms_f >= jw:
            j_cls = "warn"
            j_val = f"{j_ms_f:.2f} ms (≥ warn {jw:.0f})"
        else:
            j_cls = "good"
            j_val = f"{j_ms_f:.2f} ms"

    loss_cls = "neutral"
    l_txt = "—"
    if lr_f is not None:
        p = lr_f * 100
        if lr_f >= lrf:
            loss_cls = "bad"
            l_txt = f"{p:.4f}% (≥ fail {lrf*100:.0f}%)"
        elif lr_f >= lrw:
            loss_cls = "warn"
            l_txt = f"{p:.4f}% (≥ warn {lrw*100:.0f}%)"
        else:
            loss_cls = "good"
            l_txt = f"{p:.4f}%"

    ans_mean = (lat_ms.get("answer") or {}).get("mean")
    ans_s = f"{float(ans_mean):.0f} ms" if isinstance(ans_mean, (int, float)) else "—"
    ratio = calls.get("success_ratio")
    ratio_s = f"{ratio:.1%}" if isinstance(ratio, (int, float)) else "—"
    early = "—"
    if call_records:
        early = "yes" if call_records[0].get("early_media_rtp") else "no"

    def card(css: str, label: str, value: str, hint: str) -> str:
        return (
            f'<div class="kpi {css}"><div class="kpi-label">{html_std.escape(label)}</div>'
            f'<div class="kpi-value">{html_std.escape(value)}</div>'
            f'<div class="kpi-hint">{html_std.escape(hint)}</div></div>'
        )

    rows = "\n".join(
        (
            card(j_cls, "Voice timing wobble (avg.)", j_val or "—", "Lower is usually steadier sound transport."),
            card(loss_cls, "Missing voice pieces (avg.)", l_txt or "—", "Lower is fewer gaps in the stream."),
            card("neutral", "Time until answered", ans_s, "From dial request to call picked up."),
            card("neutral", "Share that worked", ratio_s, "Succeeded ÷ attempted for this run."),
            card("neutral", "Early media", early.title(), "Ringback or voice before formal answer."),
        )
    )
    return f"<div class=\"kpi-grid\">\n{rows}\n    </div>"


def _html_signalling_panel(
    media: Dict, sip_blk: Dict, lat_ms: Dict, calls: Dict, thresh: Dict
) -> str:
    j_ms = media.get("jitter_ms") or {}
    lf = media.get("loss_ratio") or {}
    jmf = j_ms.get("mean")
    lfm = lf.get("mean")

    js = float(jmf) if isinstance(jmf, (int, float)) else None
    ls = float(lfm) if isinstance(lfm, (int, float)) else None

    jw, jf = float(thresh.get("jitter_avg_warn_ms") or 30), float(thresh.get("jitter_avg_fail_ms") or 80)

    cards: List[Tuple[str, str]]

    # Short digest lines (matching prior dashboard wording style)
    if js is None:
        j_interp = "No aggregate jitter in this aggregate."
    elif js >= jf:
        j_interp = f"High jitter ({js:.1f} ms …) inspect path toward media."
    elif js >= jw:
        j_interp = f"Elevated jitter ({js:.1f} ms)."
    else:
        j_interp = f"Jitter low for this sample ({js:.2f} ms)."

    if ls is None:
        l_interp = "No aggregate loss."
    elif ls >= float(thresh.get("rtp_loss_fail") or 0.05):
        l_interp = f"High packet loss (~{ls*100:.2f}% …)."
    else:
        l_interp = f"Packet loss ~{ls*100:.4f}%."

    setup_m = (lat_ms.get("setup") or {}).get("mean")
    ans_m = (lat_ms.get("answer") or {}).get("mean")
    setup_s = f"{float(setup_m):.0f}" if isinstance(setup_m, (int, float)) else "—"
    ans_s = f"{float(ans_m):.0f}" if isinstance(ans_m, (int, float)) else "—"

    latency_line = (
        f"Mean setup {setup_s} ms (first 1xx) and answer {ans_s} ms (to 200)."
    )

    att = int((calls or {}).get("attempted") or 0)
    to_n = int((calls or {}).get("timed_out") or 0)
    if att and to_n:
        timeouts = f"{to_n} timeout(s) on {att} attempts check signalling path."
    else:
        timeouts = "Timeouts: none counted (or counters empty)."

    rsp = int((sip_blk or {}).get("responses_recv") or 0)
    rtx = int((sip_blk or {}).get("retransmissions") or 0)
    retr = (
        "No SIP retransmit duplicates seen."
        if rtx == 0
        else f"{rtx} duplicate responses vs {rsp} responses possible UDP loss."
    )

    fails = (sip_blk or {}).get("failure_reasons") or {}
    fail_items = sorted(((str(k), int(v)) for k, v in fails.items() if v), key=lambda x: -x[1])[:5]
    if fail_items:
        fail_str = "; ".join(f"{html_std.escape(k)} × {v}" for k, v in fail_items)
        f_line = "SIP buckets: " + fail_str + "."
    else:
        f_line = "No failure_reasons buckets flagged."

    anomaly_n = 0

    cards = [
        ("Packet timing (jitter)", j_interp),
        ("Missing frames (loss)", l_interp),
        ("Signalling latency", latency_line),
        ("SIP timeouts", timeouts),
        ("SIP retransmissions", retr),
        ("SIP failure buckets", f_line),
        ("Hints", f"No director anomaly ledger entries counted here ({anomaly_n})."),
    ]

    inner = "\n".join(
        f'      <div class="diag-card"><h4>{html_std.escape(tt)}</h4>'
        f'<p class="diag-body">{html_std.escape(bb)}</p></div>'
        for tt, bb in cards
    )
    return f'<div class="diag-grid" role="region" aria-label="Call diagnostics">\n{inner}\n    </div>'


def _html_silence_panel(call_recs: Sequence[Dict]) -> str:
    if not call_recs:
        return ""
    emq = _first_extended_media_quality(call_recs)
    if not emq:
        return ""

    inl = emq.get("inbound_from_remote") or {}

    sr = inl.get("silence_ratio")
    win = float(w) if isinstance((w := emq.get("window_monotonic_span_s")), (int, float)) else None
    inbound_line: Optional[str] = None
    if isinstance(sr, (int, float)) and win and win > 0:
        quiet_s = float(sr) * win
        inbound_line = (
            f"Roughly <strong>{quiet_s:.1f} s</strong> looked very quiet "
            f"({float(sr) * 100:.1f}% of the {win:.1f} s RTP window analysed). Hold music or long gaps can increase this."
        )

    outbound = _outbound_audio_note(call_recs[0])

    return (
        '<section class="section silence-section" aria-labelledby="sipstress-silence-heading">\n'
        '  <h3 id="sipstress-silence-heading">Quiet time on the call</h3>\n'
        "  <p class=\"prose\">\n"
        "    Sipstress scans <strong>inbound</strong> RTP for low-energy stretches. "
        "That is automatic level detection, not a perceptual score.\n"
        "  </p>\n"
        + (
            f'  <p class="prose">{inbound_line}</p>\n'
            if inbound_line
            else '  <p class="prose muted">No inbound silence ratio on this record.</p>\n'
        )
        + (f'  <p class="prose">{html_std.escape(outbound)}</p>\n' if outbound else "")
        + "</section>"
    )


def _story_lines(report: Dict) -> Tuple[bool, List[str]]:
    health = report.get("health") or {}
    pf = str(health.get("pass_fail") or "PASS")
    verdict = str(health.get("overall_verdict") or "OK").upper()
    lines: List[str] = []

    dirs = report.get("directors") or []
    ok = True
    if dirs:
        r0 = (dirs[0].get("call_records") or [{}])[0]
        suc = r0.get("success")
        if isinstance(suc, bool):
            lines.append(f"Call completed successfully (this record): {'yes' if suc else 'no'}.")
            ok = suc
        sr = dirs[0].get("calls", {}).get("success_ratio")
        if sr is None and dirs[0].get("calls"):
            pass
        if isinstance(sr, (int, float)):
            lines.append(f"Success ratio in aggregate: {float(sr):.0%}.")
    fatal = verdict == "FAIL" or pf == "FAIL"

    lines.append("<strong>Health pass_fail</strong> is " + html_std.escape(str(pf)) + ".")
    if fatal:
        lines.append("<strong>Automatic check flagged FAIL</strong> — see findings below.")

    aggregate_ok = (not fatal) and ok
    return aggregate_ok, lines


def _short_call_id(call_id: str, max_len: int = 42) -> str:
    c = str(call_id or "")
    if len(c) <= max_len:
        return c
    return c[: max_len - 1] + "…"


def _call_outcome_pill(rec: Dict) -> Tuple[str, str]:
    """(pill_css_class, label) for result column — pill.ok | pill.fail | neutral."""
    if rec.get("success") is True:
        return "ok", "ok"
    if rec.get("success") is False:
        fr = str(rec.get("failure_reason") or "")
        if "timeout" in fr.lower():
            return "fail", "timeout"
        return "fail", "no"
    return "", "—"


def _html_call_records_table(call_recs: Sequence[Dict]) -> str:
    """Scrollable table: one row per entry in ``call_records``."""
    header = (
        "<thead><tr>"
        "<th>#</th><th>Call-ID</th><th>Result</th><th>Duration (s)</th>"
        "<th><code>final_status</code></th><th><code>failure_reason</code></th>"
        "<th>Jitter (ms)</th><th>Loss</th><th>RTP pkts sent/recv</th>"
        "</tr></thead>\n"
    )
    rows: List[str] = []
    for i, rec in enumerate(call_recs, start=1):
        cid_full = str(rec.get("call_id") or "—")
        cid_s = html_std.escape(_short_call_id(cid_full))
        pcls, plab = _call_outcome_pill(rec)
        if pcls:
            res_cell = f'<span class="pill {pcls}">{html_std.escape(plab)}</span>'
        else:
            res_cell = f'<span class="muted">{html_std.escape(plab)}</span>'
        dur = rec.get("duration_s")
        dur_s = f"{float(dur):.2f}" if isinstance(dur, (int, float)) else "—"
        fst = rec.get("final_status")
        if fst is None:
            fst_s = "—"
        elif isinstance(fst, (int, float)):
            fst_s = str(int(fst))
        else:
            fst_s = html_std.escape(str(fst))
        fraw = rec.get("failure_reason")
        fr = html_std.escape(str(fraw)) if fraw not in (None, "") else "—"
        rtp = rec.get("rtp") if isinstance(rec.get("rtp"), dict) else {}
        j = rtp.get("jitter_ms")
        jt = f"{float(j):.2f}" if isinstance(j, (int, float)) else "—"
        lr = rtp.get("loss_ratio")
        lt = f"{float(lr) * 100:.2f}%" if isinstance(lr, (int, float)) else "—"
        ps = int(rtp.get("packets_sent") or 0)
        prx = int(rtp.get("packets_recv") or 0)
        pkt = f"{ps}/{prx}"
        rows.append(
            f'<tr><td>{i}</td><td title="{html_std.escape(cid_full)}">{cid_s}</td><td>{res_cell}</td>'
            f"<td><code>{html_std.escape(dur_s)}</code></td><td><code>{fst_s}</code></td>"
            f"<td>{fr}</td><td>{html_std.escape(jt)}</td><td>{html_std.escape(lt)}</td>"
            f"<td><code>{html_std.escape(pkt)}</code></td></tr>"
        )
    body = "\n".join(rows)
    return (
        '<div class="plan-table-wrap">\n'
        f'  <table class="cli calls-all">\n    {header}'
        f"    <tbody>\n{body}\n    </tbody>\n"
        "  </table>\n"
        "</div>\n"
    )


def _figure_all_calls_duration(go: Any, pio: Any, call_recs: Sequence[Dict]) -> str:
    if not call_recs:
        return ""
    xs = [str(i) for i in range(1, len(call_recs) + 1)]
    ys: List[float] = []
    colors: List[str] = []
    cdata: List[Tuple[str, str, str]] = []
    for rec in call_recs:
        d = rec.get("duration_s")
        ys.append(float(d) if isinstance(d, (int, float)) else 0.0)
        if rec.get("success") is True:
            colors.append("#34d399")
        elif rec.get("success") is False:
            colors.append("#f87171")
        else:
            colors.append("#64748b")
        cdata.append(
            (
                str(rec.get("call_id") or ""),
                str(rec.get("failure_reason") or "—"),
                "yes" if rec.get("success") is True else ("no" if rec.get("success") is False else "—"),
            )
        )
    fig = go.Figure(
        go.Bar(
            x=xs,
            y=ys,
            marker_color=colors,
            customdata=cdata,
            hovertemplate=(
                "call_id=%{customdata[0]}<br>"
                "duration=%{y:.2f}s<br>"
                "success=%{customdata[2]}<br>"
                "failure_reason=%{customdata[1]}<extra></extra>"
            ),
        )
    )
    fig.update_layout(**_layout("Seconds per call (JSON order)"))
    fig.update_layout(xaxis_title="call # (order in report)", yaxis_title="seconds")
    fig.update_layout(height=max(320, min(560, 140 + len(call_recs) * 22)))
    return _fig_div(fig, pio)


def _build_all_calls_section(call_recs: List[Dict], go: Any, pio: Any) -> str:
    if not call_recs:
        return ""
    chart = _figure_all_calls_duration(go, pio, call_recs)
    chart_block = (
        '      <div class="section">\n'
        '        <h3>Duration by call order</h3>\n'
        '        <p class="prose">Same order as <code>call_records</code> in JSON. '
        "Green means that call finished with <code>success: true</code>; red means it did not.</p>\n"
        f'        <div class="chart-host">{chart}</div>\n'
        "      </div>\n"
    )
    table_block = (
        '      <div class="section" style="margin-top:1rem">\n'
        f'        <h3>Per-call numbers ({len(call_recs)} calls)</h3>\n'
        '        <p class="prose muted">RTP columns come from each call\'s own <code>rtp</code> object when present.</p>\n'
        f"        {_html_call_records_table(call_recs)}"
        "      </div>\n"
    )
    return (
        '    <section id="sipstress-all-calls" class="report-region"'
        ' aria-labelledby="sipstress-all-calls-heading">\n'
        '      <h2 id="sipstress-all-calls-heading">All calls in this run</h2>\n'
        f"{chart_block}{table_block}"
        "    </section>\n"
    )


def build_html_dashboard(report: Dict) -> str:
    go, pio, make_subplots = _require_plotly()

    health = report.get("health") or {}
    thresh = health.get("thresholds") or {}

    dirs = report.get("directors") or []
    d0 = dirs[0] if dirs else {}
    calls = d0.get("calls") or {}
    sip_blk = d0.get("sip") or {}
    lat_ms = d0.get("latency_ms") or {}
    media = d0.get("media") or {}

    call_recs: List[Dict] = []
    for d in dirs:
        call_recs.extend(d.get("call_records") or [])

    gt_ts = report.get("generated_at")
    when = (
        datetime.fromtimestamp(float(gt_ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        if isinstance(gt_ts, (int, float))
        else "?"
    )

    pf_banner, pf_css = _banner(health)

    kpis_html = ""

    diagnostics_html = ""

    charts: List[str] = []

    all_calls_html = ""

    if d0:
        kpis_html = (
            '<section id="sipstress-kpis" class="report-region" aria-label="Key metrics at a glance">\n'
            + _kpi_cards(media, lat_ms, calls, thresh, call_recs or [{}])
            + "\n</section>"
        )

        diagnostics_inner = (
            _html_signalling_panel(media, sip_blk, lat_ms, calls, thresh)
            + "\n"
            + _html_silence_panel(call_recs or [])
        )
        diagnostics_html = (
            '<section id="sipstress-diagnostics" class="report-region"'
            ' aria-labelledby="sipstress-diagnostics-heading">\n'
            '      <h2 id="sipstress-diagnostics-heading">Diagnostics &amp; call path</h2>\n'
            '      <p class="prose lead" style="max-width:85ch">\n'
            "        Structured explanation layer on top of the JSON metrics.\n"
            "      </p>\n"
            + diagnostics_inner
            + "\n    </section>"
        )

        all_calls_html = _build_all_calls_section(call_recs, go, pio) if call_recs else ""

        charts.extend(
            [
                _section(
                    "Replies from the phone network",
                    (
                        "Bar chart of SIP <code>response_codes</code> seen on the director leg"
                    ),
                    _sip_codes(go, pio, sip_blk.get("response_codes") or {}),
                ),
                _section(
                    "Did the calls go through?",
                    "Succeeded / failed / timed out totals from aggregated call counters.",
                    _call_outcomes(go, pio, calls),
                ),
                _section(
                    "Wait times (milliseconds)",
                    "Approximate SIP timing from stored latency samples.",
                    _latency(go, pio, lat_ms, thresh),
                ),
                _section(
                    "Stability of the voice stream",
                    "Jitter and loss aggregates from director media counters.",
                    _media(go, pio, make_subplots, media, thresh),
                ),
                _section(
                    "How much voice traffic moved",
                    "<code>packets_sent</code> vs <code>packets_recv</code> aggregates.",
                    _rtp_packets(go, pio, media),
                ),
            ]
        )
        if call_recs and call_recs[0].get("events"):
            charts.append(
                _section(
                    "Timeline of the first call",
                    "Events from <code>call_records[0].events</code> in time order.",
                    _timeline(go, pio, call_recs[0]["events"]),
                )
            )
        cr0 = call_recs[0] if call_recs else {}
        ms = _figure_scenario_milestone_timeline(go, pio, cr0)
        if ms:
            charts.append(
                _section(
                    "Scenario milestones (seconds from leg start)",
                    (
                        "Each point is a moment sipstress already recorded (seconds after this leg started). "
                        "The space between <em>RTP starts</em> and <em>Call established</em> is roughly how long "
                        "early media played before the call was answered."
                    ),
                    ms,
                )
            )
        pb = _figure_first_call_packet_balance(go, pio, cr0)
        if pb:
            charts.append(
                _section(
                    "This call voice packet balance",
                    (
                        "This chart shows packets sent versus packets received for this call only. "
                        "It's normal for the two bars to differ one side may transmit little or no audio, "
                        "or may stop sending before the other when the call ends."
                   
                    ),
                    pb,
                )
            )
        rh = _figure_per_call_rtp_health(go, pio, cr0)
        if rh:
            charts.append(
                _section(
                    "This call jitter and loss (averages)",
                    (
                        "These values show the average jitter and average packet loss for this call (dialog), "
                        "calculated over the entire call duration. Lower jitter (variation in packet arrival times) "
                        "and lower packet loss percentages are desirable. Note: This is a single summary per call, not a time series."
                   
                    ),
                    rh,
                )
            )
        ext_div = _extended_audio(go, pio, call_recs or [])
        if ext_div:
            charts.append(
                _section(
                    "Quiet vs lively on what you heard",
                    (
                        "<strong>Inbound RTP only.</strong> "
                        "This chart uses extended media analysis to automatically classify periods of inbound audio as either silent (calm) or lively (active). It helps to see how much of the call was quiet versus when speech or noise was present."
                   
                    ),
                    ext_div,
                ),
            )

        plan_steps_first: List[Dict[str, Any]] = []
        pl0 = cr0.get("plan") if isinstance(cr0.get("plan"), dict) else None
        if pl0:
            plan_steps_first = list(pl0.get("steps") or [])
        if plan_steps_first:
            charts.extend(
                [
                    _section(
                        "PV3 compos durations (first call)",
                        "Seconds per executed step — reveals long queues or slow dial / play compos.",
                        _figure_plan_durations(go, pio, plan_steps_first),
                    ),
                    _section(
                        "PV3 compos mix (step types)",
                        "Counts per <code>step_type</code> (sipstress abstraction of PV3 compos families).",
                        _figure_compos_counts(go, pio, plan_steps_first),
                    ),
                ]
            )
            _rms_chart = _figure_plan_rms(go, pio, plan_steps_first)
            if _rms_chart:
                charts.append(
                    _section(
                        "Prompt loudness per compos (RMS heuristic)",
                        "This chart shows the average RMS (Root Mean Square) audio level detected while each prompt was playing. Lower RMS values (dips) often correspond to times when users hear faint or quiet audio. RMS is a standard measure of audio loudness.",
                        _rms_chart,
                    )
                )

    charts_html = "\n".join(charts)

    cli_args = report.get("cli_args") or {}
    cli_rows = "\n".join(
        "        <tr><td><code>{}</code></td><td><code>{}</code></td></tr>".format(
            html_std.escape(str(k)),
            html_std.escape(_cli_cell(v)),
        )
        for k, v in sorted(cli_args.items())
    )

    dest_html = ""

    if d0:
        rec0 = call_recs[0] if call_recs else {}
        agg_ok, bullets = _story_lines(report)
        verdict_cls = "ok" if agg_ok else "fail"
        bullets_html = "\n".join(f"    <li>{ln}</li>" for ln in bullets)
        dialed_disp = html_std.escape(str(rec0.get("to") or cli_args.get("number_or_to") or "—"))
        frm = html_std.escape(str(rec0.get("from") or cli_args.get("from") or "—"))
        succ = rec0.get("success")
        succ_s = "yes" if succ is True else ("no" if succ is False else "—")
        early_r = "yes" if rec0.get("early_media_rtp") else "no"
        pickup_html = _answer_pickup_line(rec0)
        fst_disp = _final_status_line(rec0)

        panel = (
            '<section class="destination-panel section" aria-labelledby="sipstress-quick-verdict-heading">\n'
            '  <h3 id="sipstress-quick-verdict-heading">Who you called quick verdict</h3>\n'
            '  <p class="prose">\n'
            "    Outcome summary for the SIP leg under test. Charts label <strong>you</strong>"
            " as the INVITE-originating UA and <strong>far-end</strong> as the SDP peer that "
            "returns RTP on this dialog.\n"
            "  </p>\n"
            '  <div class="dest-grid">\n'
            f'    <div class="dest-cell"><span class="dest-label">Number / address dialed</span>'
            f'<div class="dest-value">{dialed_disp}</div></div>\n'
            f'    <div class="dest-cell"><span class="dest-label">CLI target</span>'
            f'<div class="dest-value">{html_std.escape(str(cli_args.get("number_or_to") or "—"))}</div></div>\n'
            f'    <div class="dest-cell"><span class="dest-label">From</span>'
            f'<div class="dest-value">{frm}</div></div>\n'
            f'    <div class="dest-cell"><span class="dest-label">Director</span>'
            f'<div class="dest-value">{html_std.escape(str(d0.get("director") or "—"))}</div></div>\n'
            f'    <div class="dest-cell"><span class="dest-label">Scenario success</span>'
            f'<div class="dest-value"><code>{html_std.escape(succ_s)}</code></div></div>\n'
            f'    <div class="dest-cell"><span class="dest-label">Final SIP status / BYE code</span>'
            f'<div class="dest-value"><code>{fst_disp}</code></div></div>\n'
            f'    <div class="dest-cell"><span class="dest-label">Early media RTP</span>'
            f'<div class="dest-value"><code>{html_std.escape(early_r)}</code></div></div>\n'
            '    <div class="dest-cell" style="grid-column:1/-1">\n'
            '      <span class="dest-label">Answer / pickup inference</span>\n'
            f'      <div class="dest-value prose" style="font-size:.88rem">{pickup_html}</div>\n'
            "    </div>\n"
            "  </div>\n"
            f'  <ul class="dest-story prose">\n{bullets_html}\n  </ul>\n'
            f'  <p class="prose muted">Automatic verdict badge: '
            f'<span class="pill {verdict_cls}">{"OK" if agg_ok else "Review"}</span></p>\n'
            "</section>"
        )

        dest_html = (
            '<section id="sipstress-destination" class="report-region"'
            ' aria-labelledby="sipstress-destination-heading">\n'
            '      <h2 id="sipstress-destination-heading">Destination &amp; sound path</h2>\n'
            '      <p class="prose muted" style="margin:-.35rem 0 1rem; max-width:75ch;">\n'
            "        Destination panel and charts for this director's SIP/RTP aggregates.\n"
            "      </p>\n"
            f"{panel}\n"
            "    </section>"
        )

    session_pv3_html = ""
    if d0 and call_recs:
        session_pv3_html = _html_session_pv3_section(call_recs[0])

    findings_li: List[str] = []
    for d in health.get("directors") or []:
        for fi in d.get("findings") or []:
            sev_css = str(fi.get("severity") or "info").lower()
            code_e = html_std.escape(str(fi.get("code") or ""))
            sev_e = html_std.escape(sev_css)
            msg_e = html_std.escape(str(fi.get("message") or ""))
            findings_li.append(
                f'        <li class="sev-{sev_css}"><strong>{code_e}</strong> [{sev_e}] — {msg_e}</li>'
            )

    ul_find = "\n".join(findings_li) if findings_li else "        <li>(none)</li>"

    reco_lines: List[str] = []
    for d in health.get("directors") or []:
        dn = html_std.escape(str(d.get("director") or "?"))
        for r in d.get("recommendations") or []:
            reco_lines.append(f"          <li>[{dn}] {html_std.escape(str(r))}</li>")
    reco_html = (
        "<ul>\n" + "\n".join(reco_lines) + "\n      </ul>"
        if reco_lines
        else '<p class="muted">(none)</p>'
    )

    inspect_lines: List[str] = []
    for d in health.get("directors") or []:
        for fi in d.get("findings") or []:
            code = str(fi.get("code") or "")
            hint = FINDING_HINTS.get(
                code,
                "Review JSON (call_records, directors) versus charts and thresholds.",
            )
            inspect_lines.append(
                "        <li><strong>[{sev}]</strong> <strong>{code}</strong>: {msg}<br>"
                "<em>{hint}</em></li>".format(
                    sev=html_std.escape(str(fi.get("severity") or "").upper()),
                    code=html_std.escape(code),
                    msg=html_std.escape(str(fi.get("message") or "")),
                    hint=html_std.escape(hint),
                )
            )
    inspections_h = (
        "\n".join(inspect_lines)
        if inspect_lines
        else "        <li>(no checklist entries for this report)</li>"
    )

    excerpt_esc = html_std.escape(REPORT_GUIDE_EXCERPT)

    ttl = html_std.escape(str(pf_banner))
    pf_cls = html_std.escape(str(pf_css))
    ws = html_std.escape(str(when))
    sch = html_std.escape(str(report.get("report_schema") or ""))
    pfe = html_std.escape(str(health.get("pass_fail") or ""))
    hero_actions = _hero_pdf_actions(report)

    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n<head>\n'
        '  <meta charset="utf-8" />\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1" />\n'
        f"  <title>sipstress — {ttl}</title>\n"
        '  <link rel="preconnect" href="https://fonts.googleapis.com" />\n'
        '  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />\n'
        '  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap"'
        ' rel="stylesheet" />\n'
        f'  <script src="{PLOTLY_CDN}" charset="utf-8"></script>\n'
        "  <style>\n"
        f"{_EMBEDDED_CSS}\n"
        "  </style>\n"
        "</head>\n<body>\n"
        '  <div class="wrap">\n'
        '    <header class="hero">\n'
        "      <h1>Sipstress call report</h1>\n"
        f'      <span class="pill {pf_cls}">{ttl}</span>\n'
        f'      <span class="muted" style="margin-left:.75rem">pass_fail: '
        f'<strong>{pfe}</strong></span>\n'
        f"      {hero_actions}\n"
        "    </header>\n\n"
        f"{kpis_html}\n{diagnostics_html}\n{all_calls_html}\n{dest_html}\n{session_pv3_html}\n"
        '    <section id="sipstress-run-config" class="report-region"'
        ' aria-labelledby="sipstress-run-config-heading">\n'
        '      <h2 id="sipstress-run-config-heading">Run configuration</h2>\n'
        '      <div class="section">\n'
        '        <p class="prose">Snapshot of CLI / config echoed into'
        ' <code>cli_args</code> in JSON.</p>\n'
        '        <table class="cli"><thead><tr><th>Key</th><th>Value</th>'
        '</tr></thead><tbody>\n'
        f"{cli_rows}\n"
        "        </tbody></table>\n"
        "      </div>\n"
        "    </section>\n\n"
        '    <section id="sipstress-measurements" class="report-region"'
        ' aria-labelledby="sipstress-measurements-heading">\n'
        '      <h2 id="sipstress-measurements-heading">Measurements &amp; graphs</h2>\n'
        f"{charts_html}\n"
        "    </section>\n\n"
        '    <section id="sipstress-health-findings" class="report-region"'
        ' aria-labelledby="sipstress-health-findings-heading">\n'
        '      <h2 id="sipstress-health-findings-heading">Health findings</h2>\n'
        '      <div class="section"><ul class="findings">\n'
        f"{ul_find}\n"
        "      </ul></div>\n"
        "    </section>\n\n"
        '    <section id="sipstress-inspect-next" class="report-region"'
        ' aria-labelledby="sipstress-inspect-next-heading">\n'
        '      <h2 id="sipstress-inspect-next-heading">Suggested next checks</h2>\n'
        '      <div class="section"><ol class="prose" style="margin:0;padding-left:1.2rem">\n'
        f"{inspections_h}\n"
        "      </ol></div>\n"
        "    </section>\n\n"
        '    <section id="sipstress-recommendations" class="report-region"'
        ' aria-labelledby="sipstress-recommendations-heading">\n'
        '      <h2 id="sipstress-recommendations-heading">Recommendations</h2>\n'
        f'      <div class="section">{reco_html}</div>\n'
        "    </section>\n\n"
        '    <section id="sipstress-field-map" class="report-region"'
        ' aria-label="Compact field reference">\n'
        '      <details class="summary">\n'
        '        <summary><strong>Compact field map</strong> (full narrative in'
        ' <code>REPORT_GUIDE.md</code>)</summary>\n'
        '        <pre style="white-space:pre-wrap;font-size:.82rem;margin:.75rem 0 0">'
        f"{excerpt_esc}</pre>\n"
        "      </details>\n"
        "    </section>\n"
        "  </div>\n</body>\n</html>\n"
    )
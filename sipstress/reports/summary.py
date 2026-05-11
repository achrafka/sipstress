"""Human-readable summary writer."""
from __future__ import annotations

import io
from typing import Dict, List


_VERDICT_BADGE = {
    "OK": "[ OK ]",
    "WARN": "[WARN]",
    "FAIL": "[FAIL]",
}


def _fmt_ms(v):
    if v is None:
        return "-"
    return f"{v:.1f}ms"


def _avg_or_none(values):
    vals = [v for v in values if isinstance(v, (int, float))]
    if not vals:
        return None
    return sum(vals) / len(vals)


def _aggregate_plan_records(records):
    """Aggregate per-compo statistics across all calls' plan results.

    Collapses by step_id (so the same compo across calls is one row).
    """
    from collections import Counter, OrderedDict

    by_id: "OrderedDict[str, dict]" = OrderedDict()
    ok = warn = fail = completed = 0
    all_findings: Counter = Counter()
    rec_set: "OrderedDict[str, None]" = OrderedDict()
    for rec in records:
        plan = rec.get("plan") or {}
        if plan.get("completed"):
            completed += 1
        for s in plan.get("steps") or []:
            sid = s["step_id"]
            row = by_id.setdefault(sid, {
                "id": sid,
                "type": s["step_type"],
                "name": s.get("name") or sid,
                "verdicts": Counter(),
                "onset_offsets": [],
                "durations": [],
                "rms": [],
                "dropouts_total": 0,
                "dtmf_sent": Counter(),
                "findings": Counter(),
                "recs": [],
            })
            row["verdicts"][s["verdict"]] += 1
            audio = s.get("audio") or {}
            if audio.get("onset_offset_s") is not None:
                row["onset_offsets"].append(audio["onset_offset_s"])
            if audio.get("prompt_duration_s") is not None:
                row["durations"].append(audio["prompt_duration_s"])
            if audio.get("rms_avg") is not None:
                row["rms"].append(audio["rms_avg"])
            row["dropouts_total"] += int(audio.get("dropout_count") or 0)
            for d in (s.get("dtmf") or {}).get("sent") or []:
                row["dtmf_sent"][d] += 1
            for f in s.get("findings") or []:
                row["findings"][f] += 1
                all_findings[f] += 1
            for r in s.get("recommendations") or []:
                rec_set.setdefault(r, None)
            xfer = s.get("expected_transfer_to")
            if xfer:
                row.setdefault("expected_transfer_to", xfer)
        # call-level verdict tally
        v = plan.get("verdict_counts") or {}
        if v.get("FAIL"):
            fail += 1
        elif v.get("WARN"):
            warn += 1
        else:
            ok += 1
    compos = []
    for row in by_id.values():
        verdicts = row["verdicts"]
        if verdicts.get("FAIL"):
            verdict = "FAIL"
        elif verdicts.get("WARN"):
            verdict = "WARN"
        elif verdicts.get("OK"):
            verdict = "OK"
        else:
            verdict = "SKIP"
        compos.append({
            "id": row["id"],
            "type": row["type"],
            "name": row["name"],
            "verdict": verdict,
            "onset_avg_s": _avg_or_none(row["onset_offsets"]),
            "duration_avg_s": _avg_or_none(row["durations"]),
            "rms_avg": _avg_or_none(row["rms"]),
            "dropouts_total": row["dropouts_total"],
            "dtmf_summary": ",".join(
                f"{d}x{n}" if n > 1 else d
                for d, n in row["dtmf_sent"].most_common()
            ),
            "expected_transfer_to": row.get("expected_transfer_to"),
        })
    return {
        "calls": len(records),
        "ok": ok,
        "warn": warn,
        "fail": fail,
        "completed": completed,
        "compos": compos,
        "unique_findings": all_findings.most_common(),
        "unique_recs": list(rec_set.keys()),
    }


def _fmt_seconds(v):
    if v is None:
        return "-"
    return f"{v:.2f}s"


def render(report: Dict) -> str:
    out = io.StringIO()
    health = report.get("health", {})
    overall = health.get("overall_verdict", "?")
    pass_fail = health.get("pass_fail", "PASS" if overall != "FAIL" else "FAIL")
    badge = _VERDICT_BADGE.get(overall, f"[{overall}]")

    out.write("SIP call test report\n")
    out.write("====================\n")
    out.write(f"PASS / FAIL : {pass_fail}\n")
    out.write(f"SIP health  : {badge} {overall}\n\n")

    cli = report.get("cli_args", {})
    if cli.get("mode") == "call_test":
        out.write("Test parameters:\n")
        out.write(f"  director       = {cli.get('director')}\n")
        out.write(f"  callee         = {cli.get('number_or_to')}\n")
        out.write(f"  media duration = {cli.get('duration_s')}s\n")
        out.write(f"  codec          = {cli.get('codec')}\n")
        if cli.get("audit"):
            out.write("  audit log      = yes (in JSON call_records)\n")
        out.write("\n")
    elif cli:
        out.write("Run parameters:\n")
        for k in (
            "scenario",
            "directors",
            "calls",
            "duration",
            "cps",
            "concurrent",
            "ramp_up",
            "ramp_down",
            "call_duration",
            "media",
        ):
            if k in cli:
                out.write(f"  {k:<14}= {cli[k]}\n")
        out.write("\n")

    for d in report.get("directors", []):
        label = d.get("director", "?")
        calls = d.get("calls", {})
        sip = d.get("sip", {})
        lat = d.get("latency_ms", {})
        media = d.get("media", {})
        thru = d.get("throughput", {})

        out.write(f"--- Director: {label} ---\n")
        out.write(
            f"  Calls   : attempted={calls.get('attempted',0)} "
            f"ok={calls.get('succeeded',0)} fail={calls.get('failed',0)} "
            f"timeout={calls.get('timed_out',0)} "
            f"success={(calls.get('success_ratio') or 0)*100:.2f}%\n"
        )
        out.write(
            f"  SIP     : req_sent={sip.get('requests_sent',0)} "
            f"resp_recv={sip.get('responses_recv',0)} "
            f"prov={sip.get('provisional_recv',0)} "
            f"final={sip.get('final_recv',0)} "
            f"retx={sip.get('retransmissions',0)}\n"
        )
        codes = sip.get("response_codes") or {}
        if codes:
            top = sorted(codes.items(), key=lambda kv: -kv[1])
            out.write(
                "  Codes   : "
                + ", ".join(f"{c}:{n}" for c, n in top[:10])
                + ("..." if len(top) > 10 else "")
                + "\n"
            )
        setup = lat.get("setup", {}) or {}
        answer = lat.get("answer", {}) or {}
        out.write(
            f"  Setup   : p50={_fmt_ms(setup.get('p50'))} "
            f"p90={_fmt_ms(setup.get('p90'))} "
            f"p95={_fmt_ms(setup.get('p95'))} "
            f"p99={_fmt_ms(setup.get('p99'))} "
            f"max={_fmt_ms(setup.get('max'))}\n"
        )
        if answer.get("count"):
            out.write(
                f"  Answer  : p50={_fmt_ms(answer.get('p50'))} "
                f"p95={_fmt_ms(answer.get('p95'))} "
                f"p99={_fmt_ms(answer.get('p99'))} "
                f"max={_fmt_ms(answer.get('max'))}\n"
            )
        if media.get("packets_sent") or media.get("packets_recv"):
            j = media.get("jitter_ms") or {}
            l = media.get("loss_ratio") or {}
            out.write(
                f"  Media   : sent={media.get('packets_sent',0)} "
                f"recv={media.get('packets_recv',0)} "
                f"jitter_avg={_fmt_ms(j.get('mean'))} "
                f"jitter_max={_fmt_ms(j.get('max'))} "
                f"loss_avg={(l.get('mean') or 0)*100:.2f}%\n"
            )

        if cli.get("mode") == "call_test":
            recs = d.get("call_records") or []
            if len(recs) == 1:
                r0 = recs[0]
                ok = r0.get("success")
                fr = r0.get("failure_reason") or ""
                out.write(
                    f"  Call    : wall_time={float(r0.get('duration_s') or 0):.2f}s "
                    f"outcome={'SUCCESS' if ok else 'FAILURE'}"
                    + (f" ({fr})" if fr else "")
                    + "\n"
                )
                rtp = r0.get("rtp") or {}
                if rtp:
                    out.write(
                        f"  RTP leg : jitter={_fmt_ms(rtp.get('jitter_ms'))} "
                        f"loss={((rtp.get('loss_ratio') or 0)*100):.2f}%\n"
                    )
                wav = r0.get("recording_wav")
                if wav:
                    out.write(f"  WAV     : {wav}\n")
                rin = r0.get("recording")
                if isinstance(rin, dict) and rin:
                    out.write(
                        f"  Record  : layout={rin.get('layout')} "
                        f"duplex={rin.get('duplex_requested')} "
                        f"mic_tx={rin.get('microphone_on_wire')}\n"
                    )
                sp = r0.get("scenario_profile")
                if isinstance(sp, dict) and sp.get("id"):
                    out.write(f"  Scenario: {sp.get('id')} — {sp.get('summary','')[:100]}\n")
                    timings = sp.get("timings_relative_call_start") or {}
                    ts = timings.get("sip_invite_to_200_wall_s")
                    if isinstance(ts, (int, float)):
                        out.write(
                            "  Scenario timings: INVITE→200 wall "
                            f"≈{_fmt_seconds(ts)}\n"
                        )
                    finds = sp.get("findings_observed_in_scenario_layer") or []
                    if finds:
                        out.write("  Scenario findings (signals / race hints):\n")
                        for line in finds[:25]:
                            out.write(f"    - {line}\n")
                bye_x = r0.get("bye_non_2xx_accepted")
                if bye_x:
                    out.write(
                        f"  BYE     : {bye_x} (non-2xx, accepted as OK teardown)\n"
                    )

        # Plan-driven (PV3-style) call records — optional legacy
        plan_records = [
            rec for rec in (d.get("call_records") or []) if rec.get("plan")
        ]
        if plan_records:
            agg = _aggregate_plan_records(plan_records)
            out.write(
                f"  Plan    : calls={agg['calls']} ok={agg['ok']} "
                f"warn={agg['warn']} fail={agg['fail']} "
                f"completed={agg['completed']}\n"
            )
            # Per-compo aggregate table
            out.write("  Plan compos (aggregated across all calls):\n")
            header = (
                "    {idx:>3}  {type:<11} {name:<28} "
                "{verd:<5}  prompt_onset  prompt_dur  rms_avg  drop  dtmf"
            )
            out.write(
                "    {idx:>3}  {type:<11} {name:<28} "
                "{verd:<5}  {pon:<12}  {pdur:<10}  {rms:<7}  {drop:<4}  {dtmf}\n"
                .format(
                    idx="#",
                    type="type",
                    name="compo",
                    verd="vdct",
                    pon="onset",
                    pdur="dur",
                    rms="rms",
                    drop="drop",
                    dtmf="dtmf",
                )
            )
            for i, c in enumerate(agg["compos"]):
                out.write(
                    "    {idx:>3}  {type:<11} {name:<28} "
                    "{verd:<5}  {pon:<12}  {pdur:<10}  {rms:<7}  {drop:<4}  {dtmf}\n"
                    .format(
                        idx=i,
                        type=c["type"][:11],
                        name=(c["name"] or c["id"])[:28],
                        verd=c["verdict"],
                        pon=_fmt_seconds(c["onset_avg_s"]),
                        pdur=_fmt_seconds(c["duration_avg_s"]),
                        rms=f"{int(c['rms_avg']):d}" if c["rms_avg"] else "-",
                        drop=str(c["dropouts_total"]),
                        dtmf=c["dtmf_summary"][:20],
                    )
                )
            xfers = [(c["name"] or c["id"], c["expected_transfer_to"]) for c in agg["compos"] if c.get("expected_transfer_to")]
            if xfers:
                out.write("  Planned B-leg hints (verify on PBX/CDR; not read from SIP):\n")
                for compo, num in xfers:
                    out.write(f"    • {compo[:40]} → {num}\n")
            if agg["unique_recs"]:
                out.write("  Plan recommendations:\n")
                for r in agg["unique_recs"][:20]:
                    out.write(f"    * {r}\n")
            if agg["unique_findings"]:
                out.write("  Plan findings (top):\n")
                for f, n in agg["unique_findings"][:10]:
                    out.write(f"    - [{n}x] {f}\n")

        # IVR call records (legacy DTMF mode)
        ivr_records = []
        for rec in d.get("call_records", []) or []:
            if rec.get("ivr"):
                ivr_records.append(rec)
        if ivr_records:
            completed = sum(1 for r in ivr_records if r["ivr"].get("plan_completed"))
            sent_digits_total = sum(
                len(r["ivr"].get("dtmf_sent") or []) for r in ivr_records
            )
            recv_digits_total = sum(
                len(r["ivr"].get("dtmf_received") or []) for r in ivr_records
            )
            audio_active_avg = _avg_or_none(
                [r["ivr"].get("audio_active_ratio") for r in ivr_records]
            )
            out.write(
                f"  IVR     : calls={len(ivr_records)} plan_ok={completed} "
                f"dtmf_sent={sent_digits_total} dtmf_recv={recv_digits_total} "
                f"audio_active_avg="
                f"{(audio_active_avg or 0)*100:.0f}%\n"
            )
            # Render the first IVR call as a sample timeline
            sample = ivr_records[0]["ivr"]
            out.write("  IVR sample (first call):\n")
            for s in sample.get("steps", [])[:15]:
                out.write(
                    f"    [{s['t_start']:6.2f}s -> {s['t_end']:6.2f}s] "
                    f"{s['kind']:13} {s['detail']}"
                    + (f"  ({s['note']})" if s.get('note') else "")
                    + "\n"
                )
            if sample.get("dtmf_sent"):
                digits = "".join(d["digit"] for d in sample["dtmf_sent"])
                out.write(f"    DTMF sent : {digits}\n")
            if sample.get("dtmf_received"):
                digits = "".join(d["digit"] for d in sample["dtmf_received"])
                out.write(f"    DTMF recv : {digits}\n")
            if sample.get("wav_path"):
                out.write(f"    Recording : {sample['wav_path']}\n")
        if cli.get("mode") != "call_test":
            out.write(
                f"  CPS     : target_avg={thru.get('cps_target_avg') or 0:.2f} "
                f"actual_avg={thru.get('cps_actual_avg') or 0:.2f}\n"
            )

        # Health for this director
        health_for = next(
            (h for h in health.get("directors", []) if h.get("director") == label),
            None,
        )
        if health_for:
            out.write(f"  Verdict : {_VERDICT_BADGE.get(health_for['verdict'], '[?]')} "
                      f"{health_for['verdict']}\n")
            if health_for.get("findings"):
                out.write("  Findings:\n")
                for f in health_for["findings"]:
                    sev = f.get("severity", "info").upper()
                    out.write(f"    - [{sev:5}] {f.get('message','')}\n")
            if health_for.get("recommendations"):
                out.write("  Recommendations:\n")
                for r in health_for["recommendations"]:
                    out.write(f"    * {r}\n")
        out.write("\n")
    return out.getvalue()


def write(report: Dict, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(render(report))

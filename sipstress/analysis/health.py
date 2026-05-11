"""Health analyzer.

Translates raw metrics into a verdict (`OK`, `WARN`, `FAIL`) plus a list of
findings and recommendations. Thresholds are intentionally conservative and
configurable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class HealthThresholds:
    success_ratio_warn: float = 0.99
    success_ratio_fail: float = 0.95
    timeout_ratio_warn: float = 0.005
    timeout_ratio_fail: float = 0.02
    retransmission_ratio_warn: float = 0.005
    retransmission_ratio_fail: float = 0.02
    setup_p99_warn_ms: float = 150.0
    setup_p99_fail_ms: float = 500.0
    setup_p95_warn_ms: float = 100.0
    answer_p99_warn_ms: float = 1000.0
    answer_p99_fail_ms: float = 3000.0
    jitter_avg_warn_ms: float = 30.0
    jitter_avg_fail_ms: float = 80.0
    rtp_loss_warn: float = 0.01
    rtp_loss_fail: float = 0.05
    cps_drift_warn: float = 0.10  # |target-actual|/target
    cps_drift_fail: float = 0.30

    @classmethod
    def for_single_call(cls) -> "HealthThresholds":
        """Relaxed thresholds for one-off call diagnostic (UDP retransmits are normal)."""
        return cls(
            retransmission_ratio_warn=0.70,
            retransmission_ratio_fail=1.01,
            cps_drift_warn=1.0,
            cps_drift_fail=1.01,
        )


@dataclass
class Finding:
    severity: str  # "info", "warn", "fail"
    code: str
    message: str
    detail: Dict = field(default_factory=dict)


# Forward declaration so _analyze_plan can construct Findings before
# `analyze_director` runs (signatures are forward-referenced).


@dataclass
class HealthReport:
    director: str
    verdict: str   # "OK" | "WARN" | "FAIL"
    findings: List[Finding] = field(default_factory=list)
    recommendations: List[str] = field(default_factory=list)


def _ratio(num: int, den: int) -> Optional[float]:
    if den <= 0:
        return None
    return num / den


def analyze_director(
    director_metrics: Dict, thresholds: HealthThresholds
) -> HealthReport:
    """Analyze one director's serialized metrics dict (from Metrics.to_dict)."""
    rep = HealthReport(director=director_metrics.get("director", "unknown"), verdict="OK")
    findings = rep.findings
    recos = rep.recommendations

    calls = director_metrics.get("calls", {})
    sip = director_metrics.get("sip", {})
    lat = director_metrics.get("latency_ms", {})
    media = director_metrics.get("media", {})
    thru = director_metrics.get("throughput", {})

    attempted = calls.get("attempted", 0)
    succeeded = calls.get("succeeded", 0)
    failed = calls.get("failed", 0)
    timed_out = calls.get("timed_out", 0)
    success_ratio = _ratio(succeeded, attempted)
    timeout_ratio = _ratio(timed_out, attempted)

    # ---- success ratio
    if attempted > 0 and success_ratio is not None:
        if success_ratio < thresholds.success_ratio_fail:
            findings.append(
                Finding(
                    "fail",
                    "low_success_ratio",
                    f"Success ratio {success_ratio:.1%} < {thresholds.success_ratio_fail:.0%}",
                    {"success_ratio": success_ratio},
                )
            )
            recos.append(
                "Investigate failure_reasons and response_codes; the SUT is "
                "likely overloaded or rejecting calls."
            )
        elif success_ratio < thresholds.success_ratio_warn:
            findings.append(
                Finding(
                    "warn",
                    "marginal_success_ratio",
                    f"Success ratio {success_ratio:.2%} below target {thresholds.success_ratio_warn:.0%}",
                )
            )

    # ---- timeouts
    if attempted > 0 and timeout_ratio is not None and timeout_ratio > 0:
        if timeout_ratio >= thresholds.timeout_ratio_fail:
            findings.append(
                Finding(
                    "fail",
                    "excessive_timeouts",
                    f"{timed_out}/{attempted} timeouts ({timeout_ratio:.1%})",
                )
            )
            recos.append(
                "Excessive transaction timeouts; check network MTU, packet "
                "loss to director, and director response latency."
            )
        elif timeout_ratio >= thresholds.timeout_ratio_warn:
            findings.append(
                Finding(
                    "warn",
                    "elevated_timeouts",
                    f"{timed_out}/{attempted} timeouts ({timeout_ratio:.2%})",
                )
            )

    # ---- retransmissions / duplicates
    responses = sip.get("responses_recv", 0)
    rtx = sip.get("retransmissions", 0)
    rtx_ratio = _ratio(rtx, responses) if responses else None
    if rtx_ratio is not None:
        if rtx_ratio >= thresholds.retransmission_ratio_fail:
            findings.append(
                Finding(
                    "fail",
                    "high_retransmissions",
                    f"{rtx}/{responses} duplicate responses ({rtx_ratio:.2%})",
                )
            )
            recos.append(
                "High duplicate-response rate suggests UDP loss or slow ACKs. "
                "Check upstream link, OS UDP buffers (net.core.rmem_max), and "
                "director response timers."
            )
        elif rtx_ratio >= thresholds.retransmission_ratio_warn:
            findings.append(
                Finding(
                    "warn",
                    "some_retransmissions",
                    f"{rtx}/{responses} duplicate responses ({rtx_ratio:.2%})",
                )
            )

    # ---- response code distribution: warn onInvite-side failures vs teardown quirks
    codes = sip.get("response_codes", {}) or {}
    final_failures = {int(k): v for k, v in codes.items() if int(k) >= 400}
    if final_failures:
        top = sorted(final_failures.items(), key=lambda kv: -kv[1])[:5]
        teardown_only_hints = frozenset({408, 481, 513})
        all_fail_codes = set(final_failures.keys())
        if (
            succeeded == attempted
            and failed == 0
            and all_fail_codes <= teardown_only_hints
        ):
            findings.append(
                Finding(
                    "info",
                    "teardown_response_codes",
                    "Observed SIP response codes on dialog teardown "
                    "(not counted as INVITE/media failure): "
                    + ", ".join(f"{c}={n}" for c, n in top),
                    {"codes": dict(top)},
                )
            )
        else:
            findings.append(
                Finding(
                    "info",
                    "failure_codes",
                    "Top SIP response codes in the 4xx/5xx/6xx range: "
                    + ", ".join(f"{c}={n}" for c, n in top),
                    {"codes": dict(top)},
                )
            )
        if any(c in (503, 480, 486, 487) for c, _ in top):
            recos.append(
                "Failure codes 480/486/487/503 typically indicate overload or "
                "unavailable target. Check director admission control and "
                "downstream gateway capacity."
            )
        if any(c in (407, 401) for c, _ in top):
            recos.append(
                "Auth failures (401/407): check credentials and clock skew "
                "(nonce expiry)."
            )

    # ---- setup latency
    setup = lat.get("setup", {}) or {}
    p99 = setup.get("p99")
    p95 = setup.get("p95")
    if p99 is not None:
        if p99 >= thresholds.setup_p99_fail_ms:
            findings.append(
                Finding(
                    "fail",
                    "setup_latency_p99",
                    f"INVITE->1xx p99 = {p99:.0f}ms (limit {thresholds.setup_p99_fail_ms:.0f}ms)",
                    {"p99_ms": p99},
                )
            )
            recos.append(
                "p99 setup latency is high; investigate director processing "
                "queue depth and downstream DNS / SRV resolution."
            )
        elif p99 >= thresholds.setup_p99_warn_ms:
            findings.append(
                Finding(
                    "warn",
                    "setup_latency_p99",
                    f"INVITE->1xx p99 = {p99:.0f}ms (target {thresholds.setup_p99_warn_ms:.0f}ms)",
                )
            )
    if p95 is not None and p95 >= thresholds.setup_p95_warn_ms:
        findings.append(
            Finding(
                "warn",
                "setup_latency_p95",
                f"INVITE->1xx p95 = {p95:.0f}ms (target {thresholds.setup_p95_warn_ms:.0f}ms)",
            )
        )

    answer = lat.get("answer", {}) or {}
    a99 = answer.get("p99")
    if a99 is not None:
        if a99 >= thresholds.answer_p99_fail_ms:
            findings.append(
                Finding(
                    "fail",
                    "answer_latency_p99",
                    f"INVITE->200 p99 = {a99:.0f}ms (limit {thresholds.answer_p99_fail_ms:.0f}ms)",
                )
            )
        elif a99 >= thresholds.answer_p99_warn_ms:
            findings.append(
                Finding(
                    "warn",
                    "answer_latency_p99",
                    f"INVITE->200 p99 = {a99:.0f}ms (target {thresholds.answer_p99_warn_ms:.0f}ms)",
                )
            )

    # ---- media
    jitter = (media.get("jitter_ms") or {}).get("mean")
    if jitter is not None:
        if jitter >= thresholds.jitter_avg_fail_ms:
            findings.append(
                Finding(
                    "fail",
                    "high_jitter",
                    f"Mean RTP jitter {jitter:.1f}ms (limit {thresholds.jitter_avg_fail_ms:.0f}ms)",
                )
            )
            recos.append(
                "High jitter on RTP path; check QoS/DSCP, NIC offloading, "
                "and the media gateway under load."
            )
        elif jitter >= thresholds.jitter_avg_warn_ms:
            findings.append(
                Finding(
                    "warn",
                    "elevated_jitter",
                    f"Mean RTP jitter {jitter:.1f}ms",
                )
            )

    loss = (media.get("loss_ratio") or {}).get("mean")
    if loss is not None:
        if loss >= thresholds.rtp_loss_fail:
            findings.append(
                Finding(
                    "fail",
                    "rtp_loss_high",
                    f"Mean RTP loss {loss:.1%}",
                )
            )
            recos.append(
                "RTP packet loss is high; check links between sipstress and "
                "media relay, inspect firewall/NAT (RTP timeouts), and ensure "
                "ICE/SBC media handling is correct."
            )
        elif loss >= thresholds.rtp_loss_warn:
            findings.append(
                Finding(
                    "warn",
                    "rtp_loss",
                    f"Mean RTP loss {loss:.2%}",
                )
            )

    # ---- CPS drift
    # cps_actual_avg is the mean of short (500ms-window) instantaneous rates from
    # the status loop. While calls are holding media and no NEW attempts happen,
    # those windows measure ~0 CPS, which drags the average toward zero even when
    # scheduling kept up with target — compare against peak rate as a sanity gate.
    target = thru.get("cps_target_avg")
    actual = thru.get("cps_actual_avg")
    peak = thru.get("cps_actual_max")
    if (
        target
        and target > 0.5
        and actual is not None
        and not (peak is not None and peak >= 0.7 * target)
    ):
        drift = abs(target - actual) / target
        if drift >= thresholds.cps_drift_fail:
            findings.append(
                Finding(
                    "fail",
                    "cps_drift",
                    f"CPS drift {drift:.0%} (target {target:.1f}, actual {actual:.1f})",
                )
            )
            recos.append(
                "Effective attempt rate (time-averaged) is far below target CPS. "
                "That can be normal with long --duration and low -j; raise "
                "--concurrency if the platform allows, lower --cps, or run on a "
                "faster host if the CPU is saturated."
            )
        elif drift >= thresholds.cps_drift_warn:
            findings.append(
                Finding(
                    "warn",
                    "cps_drift",
                    f"CPS drift {drift:.0%} (target {target:.1f}, actual {actual:.1f})",
                )
            )

    # ---- plan / per-compo aggregates
    plan_findings, plan_recs, plan_severity = _analyze_plan(director_metrics)
    findings.extend(plan_findings)
    recos.extend(plan_recs)

    # ---- anomalies (race conditions)
    anomalies = director_metrics.get("anomalies") or []
    if anomalies:
        kinds = {}
        for a in anomalies:
            kinds[a["kind"]] = kinds.get(a["kind"], 0) + 1
        msg = "Race / out-of-dialog events: " + ", ".join(
            f"{k}={v}" for k, v in kinds.items()
        )
        sev = "warn"
        if any(k in ("late_response_after_call_ended", "late_request_after_call_ended") for k in kinds):
            sev = "warn"
        findings.append(Finding(sev, "race_conditions", msg, {"by_kind": kinds}))
        recos.append(
            "Late SIP messages after call termination point at races between "
            "BYE/200 and concurrent UA actions. Inspect the director's "
            "transaction layer and B2BUA leg synchronization."
        )

    # ---- compute verdict
    if any(f.severity == "fail" for f in findings):
        rep.verdict = "FAIL"
    elif any(f.severity == "warn" for f in findings):
        rep.verdict = "WARN"
    else:
        rep.verdict = "OK"
    return rep


def _analyze_plan(director_metrics: Dict):
    """Pull aggregate per-compo findings out of plan-driven call records."""
    findings: List[Finding] = []
    recos: List[str] = []
    severity = "info"
    records = director_metrics.get("call_records") or []
    plan_records = [r for r in records if r.get("plan")]
    if not plan_records:
        return findings, recos, severity
    from collections import Counter
    compo_verdicts: Dict[str, Counter] = {}
    compo_findings: Dict[str, Counter] = {}
    compo_meta: Dict[str, Dict] = {}
    completed = 0
    for r in plan_records:
        plan = r["plan"]
        if plan.get("completed"):
            completed += 1
        for s in plan.get("steps") or []:
            sid = s["step_id"]
            cv = compo_verdicts.setdefault(sid, Counter())
            cf = compo_findings.setdefault(sid, Counter())
            cv[s["verdict"]] += 1
            for f in s.get("findings") or []:
                cf[f] += 1
            compo_meta[sid] = {"name": s.get("name") or sid, "type": s["step_type"]}

    n = len(plan_records)
    completion_ratio = completed / n if n else 0.0
    if completion_ratio < 1.0:
        if completion_ratio < 0.5:
            findings.append(Finding(
                "fail", "plan_completion",
                f"Plan completed in {completion_ratio:.0%} of calls",
            ))
            recos.append(
                "More than half of the calls did not finish all plan steps. "
                "Inspect the per-compo breakdown — most likely the IVR is "
                "rejecting one of the prompts (no audio detected) or the "
                "DTMF is not being honoured."
            )
            severity = "fail"
        else:
            findings.append(Finding(
                "warn", "plan_completion",
                f"Plan completed in only {completion_ratio:.0%} of calls",
            ))
            severity = "warn"

    # Per-compo verdict aggregation
    for sid, verds in compo_verdicts.items():
        meta = compo_meta.get(sid, {})
        cname = meta.get("name", sid)
        ctype = meta.get("type", "?")
        total = sum(verds.values())
        fails = verds.get("FAIL", 0)
        warns = verds.get("WARN", 0)
        if fails:
            findings.append(Finding(
                "fail", f"compo_fail_{ctype}",
                f"Compo {cname} ({ctype}): FAIL in {fails}/{total} calls. "
                f"Top findings: "
                + ", ".join(f"{f}({n})" for f, n in compo_findings[sid].most_common(3)),
                {"step_id": sid},
            ))
            severity = "fail"
        elif warns >= max(2, total // 4):
            findings.append(Finding(
                "warn", f"compo_warn_{ctype}",
                f"Compo {cname} ({ctype}): WARN in {warns}/{total} calls. "
                f"Top findings: "
                + ", ".join(f"{f}({n})" for f, n in compo_findings[sid].most_common(3)),
                {"step_id": sid},
            ))
            if severity == "info":
                severity = "warn"
    return findings, recos, severity


def analyze_run(metrics_dicts: List[Dict], thresholds: Optional[HealthThresholds] = None
                ) -> Dict:
    th = thresholds or HealthThresholds()
    reports = [analyze_director(m, th) for m in metrics_dicts]
    overall = "OK"
    for r in reports:
        if r.verdict == "FAIL":
            overall = "FAIL"
            break
        if r.verdict == "WARN":
            overall = "WARN"
    return {
        "overall_verdict": overall,
        "pass_fail": "FAIL" if overall == "FAIL" else "PASS",
        "directors": [
            {
                "director": r.director,
                "verdict": r.verdict,
                "findings": [f.__dict__ for f in r.findings],
                "recommendations": r.recommendations,
            }
            for r in reports
        ],
        "thresholds": th.__dict__,
    }

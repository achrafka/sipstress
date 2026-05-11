"""Loaders for sipstress test plans.

Two formats are supported:

1. **Native YAML/JSON** (``load_plan_file``): a flat list of steps with the
   shape understood by :class:`StepSpec.from_dict`. This is the most flexible
   and is the recommended way for explicit hand-written tests.

2. **PV3 Studio scenario export** (``load_pv3_studio_json``): the JSON your
   Studio backend stores under the ``containers`` + ``end-points`` keys
   (the same shape consumed by ``pv3.pv3studio.builders.BaseBuilder``). The
   loader walks the graph from the Start compo, follows a user-provided
   "navigation" (mapping element-uuid -> branch name, or a flat list of
   branches in order of encounter) and emits one StepSpec per visited
   compo. Branching/data compos that the caller cannot perceive
   (Switch / Dispatcher / SetVariable / Counter / TableLookup / WebService
   / Discloser / StatsMarker / GetInternalVariable / StringHandler /
   DateHandler / SendEmail / SendSMS / Delegation) are surfaced as
   ``note`` steps so they appear in the report without affecting timing.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

from .spec import StepSpec, StepType, TestPlan

log = logging.getLogger("sipstress.plan.loader")


# ---------- native YAML / JSON ----------

def load_plan_file(path: str) -> TestPlan:
    """Load a sipstress-native plan file (.yaml / .yml / .json)."""
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    data: Dict[str, Any]
    if path.lower().endswith((".yaml", ".yml")):
        try:
            import yaml  # type: ignore
        except ImportError as exc:
            raise SystemExit("PyYAML required for YAML plans") from exc
        data = yaml.safe_load(text) or {}
    else:
        data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"Plan root must be a mapping; got {type(data).__name__}")
    return TestPlan.from_dict(data)


# ---------- PV3 Studio JSON ----------

# Map PV3 element types -> sipstress step types. Anything not listed is
# treated as a NOTE (no caller-perceived behaviour).
_PV3_TYPE_TO_STEP = {
    "Play": StepType.PLAY,
    "SayNumber": StepType.PLAY,
    "SaySentence": StepType.PLAY,
    "SpeechSynthesis": StepType.PLAY,
    "AudioReplace": StepType.PLAY,
    "AudioDrain": StepType.RECORD,
    "Menu": StepType.MENU,
    "GetDigits": StepType.GET_DIGITS,
    "AudioPicker": StepType.GET_DIGITS,
    "SendDTMF": StepType.SEND_DTMF,
    "DialSimple": StepType.DIAL,
    "DialMulti": StepType.DIAL,
    "DialDirect": StepType.DIAL,
    "DialWaiting": StepType.DIAL,
    "WaitingQueue": StepType.QUEUE,
    "VirtualQueue": StepType.QUEUE,
    "VoiceToMail": StepType.RECORD,
    "AudioRecordUpdate": StepType.RECORD,
    "Rasa": StepType.RECORD,
    "Answer": StepType.ANSWER,
    "Hangup": StepType.HANGUP,
    # everything below is silent; we still emit a NOTE step for traceability
    "Start": StepType.NOTE,
    "Switch": StepType.NOTE,
    "AdvancedSwitch": StepType.NOTE,
    "Dispatcher": StepType.NOTE,
    "SetVariable": StepType.NOTE,
    "Counter": StepType.NOTE,
    "TableLookup": StepType.NOTE,
    "CheckCalendar": StepType.NOTE,
    "CheckDate": StepType.NOTE,
    "CheckTime": StepType.NOTE,
    "WebService": StepType.NOTE,
    "Delegation": StepType.NOTE,
    "SendEmail": StepType.NOTE,
    "SendSMS": StepType.NOTE,
    "StatsMarker": StepType.NOTE,
    "ApiDiscloser": StepType.NOTE,
    "GetInternalVariable": StepType.NOTE,
    "StringHandler": StepType.NOTE,
    "DateHandler": StepType.NOTE,
    "AudioFTP": StepType.NOTE,
}


def _pick_param(parameters: Any, *keys: str) -> Optional[Any]:
    """Best-effort extractor for nested PV3 parameter dicts."""
    if not isinstance(parameters, dict):
        return None
    for k in keys:
        if k in parameters:
            v = parameters[k]
            if isinstance(v, dict) and "values" in v:
                vv = v["values"]
                if isinstance(vv, list) and vv:
                    return vv[0]
                return vv
            return v
    return None


def _menu_valid_digits(endpoints_for_compo: Dict[str, Any]) -> List[str]:
    """A PV3 Menu's allowed digits are the single-character endpoint keys."""
    return sorted(
        k for k in endpoints_for_compo.keys()
        if isinstance(k, str) and len(k) == 1
        and (k.isdigit() or k in "*#")
    )


def load_pv3_studio_json(
    path: str,
    navigation: Optional[Dict[str, str]] = None,
    branches_in_order: Optional[List[str]] = None,
    max_steps: int = 200,
) -> TestPlan:
    """Build a :class:`TestPlan` by walking a PV3 Studio scenario JSON.

    Parameters
    ----------
    path : str
        Path to the Studio JSON export (must contain ``containers`` and
        ``end-points`` keys).
    navigation : dict, optional
        Map ``{compo_uuid: branch_value}`` telling the walker which branch
        to take at each branching compo. Useful when you want a stable
        deterministic path through the scenario.
    branches_in_order : list[str], optional
        A flat list of branch values consumed in order each time we hit a
        compo with multiple endpoints. Used when you don't want to spell
        out uuids.
    max_steps : int
        Hard cap to prevent infinite loops in cyclic graphs.
    """
    with open(path, "r", encoding="utf-8") as f:
        scenario = json.load(f)

    containers = scenario.get("containers") or []
    raw_endpoints = scenario.get("end-points") or scenario.get("endpoints") or []

    if not containers:
        raise ValueError(f"{path}: no 'containers' in scenario JSON")

    by_uuid: Dict[str, Dict[str, Any]] = {c["uuid"]: c for c in containers}

    # source_uuid -> { branch_value: target_uuid }
    edges: Dict[str, Dict[str, str]] = {}
    for ep in raw_endpoints:
        src = ep.get("source_uuid")
        tgt = ep.get("target_uuid")
        if not src or not tgt:
            continue
        val = ep.get("value") or ""
        if val == "":
            val = "default"
        edges.setdefault(src, {})[val] = tgt

    # Find the Start compo
    start_uuid: Optional[str] = None
    for c in containers:
        if c.get("type") == "Start":
            start_uuid = c["uuid"]
            break
    if start_uuid is None:
        # fallback: take the first compo
        start_uuid = containers[0]["uuid"]

    plan = TestPlan(
        name=scenario.get("name") or os.path.basename(path),
        description=f"Walked from {scenario.get('name', '<unnamed>')} (Studio JSON)",
    )

    branches_iter = iter(branches_in_order or [])
    visited: Dict[str, int] = {}
    cur = start_uuid
    while cur and len(plan.steps) < max_steps:
        compo = by_uuid.get(cur)
        if compo is None:
            break
        if visited.get(cur, 0) > 5:  # safety: don't loop forever
            log.warning("PV3 walker: visited %s too many times, stopping", cur)
            break
        visited[cur] = visited.get(cur, 0) + 1

        compo_endpoints = edges.get(cur, {})
        step = _compo_to_step(compo, compo_endpoints)
        plan.steps.append(step)

        # decide branch
        next_branch: Optional[str] = None
        if cur in (navigation or {}):
            next_branch = navigation[cur]
        elif len(compo_endpoints) == 1:
            next_branch = next(iter(compo_endpoints))
        elif len(compo_endpoints) > 1:
            try:
                next_branch = next(branches_iter)
            except StopIteration:
                # default to "default" if it exists, else first
                next_branch = (
                    "default" if "default" in compo_endpoints
                    else next(iter(compo_endpoints))
                )
        step.branch_taken = next_branch
        if not next_branch or next_branch not in compo_endpoints:
            break
        cur = compo_endpoints[next_branch]
        step.next_step_id = cur

    return plan


def _compo_to_step(
    compo: Dict[str, Any], endpoints_for_compo: Dict[str, str]
) -> StepSpec:
    ctype = compo.get("type", "Unknown")
    cuuid = compo.get("uuid", "?")
    cname = compo.get("name") or ctype
    parameters = compo.get("parameters") or {}

    sip_type = _PV3_TYPE_TO_STEP.get(ctype, StepType.NOTE)
    step = StepSpec(
        id=cuuid,
        type=sip_type,
        name=f"{cname} ({ctype})",
        description=f"PV3 compo {ctype} {cuuid}",
    )
    step.extra["pv3_type"] = ctype
    step.extra["pv3_uuid"] = cuuid
    step.extra["pv3_endpoints"] = list(endpoints_for_compo.keys())

    if sip_type == StepType.MENU:
        step.valid_digits = _menu_valid_digits(endpoints_for_compo)
        # We don't auto-pick a digit; caller should supply navigation /
        # post-process the plan to set send_digit. Surface as a finding.
        step.expect_prompt_within_s = 5.0
        step.min_prompt_duration_s = 0.4
    elif sip_type == StepType.GET_DIGITS:
        step.expect_prompt_within_s = 4.0
        step.min_prompt_duration_s = 0.3
    elif sip_type == StepType.PLAY:
        step.expect_prompt_within_s = 4.0
        step.min_prompt_duration_s = 0.5
    elif sip_type == StepType.DIAL:
        step.expect_audible = False
        step.max_duration_s = 30.0
        step.expect_ringback = True
    elif sip_type == StepType.QUEUE:
        step.expect_audible = True
        step.max_duration_s = 60.0
        step.queue_max_wait_s = 60.0
    elif sip_type == StepType.RECORD:
        rec = _pick_param(parameters, "max-time", "duration", "record-duration") or 5
        try:
            step.record_duration_s = float(rec)
        except (TypeError, ValueError):
            step.record_duration_s = 5.0
    elif sip_type == StepType.HANGUP:
        step.expect_audible = False

    return step

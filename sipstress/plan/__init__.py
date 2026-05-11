"""Test plan abstraction for sipstress.

A test plan is a sequence of ``StepSpec``s that the runtime executes inside
a single SIP call. Each step models one PV3 "compo" (or any equivalent IVR
element) and carries:

* what we *expect* to happen (prompt onset within Xs, audible audio of at
  least Ys, sustained silence after a digit, etc.)
* what we *do* (send a single DTMF for Menu, multi-digit + terminator for
  GetDigits, just listen for Play, wait for ringback for Dial, ...)
* per-step timeouts and tolerances

After the call, every step has a :class:`StepResult` with a verdict
(OK / WARN / FAIL), measured timings, audio quality metrics over its time
window, DTMF observed, and any findings + recommendations.
"""

from .spec import StepResult, StepSpec, StepType, StepVerdict, TestPlan  # noqa: F401
from .loader import load_plan_file, load_pv3_studio_json  # noqa: F401

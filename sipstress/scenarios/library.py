"""Scenario registry."""
from __future__ import annotations

from typing import Awaitable, Callable, Dict, List

from ..engine.call import CallContext

ScenarioFn = Callable[[CallContext], Awaitable[None]]

REGISTRY: Dict[str, ScenarioFn] = {}


def register(name: str) -> Callable[[ScenarioFn], ScenarioFn]:
    def deco(fn: ScenarioFn) -> ScenarioFn:
        if name in REGISTRY:
            raise ValueError(f"Scenario {name!r} already registered")
        REGISTRY[name] = fn
        return fn

    return deco


def get(name: str) -> ScenarioFn:
    if name not in REGISTRY:
        raise KeyError(f"Unknown scenario: {name!r} (have: {sorted(REGISTRY)})")
    return REGISTRY[name]


def list_names() -> List[str]:
    return sorted(REGISTRY)


# Eagerly import built-in scenarios so they self-register.
from . import options as _options  # noqa: E402,F401
from . import register as _register  # noqa: E402,F401
from . import invite as _invite  # noqa: E402,F401
from . import invite_media as _invite_media  # noqa: E402,F401
from . import ivr as _ivr  # noqa: E402,F401

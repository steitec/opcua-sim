"""Value sources for variables that are not driven by a device model."""
from __future__ import annotations

import math
import random
from typing import Any, Callable

KINDS = ("constant", "sine", "ramp", "square", "noise")


def make_source(spec: dict[str, Any], rng: random.Random) -> Callable[[float], float]:
    """Return ``f(elapsed_seconds)`` for a ``source:`` mapping of a profile."""
    kind = spec.get("kind", "constant")
    offset = float(spec.get("offset", 0.0))
    amplitude = float(spec.get("amplitude", 1.0))
    period = float(spec.get("period", 10.0))
    if kind not in KINDS:
        raise ValueError(f"unknown source kind {kind!r}, expected one of {', '.join(KINDS)}")
    if kind != "constant" and period <= 0:
        raise ValueError("source period must be > 0")
    if kind == "constant":
        value = float(spec.get("value", offset))
        return lambda t: value
    if kind == "sine":
        return lambda t: offset + amplitude * math.sin(2 * math.pi * t / period)
    if kind == "ramp":
        return lambda t: offset + amplitude * ((t % period) / period)
    if kind == "square":
        return lambda t: offset + (amplitude if (t % period) < period / 2 else 0.0)
    return lambda t: offset + rng.gauss(0.0, amplitude)

"""Faults that can be injected at runtime.

Device faults (``inject(device, kind, **params)``):

=================  =========================  ===================================================
kind               parameters                 effect
=================  =========================  ===================================================
bad_status         variable=null,             the variable (or all of them) carries this status
                   status=BadCommunicationError  code; Bad* values read as null, Uncertain* keep
freeze             variable=null              values stop changing, source timestamps stay old
stale              seconds=60                 values go on, but source timestamps lag behind
drift              variable, per_second       a numeric value runs away; adds up
reject_write       variable=null,             client writes answer this status code
                   status=BadUserAccessDenied
method_fail        method=null,               method calls answer this status code
                   status=BadInvalidState
health             state                      forces DeviceHealth: FAILURE, CHECK_FUNCTION,
                                              OFF_SPEC or MAINTENANCE_REQUIRED
<model faults>     see ``opcua-sim profiles`` sensor_break, trip, overload, phase_loss, ...
=================  =========================  ===================================================

Server faults (``inject("server", kind, **params)``):

=================  =========================  ===================================================
kind               parameters                 effect
=================  =========================  ===================================================
delay              seconds                    every response from the server arrives late
blackhole          -                          connections stay open, nothing gets through
drop_connections   -                          all connections are cut now (one shot)
refuse             -                          new connections are closed at once
server_state       state                      ServerStatus/State: Suspended, Test, Failed, ...
service_level      value (0-255)              Server/ServiceLevel for redundancy-aware clients
restart            downtime=2                 server gone, comes back with no sessions or
                                              subscriptions (one shot)
=================  =========================  ===================================================
"""
from __future__ import annotations

import math
from typing import Any

from asyncua import ua

from .models import HEALTH, NORMAL

DEVICE = {
    "bad_status": {"variable": None, "status": "BadCommunicationError"},
    "freeze": {"variable": None},
    "stale": {"seconds": 60.0},
    "drift": {"variable": None, "per_second": None},
    "reject_write": {"variable": None, "status": "BadUserAccessDenied"},
    "method_fail": {"method": None, "status": "BadInvalidState"},
    "health": {"state": None},
}
SERVER = {
    "delay": {"seconds": None},
    "blackhole": {},
    "drop_connections": {},
    "refuse": {},
    "server_state": {"state": None},
    "service_level": {"value": None},
    "restart": {"downtime": 2.0},
}
SERVER_STATES = {state.name: state.value for state in ua.ServerState}


class FaultError(ValueError):
    """Invalid fault kind or parameter."""


def status_code(name: Any) -> int:
    """The number of an OPC UA status code name such as ``BadSensorFailure``."""
    value = getattr(ua.StatusCodes, str(name), None)
    if not isinstance(value, int):
        raise FaultError(f"unknown OPC UA status code {name!r}")
    return value


def check_params(kind: str, defaults: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    """Fill in defaults and refuse parameters this kind does not have."""
    unknown = set(params) - set(defaults)
    if unknown:
        raise FaultError(f"{kind}: unknown parameter(s) {', '.join(sorted(unknown))}")
    return {**defaults, **params}


def _positive(value: Any, what: str, zero: bool = False) -> float:
    if isinstance(value, bool):
        raise FaultError(f"{what} must be a number")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise FaultError(f"{what} must be a number") from None
    if not math.isfinite(number) or number < 0 or (number == 0 and not zero):
        raise FaultError(f"{what} must be a positive number")
    return number


def check_server_kind(kind: str) -> None:
    if kind not in SERVER:
        raise FaultError(f"unknown server fault {kind!r}, expected one of "
                         f"{', '.join(sorted(SERVER))}")


def check_server(kind: str, params: dict[str, Any]) -> dict[str, Any]:
    check_server_kind(kind)
    full = check_params(kind, SERVER[kind], params)
    if kind == "delay":
        full["seconds"] = _positive(full["seconds"], "delay seconds")
    elif kind == "restart":
        full["downtime"] = _positive(full["downtime"], "restart downtime", zero=True)
    elif kind == "server_state":
        if full["state"] not in SERVER_STATES:
            raise FaultError(f"state must be one of {', '.join(SERVER_STATES)}")
    elif kind == "service_level":
        value = full["value"]
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 255:
            raise FaultError("service_level value must be an integer 0-255")
    return full


def check_device(kind: str, params: dict[str, Any], variables: set[str],
                 methods: set[str]) -> dict[str, Any]:
    full = check_params(kind, DEVICE[kind], params)
    if full.get("variable") is not None and full["variable"] not in variables:
        raise FaultError(f"{kind}: no variable {full['variable']!r}")
    if kind == "drift":
        if full["variable"] is None:
            raise FaultError("drift needs a variable")
        try:
            full["per_second"] = float(full["per_second"])
        except (TypeError, ValueError):
            raise FaultError("drift needs a numeric per_second") from None
    elif kind == "stale":
        full["seconds"] = _positive(full["seconds"], "stale seconds")
    elif kind == "method_fail":
        if full["method"] is not None and full["method"] not in methods:
            raise FaultError(f"{kind}: no method {full['method']!r}")
    elif kind == "health":
        if full["state"] not in HEALTH or full["state"] == NORMAL:
            raise FaultError("health state must be one of "
                             f"{', '.join(h for h in HEALTH if h != NORMAL)}")
    if "status" in full:
        status_code(full["status"])
    return full

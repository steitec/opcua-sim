"""One simulated device: variables, model, faults and alarm states, without any networking."""
from __future__ import annotations

import math
import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .faults import DEVICE, FaultError, check_device, check_params
from .models import MODELS, NORMAL, StatusError
from .profile import AlarmSpec, MethodSpec, NUMERIC, Profile, VariableSpec
from .signals import make_source

_INT_RANGES = {"Byte": (0, 255), "Int16": (-2**15, 2**15 - 1), "UInt16": (0, 2**16 - 1),
               "Int32": (-2**31, 2**31 - 1), "UInt32": (0, 2**32 - 1),
               "Int64": (-2**63, 2**63 - 1), "UInt64": (0, 2**64 - 1)}
_ZERO = {"Boolean": False, "String": "", "LocalizedText": ""}


def coerce(value: Any, vtype: str) -> Any:
    """Fit a Python value into an OPC UA built-in type."""
    if vtype == "Boolean":
        return bool(value)
    if vtype in ("String", "LocalizedText"):
        return "" if value is None else str(value)
    number = float(value)
    if vtype in _INT_RANGES:
        low, high = _INT_RANGES[vtype]
        return int(min(high, max(low, 0 if math.isnan(number) else round(number))))
    return number


def hash_name(name: str) -> int:
    """A stable hash: serial numbers must not change between runs."""
    result = 0
    for char in name:
        result = (result * 131 + ord(char)) & 0xFFFFFFFF
    return result


@dataclass
class Sample:
    """What the server publishes for a variable."""
    value: Any
    status: str | None = None
    timestamp: datetime | None = None


@dataclass
class Alarm:
    spec: AlarmSpec
    active: str | None = None       # limit name, or "active" for a discrete alarm
    acked: bool = True
    event_id: bytes = b""
    time: datetime | None = None
    comment: str = ""
    severity: int = 0
    previous_severity: int = 0
    message: str = ""

    @property
    def retain(self) -> bool:
        return self.active is not None or not self.acked


class Device:
    def __init__(self, name: str, profile: Profile, params: dict[str, Any] | None = None,
                 serial_number: str | None = None, seed: int = 0):
        self.name = name
        self.profile = profile
        self.rng = random.Random(f"{seed}:{name}")
        self.identification = dict(profile.identification)
        if serial_number:
            self.identification["serial_number"] = str(serial_number)
        self.identification.setdefault("serial_number", f"SIM-{hash_name(name) % 1_000_000:06d}")
        self.model = None
        if profile.model:
            merged = {**profile.model_params, **(params or {})}
            self.model = MODELS[profile.model](merged, self.rng)
        elif params:
            raise ValueError(f"profile {profile.name} has no model to take params")
        self.variables: dict[str, VariableSpec] = {v.name: v for v in profile.variables}
        self.methods: dict[str, MethodSpec] = {m.name: m for m in profile.methods}
        self.alarms: dict[str, Alarm] = {a.name: Alarm(a) for a in profile.alarms}
        self.faults: dict[str, list[dict[str, Any]]] = {}
        self.forced: dict[str, Any] = {}
        self.written: dict[str, Any] = {}
        self.drift: dict[str, float] = {}
        self.elapsed = 0.0
        self.samples: dict[str, Sample] = {}
        self.requests = 0
        self.pending: list[Alarm] = []
        self._sources: dict[str, Callable[[float], float]] = {
            v.name: make_source(v.source, self.rng) for v in profile.variables if v.source}
        self.step(0.0)

    # -- values ----------------------------------------------------------------------------

    def _raw(self, spec: VariableSpec) -> Any:
        if spec.name in self.forced:
            return self.forced[spec.name]
        if spec.model is not None:
            return self.model.values[spec.model]
        if spec.name in self.written:
            return self.written[spec.name]
        if spec.name in self._sources:
            return self._sources[spec.name](self.elapsed)
        if spec.value is not None:
            return spec.value
        return _ZERO.get(spec.type, 0)

    def step(self, dt: float, now: datetime | None = None) -> list[Alarm]:
        """Advance the simulation; alarms whose state changed end up in ``pending``.

        ``dt=0`` only brings the published state up to date after a write, call or fault.
        A value that did not change keeps its source timestamp and is not published again.
        """
        now = now or datetime.now(timezone.utc)
        self.elapsed += dt
        if self.model is not None:
            self.model.update(dt)
        for fault in self.faults.get("drift", []):
            name = fault["variable"]
            self.drift[name] = self.drift.get(name, 0.0) + fault["per_second"] * dt
        stale = self.faults.get("stale")
        stamp = now - timedelta(seconds=stale[-1]["seconds"]) if stale else now
        for name, spec in self.variables.items():
            status = self._status(spec)
            if self._is_frozen(name) and name in self.samples:
                sample = self.samples[name]
                self.samples[name] = Sample(sample.value, status, sample.timestamp)
                continue
            value = self._raw(spec)
            if name in self.drift and spec.type in NUMERIC:
                value = float(value) + self.drift[name]
            value = coerce(value, spec.type)
            previous = self.samples.get(name)
            if previous is None or previous.value != value or previous.status != status:
                self.samples[name] = Sample(value, status, stamp)
        self.pending.extend(self._evaluate_alarms(now))
        return self.pending

    def _is_frozen(self, name: str) -> bool:
        return any(f["variable"] in (None, name) for f in self.faults.get("freeze", []))

    def _status(self, spec: VariableSpec) -> str | None:
        for fault in reversed(self.faults.get("bad_status", [])):
            if fault["variable"] in (None, spec.name):
                return fault["status"]
        if spec.name not in self.forced and spec.model is not None and self.model is not None:
            return self.model.statuses.get(spec.model)
        return None

    @property
    def health(self) -> str:
        forced = self.faults.get("health")
        if forced:
            return forced[-1]["state"]
        return self.model.health if self.model is not None else NORMAL

    # -- client access ---------------------------------------------------------------------

    def write(self, name: str, value: Any) -> str | None:
        """A client writes a value. Returns a status code name if it is refused."""
        self.requests += 1
        spec = self.variables[name]
        for fault in reversed(self.faults.get("reject_write", [])):
            if fault["variable"] in (None, name):
                return fault["status"]
        if not spec.writable:
            return "BadNotWritable"
        if spec.type in NUMERIC:
            if (spec.min is not None and value < spec.min) or \
                    (spec.max is not None and value > spec.max):
                return "BadOutOfRange"
        if spec.model is not None:
            try:
                self.model.write(spec.model, value)
            except StatusError as error:
                return error.status
        else:
            self.written[name] = value
        return None

    def call(self, name: str, args: list[Any]) -> list[Any]:
        """A client calls a method. Raises :class:`StatusError` if it is refused."""
        self.requests += 1
        for fault in reversed(self.faults.get("method_fail", [])):
            if fault["method"] in (None, name):
                raise StatusError(fault["status"])
        return self.model.call(self.methods[name].model, *args)

    def set(self, name: str, value: Any) -> None:
        """Force a value from outside (control channel); ``None`` goes back to normal."""
        spec = self.variables.get(name)
        if spec is None:
            raise FaultError(f"device {self.name} has no variable {name!r}")
        if value is None:
            self.forced.pop(name, None)
            return
        try:
            self.forced[name] = coerce(value, spec.type)
        except (TypeError, ValueError):
            raise FaultError(f"{name}: not a {spec.type}: {value!r}") from None

    def get(self, name: str) -> Any:
        if name not in self.samples:
            raise FaultError(f"device {self.name} has no variable {name!r}")
        return self.samples[name].value

    # -- faults ----------------------------------------------------------------------------

    def fault_kinds(self) -> list[str]:
        return sorted({*DEVICE, *(self.model.FAULTS if self.model else ())})

    def inject(self, kind: str, params: dict[str, Any] | None = None) -> None:
        params = dict(params or {})
        if kind in DEVICE:
            full = check_device(kind, params, set(self.variables), set(self.methods))
            self.faults.setdefault(kind, []).append(full)
        elif self.model is not None and kind in self.model.FAULTS:
            full = check_params(kind, self.model.FAULTS[kind], params)
            try:
                self.model.inject(kind, full)
            except ValueError as error:
                raise FaultError(str(error)) from None
        else:
            raise FaultError(f"device {self.name} has no fault {kind!r}, expected one of "
                             f"{', '.join(self.fault_kinds())}")

    def clear(self, kind: str | None = None) -> None:
        if kind is None:
            self.faults.clear()
            self.drift.clear()
            if self.model is not None:
                self.model.clear()
            return
        if kind not in self.fault_kinds():
            raise FaultError(f"device {self.name} has no fault {kind!r}")
        self.faults.pop(kind, None)
        if kind == "drift":
            self.drift.clear()
        if self.model is not None:
            self.model.clear(kind)

    def active_faults(self) -> dict[str, Any]:
        active: dict[str, Any] = {kind: items for kind, items in self.faults.items() if items}
        if self.model is not None:
            active.update(self.model.active)
        return active

    # -- alarms ----------------------------------------------------------------------------

    def _evaluate_alarms(self, now: datetime) -> list[Alarm]:
        changed = []
        for alarm in self.alarms.values():
            spec = alarm.spec
            if spec.kind == "level":
                sample = self.samples[spec.variable]
                if sample.status is not None and sample.status.startswith("Bad"):
                    continue  # no valid value: the alarm holds its state
                state = _level_state(float(sample.value), spec, alarm.active)
                severity = spec.severities[state] if state else 0
            else:
                active = self.model.flags.get(spec.when, False) if self.model else False
                state = "active" if active else None
                severity = spec.severity if active else 0
            if state == alarm.active:
                continue
            alarm.previous_severity = alarm.severity
            was_inactive = alarm.active is None
            alarm.active = state
            if state is not None:
                alarm.severity = severity
                if was_inactive or severity > alarm.previous_severity:
                    alarm.acked = False
            alarm.message = spec.message if state else f"{spec.message}: back to normal"
            alarm.time = now
            alarm.event_id = uuid.uuid4().bytes
            changed.append(alarm)
        return changed

    def acknowledge(self, name: str, event_id: bytes, comment: str = "") -> str | None:
        """OPC UA Acknowledge: returns a status code name if it is refused."""
        alarm = self.alarms[name]
        if event_id != alarm.event_id:
            return "BadEventIdUnknown"
        if alarm.acked:
            return "BadConditionBranchAlreadyAcked"
        alarm.acked = True
        alarm.comment = comment
        alarm.time = datetime.now(timezone.utc)
        alarm.event_id = uuid.uuid4().bytes
        return None


def _level_state(value: float, spec: AlarmSpec, current: str | None) -> str | None:
    """The limit a value violates; ``deadband`` keeps an active limit on the way back."""
    limits = spec.limits
    high_side = current in ("high", "high_high")
    low_side = current in ("low", "low_low")
    for limit in ("high_high", "high"):
        # Falling from high_high to high still counts as leaving high_high: band only there.
        band = spec.deadband if current == limit or (limit == "high" and high_side) else 0.0
        if limit in limits and value >= limits[limit] - band:
            return limit
    for limit in ("low_low", "low"):
        band = spec.deadband if current == limit or (limit == "low" and low_side) else 0.0
        if limit in limits and value <= limits[limit] + band:
            return limit
    return None

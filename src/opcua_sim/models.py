"""Device behaviour. A model has no idea about OPC UA: values, methods, flags and health."""
from __future__ import annotations

import random
from typing import Any, Callable

NORMAL = "NORMAL"
FAILURE = "FAILURE"
CHECK_FUNCTION = "CHECK_FUNCTION"
OFF_SPEC = "OFF_SPEC"
MAINTENANCE_REQUIRED = "MAINTENANCE_REQUIRED"
#: Same names and numbers as DeviceHealthEnumeration of OPC UA DI, which are the NE 107 states.
HEALTH = (NORMAL, FAILURE, CHECK_FUNCTION, OFF_SPEC, MAINTENANCE_REQUIRED)


class StatusError(Exception):
    """A write or method call is answered with this OPC UA status code."""

    def __init__(self, status: str):
        super().__init__(status)
        self.status = status


class Model:
    #: value name -> writable by a client
    VALUES: dict[str, bool] = {}
    #: method name -> attribute name
    METHODS: dict[str, str] = {}
    #: flags alarms can watch
    FLAGS: tuple[str, ...] = ()
    #: fault kind -> parameter names with defaults
    FAULTS: dict[str, dict[str, Any]] = {}
    DEFAULTS: dict[str, Any] = {}

    def __init__(self, params: dict[str, Any] | None = None, rng: random.Random | None = None):
        params = dict(params or {})
        unknown = set(params) - set(self.DEFAULTS)
        if unknown:
            raise ValueError(f"unknown model parameter(s) {', '.join(sorted(unknown))}")
        self.params = {**self.DEFAULTS, **params}
        self.rng = rng or random.Random(0)
        self.values: dict[str, Any] = {}
        self.statuses: dict[str, str] = {}
        self.flags: dict[str, bool] = {flag: False for flag in self.FLAGS}
        self.health = NORMAL
        self.active: dict[str, dict[str, Any]] = {}
        self.setup()
        self.update(0.0)

    def setup(self) -> None:
        raise NotImplementedError

    def step(self, dt: float) -> None:
        raise NotImplementedError

    def update(self, dt: float) -> None:
        self.statuses = {}
        self.health = NORMAL
        self.flags = {flag: False for flag in self.FLAGS}
        self.step(dt)

    def write(self, name: str, value: Any) -> None:
        self.values[name] = value

    def call(self, method: str, *args: Any) -> list[Any]:
        function: Callable[..., Any] = getattr(self, self.METHODS[method])
        result = function(*args)
        return [] if result is None else list(result)

    def inject(self, kind: str, params: dict[str, Any]) -> None:
        self.active[kind] = {**self.FAULTS[kind], **params}

    def clear(self, kind: str | None = None) -> None:
        if kind is None:
            self.active.clear()
        else:
            self.active.pop(kind, None)


class Transmitter(Model):
    """Process transmitter (pressure, level): a noisy value, 4-20 mA, zero trim, self test."""

    VALUES = {"pv": False, "output_ma": False, "lower_range": True, "upper_range": True,
              "damping": True, "zero_offset": False, "process": True}
    METHODS = {"zero_trim": "zero_trim", "self_test": "self_test"}
    FLAGS = ("sensor_break",)
    FAULTS = {"sensor_break": {}, "over_range": {}, "maintenance_due": {}}
    DEFAULTS = {"process": 6.0, "lower_range": 0.0, "upper_range": 16.0, "noise": 0.005,
                "damping": 0.5, "self_test_seconds": 3.0}

    def setup(self) -> None:
        if self.params["lower_range"] >= self.params["upper_range"]:
            raise ValueError("lower_range must be below upper_range")
        self.values.update(pv=self.params["process"], output_ma=4.0,
                           lower_range=self.params["lower_range"],
                           upper_range=self.params["upper_range"],
                           damping=self.params["damping"], zero_offset=0.0,
                           process=self.params["process"])
        self.self_test_left = 0.0
        self.damped = float(self.params["process"])
        self.noise = 0.0

    def step(self, dt: float) -> None:
        low, high = self.values["lower_range"], self.values["upper_range"]
        span = high - low
        target = self.values["process"] + self.values["zero_offset"]
        if "over_range" in self.active:
            target = high + 0.1 * span
        tau = self.values["damping"]
        if dt > 0:
            self.damped += (target - self.damped) * min(1.0, dt / tau if tau > 0 else 1.0)
            self.noise = self.rng.gauss(0.0, self.params["noise"])
        pv = self.damped + self.noise
        self.values["pv"] = pv
        self.self_test_left = max(0.0, self.self_test_left - dt)
        if "sensor_break" in self.active:
            self.flags["sensor_break"] = True
            self.health = FAILURE
            self.statuses["pv"] = "BadSensorFailure"
            self.values["output_ma"] = 3.6  # NAMUR NE 43 failure current, low
            return
        if self.self_test_left > 0:
            self.health = CHECK_FUNCTION
            self.statuses["pv"] = "UncertainInitialValue"
            self.values["output_ma"] = 21.0  # NE 43 failure current, high
            return
        milliamps = 4.0 + 16.0 * (pv - low) / span
        self.values["output_ma"] = min(20.5, max(3.8, milliamps))  # NE 43 saturation
        if "over_range" in self.active or not low <= pv <= high:
            self.health = OFF_SPEC
            self.statuses["pv"] = "UncertainEngineeringUnitsExceeded"
        elif "maintenance_due" in self.active:
            self.health = MAINTENANCE_REQUIRED

    def write(self, name: str, value: Any) -> None:
        low = value if name == "lower_range" else self.values["lower_range"]
        high = value if name == "upper_range" else self.values["upper_range"]
        if low >= high:
            raise StatusError("BadOutOfRange")
        super().write(name, value)

    def zero_trim(self) -> list[Any]:
        if "sensor_break" in self.active:
            raise StatusError("BadInvalidState")
        self.values["zero_offset"] -= self.damped - self.values["lower_range"]
        self.damped = self.values["lower_range"]
        return [self.values["zero_offset"]]

    def self_test(self) -> None:
        if self.self_test_left > 0:
            raise StatusError("BadInvalidState")
        self.self_test_left = float(self.params["self_test_seconds"])


class TemperatureController(Model):
    """PI loop on a first-order plant. A broken sensor stops the loop and fails safe."""

    VALUES = {"pv": False, "setpoint": True, "output": False, "manual_output": True,
              "running": False, "manual": True}
    METHODS = {"start": "start", "stop": "stop"}
    FLAGS = ("sensor_break",)
    FAULTS = {"sensor_break": {}, "heater_failure": {}}
    DEFAULTS = {"ambient": 22.0, "max_temperature": 300.0, "time_constant": 60.0, "setpoint": 60.0,
                "kp": 8.0, "ki": 0.2, "running": False}

    def setup(self) -> None:
        if self.params["time_constant"] <= 0:
            raise ValueError("time_constant must be positive")
        if self.params["max_temperature"] <= self.params["ambient"]:
            raise ValueError("max_temperature must be above ambient")
        self.values.update(pv=self.params["ambient"], setpoint=self.params["setpoint"], output=0.0,
                           manual_output=0.0, running=bool(self.params["running"]), manual=False)
        self.integral = 0.0

    def step(self, dt: float) -> None:
        values = self.values
        broken = "sensor_break" in self.active
        if not values["running"] or broken:
            output = 0.0
            self.integral = 0.0
        elif values["manual"]:
            output = values["manual_output"]
        else:
            error = values["setpoint"] - values["pv"]
            self.integral = min(100.0, max(-100.0, self.integral + error * self.params["ki"] * dt))
            output = min(100.0, max(0.0, self.params["kp"] * error + self.integral))
        values["output"] = output
        ambient, top = self.params["ambient"], self.params["max_temperature"]
        heat = 0.0 if "heater_failure" in self.active else output
        target = ambient + (top - ambient) * heat / 100.0
        values["pv"] += (target - values["pv"]) * min(1.0, dt / self.params["time_constant"])
        if broken:
            self.flags["sensor_break"] = True
            self.health = FAILURE
            self.statuses["pv"] = "BadSensorFailure"
        elif "heater_failure" in self.active and values["running"]:
            self.health = OFF_SPEC

    def write(self, name: str, value: Any) -> None:
        if name == "manual_output" and not 0 <= value <= 100:
            raise StatusError("BadOutOfRange")
        super().write(name, value)

    def start(self) -> None:
        if "sensor_break" in self.active:
            raise StatusError("BadInvalidState")
        self.values["running"] = True

    def stop(self) -> None:
        self.values["running"] = False


class Drive(Model):
    """Frequency converter: Start, Stop, ramps; a trip latches until Reset."""

    STOPPED, RUNNING, STOPPING, TRIPPED = range(4)
    VALUES = {"speed_setpoint": True, "speed": False, "current": False, "state": False,
              "fault_code": False, "running": False}
    METHODS = {"start": "start", "stop": "stop", "reset": "reset"}
    FLAGS = ("tripped", "overload")
    FAULTS = {"trip": {"code": 1}, "overload": {}, "fan_worn": {}}
    DEFAULTS = {"max_speed": 50.0, "ramp": 10.0, "rated_current": 8.0, "speed_setpoint": 30.0}

    def setup(self) -> None:
        if self.params["max_speed"] <= 0 or self.params["ramp"] <= 0:
            raise ValueError("max_speed and ramp must be positive")
        self.values.update(speed_setpoint=self.params["speed_setpoint"], speed=0.0, current=0.0,
                           state=self.STOPPED, fault_code=0, running=False)
        self.fault_latched = False

    def step(self, dt: float) -> None:
        values = self.values
        if "trip" in self.active and not self.fault_latched:
            self.fault_latched = True
            values["fault_code"] = int(self.active["trip"]["code"])
            values["state"] = self.TRIPPED
        ramp = self.params["ramp"] * dt
        if values["state"] == self.RUNNING:
            target = min(values["speed_setpoint"], self.params["max_speed"])
            delta = target - values["speed"]
            values["speed"] += max(-ramp, min(ramp, delta))
        elif values["state"] == self.STOPPING:
            values["speed"] = max(0.0, values["speed"] - ramp)
            if values["speed"] == 0.0:
                values["state"] = self.STOPPED
        elif values["state"] == self.TRIPPED:
            values["speed"] = 0.0  # the output is off
        values["running"] = values["state"] == self.RUNNING
        ratio = values["speed"] / self.params["max_speed"]
        factor = 1.6 if "overload" in self.active else 1.0
        values["current"] = self.params["rated_current"] * (0.3 + 0.7 * ratio) * factor \
            if values["speed"] > 0 else 0.0
        if values["state"] == self.TRIPPED:
            self.flags["tripped"] = True
            self.health = FAILURE
        elif "overload" in self.active:
            self.flags["overload"] = True
            self.health = OFF_SPEC
        elif "fan_worn" in self.active:
            self.health = MAINTENANCE_REQUIRED

    def write(self, name: str, value: Any) -> None:
        if name == "speed_setpoint" and not 0 <= value <= self.params["max_speed"]:
            raise StatusError("BadOutOfRange")
        super().write(name, value)

    def start(self) -> None:
        if self.values["state"] == self.TRIPPED:
            raise StatusError("BadInvalidState")
        self.values["state"] = self.RUNNING

    def stop(self) -> None:
        if self.values["state"] == self.RUNNING:
            self.values["state"] = self.STOPPING

    def reset(self) -> None:
        if self.values["state"] != self.TRIPPED:
            raise StatusError("BadInvalidState")
        if "trip" in self.active:
            raise StatusError("BadInvalidState")  # the cause is still there
        self.fault_latched = False
        self.values.update(state=self.STOPPED, fault_code=0)


class EnergyMeter(Model):
    """Three-phase meter: voltages, currents, power and a counter that really counts."""

    VALUES = {"voltage_l1": False, "voltage_l2": False, "voltage_l3": False, "current": True,
              "power": False, "energy": False, "power_factor": True}
    METHODS = {"reset_energy": "reset_energy"}
    FLAGS = ("phase_loss",)
    FAULTS = {"phase_loss": {"phase": 1}}
    DEFAULTS = {"voltage": 230.0, "current": 12.0, "power_factor": 0.95, "noise": 0.2,
                "energy": 0.0}

    def setup(self) -> None:
        self.values.update(voltage_l1=self.params["voltage"], voltage_l2=self.params["voltage"],
                           voltage_l3=self.params["voltage"], current=self.params["current"],
                           power=0.0, energy=float(self.params["energy"]),
                           power_factor=self.params["power_factor"])
        self.noise = [0.0, 0.0, 0.0]

    def step(self, dt: float) -> None:
        values = self.values
        lost = int(self.active["phase_loss"]["phase"]) if "phase_loss" in self.active else None
        total = 0.0
        for phase in (1, 2, 3):
            if dt > 0:
                self.noise[phase - 1] = self.rng.gauss(0.0, self.params["noise"])
            voltage = 0.0 if phase == lost else self.params["voltage"] + self.noise[phase - 1]
            values[f"voltage_l{phase}"] = voltage
            total += voltage * values["current"] * values["power_factor"]
        values["power"] = total
        values["energy"] += total * dt / 3_600_000.0  # kWh
        if lost is not None:
            self.flags["phase_loss"] = True
            self.health = OFF_SPEC
            self.statuses[f"voltage_l{lost}"] = "BadNoCommunication"

    def inject(self, kind: str, params: dict[str, Any]) -> None:
        if kind == "phase_loss" and int(params.get("phase", 1)) not in (1, 2, 3):
            raise ValueError("phase must be 1, 2 or 3")
        super().inject(kind, params)

    def reset_energy(self) -> list[Any]:
        before = self.values["energy"]
        self.values["energy"] = 0.0
        return [before]


class PlcMachine(Model):
    """A small PLC program: run and reset bits, a ramp, a part counter, a latching fault.

    PLC servers publish the variables of whatever program runs on them, so the profiles of
    real PLCs share this program and differ in how their server presents it.
    """

    VALUES = {"run": True, "reset": True, "speed_setpoint": True, "speed": False,
              "temperature": False, "parts": False, "fault": False, "fault_code": False,
              "mode_text": False, "error_text": False}
    FLAGS = ()
    FAULTS = {"trip": {"code": 1}, "plc_stop": {}}
    DEFAULTS = {"ramp": 200.0, "max_speed": 1500.0, "speed_setpoint": 1000.0, "ambient": 22.0,
                "parts_per_revolution": 0.01}

    def setup(self) -> None:
        if self.params["max_speed"] <= 0 or self.params["ramp"] <= 0:
            raise ValueError("max_speed and ramp must be positive")
        self.values.update(run=False, reset=False, speed_setpoint=self.params["speed_setpoint"],
                           speed=0.0, temperature=self.params["ambient"], parts=0, fault=False,
                           fault_code=0, mode_text="RUN", error_text="NoError")
        self.parts = 0.0
        self.reset_before = False

    def step(self, dt: float) -> None:
        values = self.values
        if "plc_stop" in self.active:
            # Program stopped: nothing is calculated any more, the outputs are off.
            values.update(speed=0.0, mode_text="PROGRAM")
            self.health = CHECK_FUNCTION
            return
        values["mode_text"] = "RUN"
        if "trip" in self.active and not values["fault"]:
            values.update(fault=True, fault_code=int(self.active["trip"]["code"]))
        if values["reset"] and not self.reset_before and "trip" not in self.active:
            values.update(fault=False, fault_code=0)
        self.reset_before = values["reset"]
        target = 0.0
        if values["run"] and not values["fault"]:
            target = min(max(0.0, values["speed_setpoint"]), self.params["max_speed"])
        if values["fault"]:
            values["speed"] = 0.0
        else:
            ramp = self.params["ramp"] * dt
            values["speed"] += max(-ramp, min(ramp, target - values["speed"]))
        self.parts += values["speed"] / 60.0 * dt * self.params["parts_per_revolution"]
        values["parts"] = int(self.parts)
        warm = self.params["ambient"] + 40.0 * values["speed"] / self.params["max_speed"]
        values["temperature"] += (warm - values["temperature"]) * min(1.0, dt / 10.0)
        values["error_text"] = "MajorFault" if values["fault"] else "NoError"
        if values["fault"]:
            self.health = FAILURE


class BarcodeReader(Model):
    """A stationary reader: scans on its own while active and counts good and bad reads."""

    VALUES = {"scan_active": True, "device_status": False, "last_scan_data": False,
              "temperature": False, "total": False, "ok": False, "not_ok": False, "portion": False}
    METHODS = {"scan_stop": "scan_stop", "reset": "reset"}
    FAULTS = {"read_fail": {}, "device_error": {}}
    DEFAULTS = {"codes": ["4006381333931", "0123456789012", "LEUZE-TEST-01"], "interval": 1.0,
                "temperature": 38.0}
    IDLE, ERROR, SCANNING, BUSY = range(4)      # DeviceStatusEnumeration of OPC UA AutoID

    def setup(self) -> None:
        codes, interval = self.params["codes"], self.params["interval"]
        if not isinstance(codes, list) or not codes:
            raise ValueError("codes must be a non-empty list")
        if interval <= 0:
            raise ValueError("interval must be positive")
        self.values.update(scan_active=True, device_status=self.SCANNING, last_scan_data="",
                           temperature=float(self.params["temperature"]), total=0, ok=0, not_ok=0,
                           portion=0.0)
        self.waited = 0.0
        self.next_code = 0

    def step(self, dt: float) -> None:
        values = self.values
        if "device_error" in self.active:
            values["device_status"] = self.ERROR
            self.health = FAILURE
            return
        values["device_status"] = self.SCANNING if values["scan_active"] else self.IDLE
        if not values["scan_active"]:
            return
        self.waited += dt
        while self.waited >= self.params["interval"]:
            self.waited -= self.params["interval"]
            values["total"] += 1
            if "read_fail" in self.active:
                values["not_ok"] += 1
                values["last_scan_data"] = ""
            else:
                values["ok"] += 1
                codes = self.params["codes"]
                values["last_scan_data"] = str(codes[self.next_code % len(codes)])
                self.next_code += 1
            values["portion"] = 100.0 * values["ok"] / values["total"]
        if "read_fail" in self.active:
            self.health = OFF_SPEC

    def scan_stop(self) -> None:
        self.values["scan_active"] = False

    def reset(self) -> None:
        if "device_error" in self.active:
            raise StatusError("BadInvalidState")
        self.values.update(total=0, ok=0, not_ok=0, portion=0.0, scan_active=True)
        self.waited = 0.0


MODELS: dict[str, type[Model]] = {
    "transmitter": Transmitter,
    "temperature_controller": TemperatureController,
    "drive": Drive,
    "energy_meter": EnergyMeter,
    "plc_machine": PlcMachine,
    "barcode_reader": BarcodeReader,
}

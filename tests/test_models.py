"""Device behaviour, stepped by hand: no server, no waiting."""
import pytest

from opcua_sim import load_profile
from opcua_sim.device import Device
from opcua_sim.models import StatusError


def device(profile, **params):
    return Device("d", load_profile(profile), params)


def run(target, seconds, dt=0.1):
    for _ in range(round(seconds / dt)):
        target.step(dt)


def test_drive_ramps_at_the_set_rate_and_stops():
    drive = device("drive", ramp=10.0)
    drive.call("Start", [])
    run(drive, 1.0)
    assert drive.get("Speed") == pytest.approx(10.0)
    run(drive, 10.0)
    assert drive.get("Speed") == pytest.approx(30.0)
    drive.call("Stop", [])
    run(drive, 1.0)
    assert drive.get("State") == 2
    run(drive, 5.0)
    assert (drive.get("State"), drive.get("Speed"), drive.get("MotorCurrent")) == (0, 0, 0)


def test_drive_trip_latches_and_stops_the_motor():
    drive = device("drive")
    drive.call("Start", [])
    run(drive, 1.0)
    drive.inject("trip", {"code": 9})
    run(drive, 0.1)
    assert (drive.get("State"), drive.get("FaultCode"), drive.get("Speed")) == (3, 9, 0)
    assert drive.health == "FAILURE"
    drive.clear("trip")
    run(drive, 1.0)
    assert drive.get("State") == 3
    drive.call("Reset", [])
    run(drive, 0.1)
    assert (drive.get("State"), drive.get("FaultCode"), drive.health) == (0, 0, "NORMAL")


def test_controller_reaches_setpoint_and_breaks_safely():
    oven = device("temperature-controller", time_constant=5.0, setpoint=80.0)
    oven.call("Start", [])
    run(oven, 60.0)
    assert oven.get("ProcessValue") == pytest.approx(80.0, abs=1.0)
    oven.inject("sensor_break", {})
    run(oven, 0.1)
    assert oven.get("Output") == 0
    assert oven.samples["ProcessValue"].status == "BadSensorFailure"
    with pytest.raises(StatusError):
        oven.call("Start", [])


def test_controller_manual_mode_and_heater_failure():
    oven = device("temperature-controller", time_constant=1.0)
    oven.call("Start", [])
    assert oven.write("Manual", True) is None
    assert oven.write("ManualOutput", 50.0) is None
    assert oven.write("ManualOutput", 150.0) == "BadOutOfRange"
    run(oven, 10.0)
    assert oven.get("Output") == 50.0
    assert oven.get("ProcessValue") == pytest.approx(161.0, abs=1.0)
    oven.inject("heater_failure", {})
    run(oven, 10.0)
    assert oven.get("ProcessValue") == pytest.approx(22.0, abs=1.0)
    assert oven.health == "OFF_SPEC"


def test_transmitter_current_and_zero_trim():
    pt = device("pressure-transmitter", process=8.0, noise=0.0)
    run(pt, 0.2)
    assert pt.get("OutputCurrent") == pytest.approx(12.0)
    assert pt.call("ZeroTrim", [])[0] == pytest.approx(-8.0)
    run(pt, 0.2)
    assert pt.get("Pressure") == pytest.approx(0.0)
    assert pt.get("OutputCurrent") == pytest.approx(4.0)


def test_transmitter_refuses_an_empty_range():
    pt = device("pressure-transmitter")
    assert pt.write("LowerRangeValue", 16.0) == "BadOutOfRange"
    assert pt.write("UpperRangeValue", 0.0) == "BadOutOfRange"
    run(pt, 0.2)
    assert pt.get("OutputCurrent") > 4


def test_transmitter_saturates_like_ne43():
    pt = device("pressure-transmitter", noise=0.0)
    pt.inject("over_range", {})
    run(pt, 5.0)
    assert pt.get("OutputCurrent") == 20.5
    pt.clear()
    pt.inject("maintenance_due", {})
    run(pt, 5.0)
    assert pt.health == "MAINTENANCE_REQUIRED"


def test_meter_counts_and_loses_a_phase():
    meter = device("energy-meter", current=10.0, power_factor=1.0, noise=0.0)
    run(meter, 3.6)
    assert meter.get("ActivePower") == pytest.approx(6900.0)
    assert meter.get("Energy") == pytest.approx(6.9 / 1000, rel=0.01)
    meter.inject("phase_loss", {"phase": 2})
    run(meter, 0.1)
    assert meter.get("VoltageL2") == 0
    assert meter.get("ActivePower") == pytest.approx(4600.0)
    assert meter.samples["VoltageL2"].status == "BadNoCommunication"
    assert meter.health == "OFF_SPEC"


def test_alarm_does_not_ask_for_a_new_ack_when_it_falls_back_a_limit():
    pt = device("pressure-transmitter")
    pt.set("Pressure", 15.5)
    alarm = pt.alarms["PressureLevel"]
    pt.step(0.1)
    assert alarm.active == "high_high"
    assert pt.acknowledge("PressureLevel", alarm.event_id) is None
    pt.set("Pressure", 13.0)
    pt.step(0.1)
    assert (alarm.active, alarm.acked) == ("high", True)
    pt.set("Pressure", 15.5)
    pt.step(0.1)
    assert (alarm.active, alarm.acked) == ("high_high", False)


def test_bad_value_holds_the_level_alarm():
    pt = device("pressure-transmitter")
    pt.set("Pressure", 13.0)
    pt.step(0.1)
    assert pt.alarms["PressureLevel"].active == "high"
    pt.inject("bad_status", {"variable": "Pressure"})
    pt.set("Pressure", 5.0)
    pt.step(0.1)
    assert pt.alarms["PressureLevel"].active == "high"


def test_forced_value_goes_back_to_the_simulation():
    pt = device("pressure-transmitter", noise=0.0)
    pt.set("Pressure", 2.0)
    pt.step(0.1)
    assert pt.get("Pressure") == 2.0
    pt.set("Pressure", None)
    pt.step(0.1)
    assert pt.get("Pressure") == pytest.approx(6.0)

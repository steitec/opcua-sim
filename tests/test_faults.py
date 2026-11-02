"""Injected faults as a client experiences them."""
import asyncio
import time

import pytest
from asyncua import ua
from asyncua.ua import uaerrors

from conftest import Sync
from opcua_sim import Bench
from opcua_sim.faults import FaultError

BROKEN = (ConnectionError, asyncio.TimeoutError, uaerrors.UaError, OSError)


def test_sensor_break_is_bad_status_failure_and_ne43_current(benches, connect):
    bench = benches("pressure-transmitter", name="pt")
    client = connect(bench)
    bench.inject("pt", "sensor_break")
    client.eventually(lambda: client.value("pt.DeviceHealth") == 1)
    data = client.data("pt.Pressure")
    assert data.StatusCode.value == ua.StatusCodes.BadSensorFailure
    assert data.Value is None or data.Value.Value is None
    assert client.value("pt.OutputCurrent") == 3.6
    bench.clear("pt")
    client.eventually(lambda: client.data("pt.Pressure").StatusCode.is_good())
    assert client.value("pt.DeviceHealth") == 0


def test_over_range_is_uncertain_with_value_and_off_spec(benches, connect):
    bench = benches("pressure-transmitter", name="pt")
    client = connect(bench)
    bench.inject("pt", "over_range")
    client.eventually(lambda: client.value("pt.DeviceHealth") == 3)
    client.eventually(lambda: client.data("pt.Pressure").StatusCode.value ==
                      ua.StatusCodes.UncertainEngineeringUnitsExceeded)
    assert client.data("pt.Pressure").Value.Value is not None


def test_self_test_shows_check_function(benches, connect):
    bench = Bench.single("pressure-transmitter", name="pt", params={"self_test_seconds": 0.5})
    client = connect(benches(bench))
    client.call("pt", "StartSelfTest")
    client.eventually(lambda: client.value("pt.DeviceHealth") == 2)
    assert client.value("pt.OutputCurrent") == 21.0
    with pytest.raises(uaerrors.BadInvalidState):
        client.call("pt", "StartSelfTest")
    client.eventually(lambda: client.value("pt.DeviceHealth") == 0)


def test_bad_status_on_one_variable_with_chosen_code(benches, connect):
    bench = benches("energy-meter", name="m")
    client = connect(bench)
    bench.inject("m", "bad_status", variable="Energy", status="BadDeviceFailure")
    client.eventually(lambda: client.data("m.Energy").StatusCode.value ==
                      ua.StatusCodes.BadDeviceFailure)
    assert client.data("m.ActivePower").StatusCode.is_good()


def test_freeze_stops_values_and_timestamps(benches, connect):
    bench = benches("energy-meter", name="m")
    client = connect(bench)
    bench.inject("m", "freeze")
    client.pump(0.3)
    first = client.data("m.VoltageL1")
    client.pump(0.5)
    second = client.data("m.VoltageL1")
    assert second.Value.Value == first.Value.Value
    assert second.SourceTimestamp == first.SourceTimestamp
    bench.clear("m", "freeze")
    client.eventually(lambda: client.data("m.VoltageL1").SourceTimestamp != first.SourceTimestamp)


def test_stale_makes_source_timestamps_old(benches, connect):
    bench = benches("energy-meter", name="m")
    client = connect(bench)
    bench.inject("m", "stale", seconds=120)
    client.pump(0.3)
    data = client.data("m.VoltageL1")
    assert (data.ServerTimestamp - data.SourceTimestamp).total_seconds() > 100


def test_drift_runs_away(benches, connect):
    bench = benches("drive", name="pump")
    client = connect(bench)
    before = client.value("pump.MotorCurrent")
    bench.inject("pump", "drift", variable="MotorCurrent", per_second=20.0)
    client.eventually(lambda: client.value("pump.MotorCurrent") > before + 5)
    bench.clear("pump", "drift")
    client.eventually(lambda: client.value("pump.MotorCurrent") == before)


def test_reject_write_and_method_fail(benches, connect):
    bench = benches("drive", name="pump")
    client = connect(bench)
    bench.inject("pump", "reject_write")
    with pytest.raises(uaerrors.BadUserAccessDenied):
        client.write("pump.SpeedSetpoint", 1.0)
    bench.clear("pump", "reject_write")
    client.write("pump.SpeedSetpoint", 1.0)
    bench.inject("pump", "method_fail", method="Start", status="BadOutOfService")
    with pytest.raises(uaerrors.BadOutOfService):
        client.call("pump", "Start")
    client.call("pump", "Stop")


def test_forced_health(benches, connect):
    bench = benches("drive", name="pump")
    client = connect(bench)
    bench.inject("pump", "health", state="MAINTENANCE_REQUIRED")
    client.eventually(lambda: client.value("pump.DeviceHealth") == 4)


@pytest.mark.parametrize("target,kind,params", [
    ("pump", "nonsense", {}),
    ("nobody", "trip", {}),
    ("server", "nonsense", {}),
    ("pump", "bad_status", {"status": "BadNonsense"}),
    ("pump", "bad_status", {"variable": "Nonsense"}),
    ("pump", "bad_status", {"oops": 1}),
    ("pump", "drift", {"variable": "Speed"}),
    ("pump", "method_fail", {"method": "Nonsense"}),
    ("pump", "health", {"state": "NORMAL"}),
    ("server", "delay", {"seconds": -1}),
    ("server", "server_state", {"state": "Nonsense"}),
    ("server", "service_level", {"value": 300}),
])
def test_invalid_faults_are_refused(benches, target, kind, params):
    bench = benches("drive", name="pump")
    with pytest.raises(FaultError):
        bench.inject(target, kind, **params)


# -- server faults -------------------------------------------------------------------------


def test_server_state_and_service_level(benches, connect):
    bench = benches("drive", name="pump")
    client = connect(bench)
    state = client.client.get_node(ua.ObjectIds.Server_ServerStatus_State)
    level = client.client.get_node(ua.ObjectIds.Server_ServiceLevel)
    assert client.run(state.read_value()) == ua.ServerState.Running
    bench.inject("server", "server_state", state="Suspended")
    bench.inject("server", "service_level", value=10)
    assert client.run(state.read_value()) == ua.ServerState.Suspended
    assert client.run(level.read_value()) == 10
    bench.clear("server")
    assert client.run(state.read_value()) == ua.ServerState.Running
    assert client.run(level.read_value()) == 255


def test_delay(benches, connect):
    bench = benches("drive", name="pump")
    client = connect(bench)
    bench.inject("server", "delay", seconds=0.4)
    start = time.monotonic()
    client.value("pump.Speed")
    assert time.monotonic() - start >= 0.35
    bench.clear("server", "delay")
    start = time.monotonic()
    client.value("pump.Speed")
    assert time.monotonic() - start < 0.3


def test_drop_connections_breaks_the_client_and_it_can_reconnect(benches, connect):
    bench = benches("drive", name="pump")
    client = connect(bench)
    bench.inject("server", "drop_connections")
    with pytest.raises(BROKEN):
        for _ in range(20):
            client.value("pump.Speed")
            client.pump(0.05)
    assert connect(bench).value("pump.Speed") == 0


def test_blackhole_times_out_and_recovers(benches, connect):
    bench = benches("drive", name="pump")
    client = connect(bench, timeout=0.5)
    bench.inject("server", "blackhole")
    with pytest.raises((asyncio.TimeoutError, ConnectionError)):
        client.value("pump.Speed")
    bench.clear("server", "blackhole")
    assert connect(bench).value("pump.Speed") == 0


def test_refuse_new_connections(benches, connect):
    bench = benches("drive", name="pump")
    client = connect(bench)
    bench.inject("server", "refuse")
    fresh = Sync(bench.url, timeout=1)
    try:
        with pytest.raises(BROKEN):
            fresh.connect()
    finally:
        fresh.close()
    assert client.value("pump.Speed") == 0   # existing connections live on
    bench.clear("server")
    assert connect(bench).value("pump.Speed") == 0


def test_restart_loses_sessions_keeps_device_state_and_port(benches, connect):
    bench = benches("drive", name="pump")
    client = connect(bench)
    client.write("pump.SpeedSetpoint", 11.0)
    port = bench.port
    bench.inject("server", "restart", downtime=0.2)
    bench.wait_restarted()
    with pytest.raises(BROKEN):
        client.value("pump.Speed")
    assert bench.port == port
    assert connect(bench).value("pump.SpeedSetpoint") == 11.0


def test_restart_keeps_status_faults(benches, connect):
    bench = benches("drive", name="pump")
    bench.inject("server", "server_state", state="Test")
    bench.inject("server", "restart", downtime=0)
    bench.wait_restarted()
    client = connect(bench)
    state = client.client.get_node(ua.ObjectIds.Server_ServerStatus_State)
    assert client.run(state.read_value()) == ua.ServerState.Test

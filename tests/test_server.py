"""Through a real OPC UA client: address space, values, writes, methods."""
import pytest
from asyncua import ua
from asyncua.ua import uaerrors

from conftest import Events
from opcua_sim import Bench


def test_device_set_and_identification(benches, connect):
    bench = Bench.from_dict({"devices": [
        {"name": "oven", "profile": "temperature-controller", "serial_number": "TC-7"}]})
    client = connect(benches(bench))
    assert client.value("oven.Manufacturer") == "steitec"
    assert client.value("oven.SerialNumber") == "TC-7"
    assert client.value("oven.DeviceHealth") == 0
    names = [client.run(c.read_browse_name()).Name
             for c in client.run(client.node("oven").get_children())]
    assert {"ParameterSet", "MethodSet", "Alarms", "DeviceHealth", "Model"} <= set(names)


def test_analog_item_properties(benches, connect):
    client = connect(benches("temperature-controller", name="oven"))
    node = client.node("oven.ProcessValue")
    assert client.run(node.read_type_definition()) == ua.NodeId(ua.ObjectIds.AnalogItemType)
    assert client.run(node.read_data_type()) == ua.NodeId(ua.ObjectIds.Double)
    eu_range = client.run(client.run(node.get_child("EURange")).read_value())
    assert (eu_range.Low, eu_range.High) == (-50, 300)
    units = client.run(client.run(node.get_child("0:EngineeringUnits")).read_value())
    assert units.DisplayName.Text == "degC"


def test_health_enumeration_is_a_real_enum(benches, connect):
    client = connect(benches("drive", name="pump"))
    datatype = client.run(client.node("pump.DeviceHealth").read_data_type())
    names = client.run(client.client.get_node(datatype).get_child("0:EnumStrings"))
    texts = [t.Text for t in client.run(names.read_value())]
    assert texts == ["NORMAL", "FAILURE", "CHECK_FUNCTION", "OFF_SPEC", "MAINTENANCE_REQUIRED"]


def test_write_and_behaviour(benches, connect):
    bench = Bench.single("drive", name="pump", params={"ramp": 100.0})
    client = connect(benches(bench))
    client.write("pump.SpeedSetpoint", 20.0)
    client.call("pump", "Start")
    client.eventually(lambda: client.value("pump.Speed") == pytest.approx(20.0))
    assert client.value("pump.Running") is True
    assert client.value("pump.MotorCurrent") > 0
    client.call("pump", "Stop")
    client.eventually(lambda: client.value("pump.State") == 0)


def test_write_is_refused_outside_limits_and_when_read_only(benches, connect):
    client = connect(benches("drive", name="pump"))
    with pytest.raises(uaerrors.BadOutOfRange):
        client.write("pump.SpeedSetpoint", 99.0)
    with pytest.raises(uaerrors.BadNotWritable):
        client.write("pump.Speed", 1.0)
    with pytest.raises(uaerrors.BadTypeMismatch):
        client.write("pump.SpeedSetpoint", 1, ua.VariantType.Int32)


def test_written_value_can_be_read_back(benches, connect):
    client = connect(benches("drive", name="pump"))
    client.write("pump.SpeedSetpoint", 12.5)
    client.eventually(lambda: client.value("pump.SpeedSetpoint") == 12.5)


def test_method_returns_output_and_checks_arguments(benches, connect):
    bench = Bench.single("energy-meter", name="meter", params={"energy": 5.0})
    client = connect(benches(bench))
    assert client.call("meter", "ResetEnergy") == pytest.approx(5.0, abs=0.01)
    with pytest.raises(uaerrors.BadTooManyArguments):
        client.call("meter", "ResetEnergy", 1.0)


def test_method_refused_in_wrong_state(benches, connect):
    bench = benches("drive", name="pump")
    client = connect(bench)
    with pytest.raises(uaerrors.BadInvalidState):
        client.call("pump", "Reset")
    bench.inject("pump", "trip", code=7)
    client.eventually(lambda: client.value("pump.FaultCode") == 7)
    assert client.value("pump.DeviceHealth") == 1
    with pytest.raises(uaerrors.BadInvalidState):
        client.call("pump", "Start")
    with pytest.raises(uaerrors.BadInvalidState):
        client.call("pump", "Reset")           # the cause is still there
    bench.clear("pump", "trip")
    client.pump(0.3)
    assert client.value("pump.State") == 3     # still latched
    client.call("pump", "Reset")
    client.eventually(lambda: client.value("pump.DeviceHealth") == 0)


def test_values_change_through_subscription(benches, connect):
    bench = Bench.single("energy-meter", name="meter", params={"current": 100.0})
    client = connect(benches(bench))
    handler = Events()
    subscription = client.run(client.client.create_subscription(50, handler))
    client.run(subscription.subscribe_data_change(client.node("meter.Energy")))
    client.eventually(lambda: len(handler.events) >= 3)
    values = [item["value"] for item in handler.events]
    assert values == sorted(values) and values[-1] > values[0]


def test_generic_profile_sources_and_writable_without_model(benches, connect):
    client = connect(benches("generic-sensor", name="s"))
    assert client.value("s.Tag") == "line 1"
    client.write("s.Enabled", False, ua.VariantType.Boolean)
    client.eventually(lambda: client.value("s.Enabled") is False)
    first = client.value("s.Humidity")
    client.eventually(lambda: client.value("s.Humidity") != first)

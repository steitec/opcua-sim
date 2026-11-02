"""Alarms as OPC UA events: fields, Acknowledge, ConditionRefresh."""
import pytest
from asyncua import ua
from asyncua.ua import uaerrors

from conftest import Events

ALARM = ua.ObjectIds.ExclusiveLevelAlarmType


def subscribe(client, source=ua.ObjectIds.Server, etype=ALARM):
    handler = Events()
    subscription = client.run(client.client.create_subscription(50, handler))
    client.run(subscription.subscribe_events(source, etype))
    return handler, subscription


def value(event, name):
    return event[name].Value


def named(handler, condition):
    return [e for e in handler.events
            if "ConditionName" in e and value(e, "ConditionName") == condition]


def test_discrete_alarm_raises_with_fields(benches, connect):
    bench = benches("pressure-transmitter", name="pt")
    client = connect(bench)
    handler, _ = subscribe(client, etype=ua.ObjectIds.OffNormalAlarmType)
    bench.inject("pt", "sensor_break")
    client.eventually(lambda: named(handler, "SensorFailure"))
    event = named(handler, "SensorFailure")[0]
    assert value(event, "Severity") == 900
    assert value(event, "SourceName") == "pt"
    assert value(event, "ActiveState/Id") is True
    assert value(event, "AckedState/Id") is False
    assert value(event, "Retain") is True
    assert value(event, "Message").Text == "Pressure sensor failed"
    assert value(event, "NodeId") == ua.NodeId("pt.Alarms.SensorFailure", client.idx)


def test_event_also_reaches_a_subscription_on_the_device(benches, connect):
    bench = benches("drive", name="pump")
    client = connect(bench)
    handler, _ = subscribe(client, source=client.node("pump"), etype=ua.ObjectIds.OffNormalAlarmType)
    bench.inject("pump", "overload")
    client.eventually(lambda: named(handler, "Overload"))


def test_level_alarm_limit_states_and_deadband(benches, connect):
    bench = benches("pressure-transmitter", name="pt")
    client = connect(bench)
    handler, _ = subscribe(client)
    bench.set("pt", "Pressure", 12.5)
    client.eventually(lambda: named(handler, "PressureLevel"))
    event = named(handler, "PressureLevel")[-1]
    assert value(event, "ActiveState").Text == "High"
    assert value(event, "Severity") == 700
    assert value(event, "HighLimit") == 12
    assert value(event, "LimitState/CurrentState").Text == "High"
    bench.set("pt", "Pressure", 15.5)
    client.eventually(lambda: value(named(handler, "PressureLevel")[-1], "ActiveState").Text
                      == "HighHigh")
    assert value(named(handler, "PressureLevel")[-1], "Severity") == 900
    bench.set("pt", "Pressure", 11.95)   # inside the deadband of "high": still high
    client.eventually(lambda: value(named(handler, "PressureLevel")[-1], "ActiveState").Text
                      == "High")
    client.pump(0.3)
    assert value(named(handler, "PressureLevel")[-1], "ActiveState/Id") is True
    bench.set("pt", "Pressure", 8.0)
    client.eventually(lambda: value(named(handler, "PressureLevel")[-1], "ActiveState/Id") is False)
    assert value(named(handler, "PressureLevel")[-1], "Retain") is True  # not acknowledged yet


def _acknowledge(client, condition, event_id, comment="seen"):
    node = client.client.get_node(condition)
    method = client.client.get_node(ua.ObjectIds.AcknowledgeableConditionType_Acknowledge)
    return client.run(node.call_method(method, ua.Variant(event_id, ua.VariantType.ByteString),
                                       ua.LocalizedText(comment)))


def test_acknowledge_flow(benches, connect):
    bench = benches("drive", name="pump")
    client = connect(bench)
    handler, _ = subscribe(client, etype=ua.ObjectIds.OffNormalAlarmType)
    bench.inject("pump", "overload")
    client.eventually(lambda: named(handler, "Overload"))
    event = named(handler, "Overload")[-1]
    condition, event_id = value(event, "NodeId"), value(event, "EventId")
    with pytest.raises(uaerrors.BadEventIdUnknown):
        _acknowledge(client, condition, b"not the event")
    _acknowledge(client, condition, event_id, "on it")
    client.eventually(lambda: value(named(handler, "Overload")[-1], "AckedState/Id") is True)
    event = named(handler, "Overload")[-1]
    assert value(event, "Comment").Text == "on it"
    assert value(event, "Retain") is True            # still active
    with pytest.raises(uaerrors.BadConditionBranchAlreadyAcked):
        _acknowledge(client, condition, value(event, "EventId"))
    bench.clear("pump", "overload")
    client.eventually(lambda: value(named(handler, "Overload")[-1], "Retain") is False)


def test_condition_refresh_replays_retained_conditions(benches, connect):
    bench = benches("drive", name="pump")
    client = connect(bench)
    bench.inject("pump", "overload")
    client.eventually(lambda: bench.status()["devices"]["pump"]["alarms"]["Overload"]["active"])
    # a client that subscribes later learns of the alarm only through a refresh
    handler, subscription = subscribe(client, etype=ua.ObjectIds.OffNormalAlarmType)
    client.pump(0.3)
    assert not named(handler, "Overload")
    conditions = client.client.get_node(ua.ObjectIds.ConditionType)
    method = client.client.get_node(ua.ObjectIds.ConditionType_ConditionRefresh)
    client.run(conditions.call_method(method, ua.Variant(subscription.subscription_id,
                                                         ua.VariantType.UInt32)))
    client.eventually(lambda: named(handler, "Overload"))


def test_condition_refresh_bad_subscription(benches, connect):
    client = connect(benches("drive", name="pump"))
    conditions = client.client.get_node(ua.ObjectIds.ConditionType)
    method = client.client.get_node(ua.ObjectIds.ConditionType_ConditionRefresh)
    with pytest.raises(uaerrors.BadSubscriptionIdInvalid):
        client.run(conditions.call_method(method, ua.Variant(12345, ua.VariantType.UInt32)))

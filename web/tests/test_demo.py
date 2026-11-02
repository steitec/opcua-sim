"""The Python layer of the browser demo, without a browser."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "web" / "py"))

from demo import Demo  # noqa: E402


@pytest.fixture
def demo():
    return Demo(str(ROOT / "examples" / "plant.yaml"))


def run(demo, line, target="plant"):
    return json.loads(demo.run(target, line))


def texts(result, kind=None):
    return [line["text"] for line in result["lines"] if kind is None or line["kind"] == kind]


def test_read_short_and_full_node_ids(demo):
    short = texts(run(demo, "read pump.State"), "value")[0]
    full = texts(run(demo, "read ns=2;s=pump.State"), "value")[0]
    assert short.startswith("pump.State: 0  [Good,")
    assert full.startswith("ns=2;s=pump.State: 0  [Good,")


def test_first_command_opens_a_session(demo):
    assert "# new session" in texts(run(demo, "read pump.State"), "note")
    assert "# new session" not in texts(run(demo, "read pump.State"), "note")


def test_write_call_and_ramp(demo):
    assert "written, Good" in texts(run(demo, "write pump.SpeedSetpoint 30"), "value")[0]
    assert "Good" in texts(run(demo, "call pump.Start"), "value")[0]
    demo.advance(1.0)
    assert json.loads(demo.state())["devices"]["pump"]["values"]["Speed"] == pytest.approx(10.0, abs=1)


@pytest.mark.parametrize("line,status", [
    ("write pump.SpeedSetpoint 99", "BadOutOfRange"),
    ("write pump.Speed 1", "BadNotWritable"),
    ("write pump.SpeedSetpoint fast", "BadTypeMismatch"),
    ("write pump.Running maybe", "BadTypeMismatch"),
    ("call pump.Reset", "BadInvalidState"),
    ("call pump.Speed", "BadMethodInvalid"),
    ("write pump.Start 1", "BadNotWritable"),
    ("ack pump.Nothing", "BadNodeIdInvalid"),
])
def test_refused_requests_answer_status_codes(demo, line, status):
    assert status in texts(run(demo, line), "error")


def test_unknown_node_on_read_is_per_node(demo):
    result = run(demo, "read pump.Nope pump.State")
    assert "pump.Nope: BadNodeIdUnknown" in texts(result, "error")
    assert texts(result, "value")[0].startswith("pump.State: 0")


def test_trip_latches(demo):
    run(demo, "inject pump trip code=4")
    assert "BadInvalidState" in texts(run(demo, "call pump.Start"), "error")
    state = json.loads(demo.state())["devices"]["pump"]
    assert state["health"] == "FAILURE"
    assert state["values"]["FaultCode"] == 4
    run(demo, "clear pump trip")
    assert "Good" in texts(run(demo, "call pump.Reset"), "value")[0]
    assert json.loads(demo.state())["devices"]["pump"]["health"] == "NORMAL"


def test_bad_value_reads_null_and_is_null_in_the_state(demo):
    run(demo, "inject tank_pressure sensor_break")
    assert "null  [BadSensorFailure," in texts(run(demo, "read tank_pressure.Pressure"), "value")[0]
    state = json.loads(demo.state())["devices"]["tank_pressure"]
    assert state["values"]["Pressure"] is None
    assert state["status"]["Pressure"] == "BadSensorFailure"
    assert state["values"]["OutputCurrent"] == 3.6


def test_health_node(demo):
    run(demo, "inject pump health state=OFF_SPEC")
    assert "3 (OFF_SPEC)" in texts(run(demo, "read pump.DeviceHealth"), "value")[0]


def test_alarm_event_ack_and_retain(demo):
    run(demo, "read pump.State")                     # a session, so events arrive
    result = run(demo, "inject pump overload")
    assert "Overload severity=700 active=yes acked=no" in result["state"]["events"][0]["text"]
    alarms = result["state"]["endpoints"]["plant"]["alarms"]
    assert alarms == [{"id": "pump.Overload", "severity": 700, "active": True, "acked": False,
                       "message": "Motor overload"}]
    assert "acked=yes" in texts(run(demo, "ack pump.Overload seen it"), "event")[0]
    assert demo.endpoints["plant"].devices["pump"].alarms["Overload"].comment == "seen it"
    assert "BadConditionBranchAlreadyAcked" in texts(run(demo, "ack pump.Overload"), "error")
    result = run(demo, "clear pump overload")
    assert result["state"]["endpoints"]["plant"]["alarms"] == []


def test_events_without_a_session_are_missed_and_refresh_brings_them_back(demo):
    run(demo, "read pump.State")
    run(demo, "inject server drop_connections")
    result = run(demo, "inject pump overload")
    assert result["state"]["events"] == []
    result = run(demo, "refresh")
    assert any("missed" in text for text in texts(result, "note"))
    assert any("pump.Overload" in text for text in texts(result, "event"))
    events = texts(result, "event")
    assert events[0].endswith("RefreshStartEvent") and events[-1].endswith("RefreshEndEvent")


def test_blackhole_refuse_delay_and_restart(demo):
    run(demo, "read pump.State")
    run(demo, "inject server delay seconds=1.5")
    assert run(demo, "read pump.State")["wait"] == 1.5
    run(demo, "clear server delay")
    run(demo, "inject server blackhole")
    assert texts(run(demo, "read pump.State"), "timeout")
    run(demo, "clear server")
    run(demo, "inject server restart downtime=2")
    assert "restarting" in texts(run(demo, "read pump.State"), "error")[0]
    demo.advance(2.5)
    assert "# new session" in texts(run(demo, "read pump.State"), "note")
    run(demo, "inject server drop_connections")
    run(demo, "inject server refuse")
    assert "closed by the server" in texts(run(demo, "read pump.State"), "error")[0]


def test_siemens_quotes_are_part_of_the_id(demo):
    assert "ns=3;s=DB_Line.Speed: BadNodeIdUnknown" in \
        texts(run(demo, "read ns=3;s=DB_Line.Speed", "s7"), "error")
    assert '"Speed": 0' in texts(run(demo, 'read ns=3;s="DB_Line"."Speed"', "s7"), "value")[0]
    assert "BadNodeIdUnknown" in texts(run(demo, "read plc.Speed", "s7"), "error")[0]
    assert any("simatic-s7-opcua" in text for text in
               texts(run(demo, "namespaces", "s7"), "value"))
    run(demo, 'write ns=3;s="DB_Line"."Run" true', "s7")
    demo.advance(1.0)
    assert json.loads(demo.state())["devices"]["plc"]["values"]["Speed"] > 0


def test_leuze_numeric_ids(demo):
    assert "2  [Good" in texts(run(demo, "read ns=4;i=6006", "reader"), "value")[0]
    demo.advance(1.5)
    assert json.loads(demo.state())["devices"]["reader"]["values"]["LastScanData"]
    assert "BCL 300" in texts(run(demo, "read ns=4;i=6071", "reader"), "value")[0]
    run(demo, "call ns=4;i=7002", "reader")
    assert "0  [Good" in texts(run(demo, "read ns=4;i=6006", "reader"), "value")[0]


@pytest.mark.parametrize("line", [
    "", "nonsense", "read", "write pump.State", "call", "ack", "ack pump", "inject pump",
    "inject pump nonsense", "inject nobody trip", "inject server nonsense", "inject pump trip code",
    "inject server delay seconds=-1", "clear", "clear nobody", "clear server nonsense",
    "clear pump nonsense",
])
def test_invalid_commands_are_an_error_line(demo, line):
    assert texts(run(demo, line), "error")


def test_help_and_browse(demo):
    assert any("inject TARGET" in text for text in texts(run(demo, "help"), "note"))
    assert any("ns=2;s=pump.Speed" in text for text in texts(run(demo, "browse"), "value"))


def test_big_time_steps_are_limited(demo):
    demo.last -= 3600
    json.loads(demo.state())
    assert demo.endpoints["plant"].devices["pump"].elapsed <= 6


def test_reset_starts_over(demo):
    run(demo, "inject pump trip")
    run(demo, "inject server blackhole")
    state = json.loads(demo.reset())
    assert state["devices"]["pump"]["faults"] == []
    assert state["endpoints"]["plant"]["faults"] == []
    assert state["endpoints"]["plant"]["connected"] is False

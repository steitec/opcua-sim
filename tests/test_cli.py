"""The command line, against a bench in this process."""
import json
import socket

import pytest

from opcua_sim import Bench, __version__
from opcua_sim.cli import main
from opcua_sim.control import ControlError, request


@pytest.fixture
def plant(benches):
    return benches(Bench.from_dict({"devices": [
        {"name": "pump", "profile": "drive", "params": {"ramp": 100.0}},
        {"name": "pt", "profile": "pressure-transmitter"}]}))


def client_args(bench):
    return ["--url", bench.url]


def control_args(bench):
    return ["--control-port", str(bench.control_port)]


def test_version(capsys):
    with pytest.raises(SystemExit) as info:
        main(["--version"])
    assert info.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_profiles_and_map(capsys):
    assert main(["profiles"]) == 0
    out = capsys.readouterr().out
    assert "drive" in out and "trip" in out and "drop_connections" in out
    assert main(["map", "pressure-transmitter"]) == 0
    out = capsys.readouterr().out
    assert "ZeroTrim" in out and "PressureLevel" in out and "rw" in out


def test_browse_read_write_call(plant, capsys):
    assert main(["browse", *client_args(plant)]) == 0
    assert "SpeedSetpoint" in capsys.readouterr().out
    assert main(["write", "pump.SpeedSetpoint", "12", *client_args(plant)]) == 0
    assert main(["call", "pump.Start", *client_args(plant)]) == 0
    capsys.readouterr()
    assert main(["read", "pump.SpeedSetpoint", "pump.Running", *client_args(plant)]) == 0
    out = capsys.readouterr().out
    assert "12.0" in out and "Good" in out


def test_refused_call_is_exit_1_with_the_status_code(plant, capsys):
    assert main(["call", "pump.Reset", *client_args(plant)]) == 1
    assert "BadInvalidState" in capsys.readouterr().err


def test_inject_read_clear(plant, capsys):
    assert main(["inject", "pt", "sensor_break", *control_args(plant)]) == 0
    assert main(["inject", "pump", "trip", "code=4", *control_args(plant)]) == 0
    assert main(["list", *control_args(plant)]) == 0
    out = capsys.readouterr().out
    assert "health=FAILURE" in out and "sensor_break" in out
    assert plant.status()["devices"]["pump"]["faults"]["trip"]["code"] == 4
    assert main(["clear", "pt", *control_args(plant)]) == 0
    assert plant.status()["devices"]["pt"]["faults"] == {}


def test_set_and_get(plant, capsys):
    assert main(["set", "pt", "Pressure=3.5", *control_args(plant)]) == 0
    assert main(["get", "pt", "Pressure", *control_args(plant)]) == 0
    values = json.loads(capsys.readouterr().out)
    assert values["Pressure"] == 3.5


def test_watch_prints_values_and_alarms(plant, capsys):
    plant.inject("pump", "overload")
    assert main(["watch", "pump.State", "--alarms", "--seconds", "0.5", "--interval", "50",
                 *client_args(plant)]) == 0
    assert "pump.State" in capsys.readouterr().out


def test_invalid_fault_is_exit_1(plant, capsys):
    assert main(["inject", "pump", "nonsense", *control_args(plant)]) == 1
    assert "no fault" in capsys.readouterr().err
    assert main(["inject", "pump", "trip", "code", *control_args(plant)]) == 1
    assert "name=value" in capsys.readouterr().err


def _free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_no_bench_is_a_clean_error(capsys):
    assert main(["list", "--control-port", str(_free_port())]) == 1
    assert "no bench" in capsys.readouterr().err
    # A long timeout: Windows retries a refused connection for about two seconds.
    assert main(["read", "pump.Speed", "--timeout", "8", "--url",
                 f"opc.tcp://127.0.0.1:{_free_port()}"]) == 1
    assert "no connection" in capsys.readouterr().err


def test_late_answer_is_a_clean_error(plant, capsys):
    plant.inject("server", "blackhole")
    assert main(["read", "pump.Speed", "--timeout", "0.5", *client_args(plant)]) == 1
    assert "no answer in time" in capsys.readouterr().err


def test_bad_bench_file_is_a_clean_error(capsys):
    assert main(["serve", "--bench", "does-not-exist.yaml"]) == 1
    assert "cannot read" in capsys.readouterr().err


def test_control_channel_stays_open_after_errors(plant):
    with socket.create_connection(("127.0.0.1", plant.control_port), 5) as connection:
        stream = connection.makefile("rwb")
        for line, ok in ((b"not json\n", False), (b"[1]\n", False), (b'{"target": 1}\n', False),
                         (b'{"target": "pump"}\n', False),
                         (b'{"target": "pump", "get": 3}\n', False),
                         (b'{"target": "pump", "set": 3}\n', False),
                         (b'{"target": "pump", "inject": "trip", "params": 3}\n', False),
                         (b'{"list": true}\n', True)):
            stream.write(line)
            stream.flush()
            assert json.loads(stream.readline())["ok"] is ok


def test_request_reports_an_error():
    with pytest.raises(ControlError):
        request(_free_port(), {"list": True}, timeout=1)


def test_serve_options_from_the_environment(monkeypatch):
    from opcua_sim.cli import parser
    monkeypatch.setenv("OPCUA_SIM_ENDPOINT_HOST", "opcua")
    monkeypatch.setenv("OPCUA_SIM_CERTIFICATE_DIR", "/certs")
    args = parser().parse_args(["serve", "--profile", "drive"])
    assert (args.endpoint_host, args.certificate_dir) == ("opcua", "/certs")
    args = parser().parse_args(["serve", "--profile", "drive", "--endpoint-host", "h",
                                "--certificate-dir", "d"])
    assert (args.endpoint_host, args.certificate_dir) == ("h", "d")


def test_serve_without_the_environment(monkeypatch):
    from opcua_sim.cli import parser
    monkeypatch.delenv("OPCUA_SIM_ENDPOINT_HOST", raising=False)
    monkeypatch.delenv("OPCUA_SIM_CERTIFICATE_DIR", raising=False)
    args = parser().parse_args(["serve", "--profile", "drive"])
    assert (args.endpoint_host, args.certificate_dir) == (None, None)

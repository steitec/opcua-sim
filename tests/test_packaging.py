"""Things a release can get wrong."""
import pytest
import subprocess
import sys
from pathlib import Path

tomllib = pytest.importorskip("tomllib")

import opcua_sim

ROOT = Path(__file__).parent.parent


def test_versions_agree():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["project"]
    assert project["version"] == opcua_sim.__version__
    assert f"## {opcua_sim.__version__} " in (ROOT / "CHANGELOG.md").read_text("utf-8")


def test_example_bench_loads():
    assert set(opcua_sim.Bench.from_yaml(ROOT / "examples" / "plant.yaml").devices) == \
        {"oven", "pump", "tank_pressure", "meter"}


def test_server_states_match_the_stack():
    from asyncua import ua

    from opcua_sim.faults import SERVER_STATES
    assert SERVER_STATES == {state.name: state.value for state in ua.ServerState}


def test_devices_run_without_asyncua():
    """The browser demo has no asyncua: only the server and the CLI may need it."""
    code = """
import sys
class Block:
    def find_spec(self, name, path=None, target=None):
        if name == "asyncua" or name.startswith("asyncua."):
            raise ImportError("blocked: " + name)
sys.meta_path.insert(0, Block())
from opcua_sim import load_profile
from opcua_sim.device import Device
from opcua_sim.faults import FaultError, check_server
device = Device("d", load_profile("drive"))
device.inject("bad_status", {"status": "BadSensorFailure"})
device.step(0.1)
assert device.samples["Speed"].status == "BadSensorFailure"
try:
    device.inject("bad_status", {"status": "Nonsense"})
except FaultError:
    pass
else:
    raise SystemExit("bad status code accepted")
check_server("server_state", {"state": "Suspended"})
import opcua_sim
try:
    opcua_sim.Bench
except ImportError:
    pass
else:
    raise SystemExit("Bench imported without asyncua")
print("ok")
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.stdout.strip() == "ok", result.stderr

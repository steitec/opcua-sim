"""Things a release can get wrong."""
import pytest
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

"""Profiles and benches: what is accepted and what is refused, without a server."""
import pytest

from opcua_sim import Bench, BenchError, ProfileError, bundled_profiles, load_profile
from opcua_sim.device import Device, coerce
from opcua_sim.profile import parse_profile


def base(**extra):
    return {"name": "p", "variables": [{"name": "V"}], **extra}


def test_bundled_profiles_all_load():  # includes real-product layouts
    names = bundled_profiles()
    assert {"drive", "energy-meter", "generic-sensor", "pressure-transmitter",
            "temperature-controller"} <= set(names)
    for name in names:
        Device("d", load_profile(name))


@pytest.mark.parametrize("data", [
    [],
    {"variables": [{"name": "V"}]},
    {"name": "p"},
    {"name": "p", "variables": []},
    base(extra=1),
    base(model="nonsense"),
    {"name": "p", "variables": [{"name": "V", "type": "Nonsense"}]},
    {"name": "p", "variables": [{"name": "a.b"}]},
    {"name": "p", "variables": [{"name": "V"}, {"name": "V"}]},
    {"name": "p", "variables": [{"name": "V", "model": "x"}]},
    {"name": "p", "variables": [{"name": "V", "value": 1, "source": {"kind": "sine"}}]},
    {"name": "p", "variables": [{"name": "V", "range": [5, 1]}]},
    {"name": "p", "variables": [{"name": "V", "type": "String", "range": [0, 1]}]},
    {"name": "p", "variables": [{"name": "V", "source": {"kind": "nonsense"}}]},
    base(methods=[{"name": "M", "model": "x"}]),
    base(alarms=[{"name": "A", "kind": "level", "variable": "Nope", "high": 1}]),
    base(alarms=[{"name": "A", "kind": "level", "variable": "V"}]),
    base(alarms=[{"name": "A", "kind": "level", "variable": "V", "high": 1, "high_high": 0}]),
    base(alarms=[{"name": "A", "kind": "discrete"}]),
    base(alarms=[{"name": "A", "kind": "discrete", "when": "nope"}]),
    base(alarms=[{"name": "A", "kind": "nonsense"}]),
    base(identification={"colour": "red"}),
])
def test_invalid_profiles(data):
    with pytest.raises(ProfileError):
        parse_profile(data)


def test_missing_profile_names_the_bundled_ones():
    with pytest.raises(ProfileError, match="drive"):
        load_profile("does-not-exist")


def test_invalid_yaml(tmp_path):
    path = tmp_path / "broken.yaml"
    path.write_text("name: [", "utf-8")
    with pytest.raises(ProfileError, match="invalid YAML"):
        load_profile(path)


def test_profile_file_relative_to_the_bench_file(tmp_path):
    (tmp_path / "mine.yaml").write_text("name: mine\nvariables:\n  - {name: V, value: 3}\n", "utf-8")
    bench_file = tmp_path / "bench.yaml"
    bench_file.write_text("devices:\n  - {name: d, profile: mine.yaml}\n", "utf-8")
    assert Bench.from_yaml(bench_file).devices["d"].get("V") == 3


def test_inline_profile():
    bench = Bench.from_dict({"devices": [{"name": "d", "inline": base()}]})
    assert bench.devices["d"].get("V") == 0


@pytest.mark.parametrize("data", [
    [],
    {},
    {"devices": []},
    {"devices": [1]},
    {"devices": [{"name": "d"}]},
    {"devices": [{"profile": "drive"}]},
    {"devices": [{"name": "a b", "profile": "drive"}]},
    {"devices": [{"name": "server", "profile": "drive"}]},
    {"devices": [{"name": "d", "profile": "drive"}, {"name": "d", "profile": "drive"}]},
    {"devices": [{"name": "d", "profile": "drive", "params": {"nonsense": 1}}]},
    {"devices": [{"name": "d", "profile": "drive", "params": {"max_speed": 0}}]},
    {"devices": [{"name": "d", "profile": "generic-sensor", "params": {"a": 1}}]},
    {"devices": [{"name": "d", "profile": "drive", "oops": 1}]},
    {"devices": [{"name": "d", "profile": "drive"}], "oops": 1},
])
def test_invalid_benches(data):
    with pytest.raises(BenchError):
        Bench.from_dict(data)


def test_missing_bench_file():
    with pytest.raises(BenchError, match="cannot read"):
        Bench.from_yaml("does-not-exist.yaml")


@pytest.mark.parametrize("value,vtype,expected", [
    (1.6, "Int32", 2), (-5, "UInt16", 0), (70000, "Int16", 32767), (1, "Boolean", True),
    (2, "Double", 2.0), (None, "String", ""), (float("nan"), "Int32", 0),
])
def test_coerce(value, vtype, expected):
    assert coerce(value, vtype) == expected


def test_serial_number_is_stable_and_can_be_set():
    profile = load_profile("drive")
    assert Device("a", profile).identification["serial_number"] == \
        Device("a", profile).identification["serial_number"]
    assert Device("a", profile, serial_number="X1").identification["serial_number"] == "X1"

"""The pytest plugin, in a pytest of its own."""
pytest_plugins = ["pytester"]

BODY = """
import asyncio
from asyncua import Client, ua

def read(url, name):
    async def go():
        async with Client(url, timeout=3) as client:
            idx = await client.get_namespace_index("urn:steitec:opcua-sim")
            return await client.get_node(ua.NodeId(name, idx)).read_value()
    return asyncio.run(go())
"""


def test_default_fixture(pytester):
    pytester.makepyfile(BODY + """
def test_default(opcua_bench):
    assert read(opcua_bench.url, "drive.State") == 0
    opcua_bench.inject("drive", "health", state="OFF_SPEC")
""")
    pytester.runpytest("-p", "no:cacheprovider").assert_outcomes(passed=1)


def test_every_test_gets_a_fresh_bench(pytester):
    pytester.makepyfile("""
def test_one(opcua_bench):
    opcua_bench.inject("drive", "health", state="FAILURE")
def test_two(opcua_bench):
    assert opcua_bench.status()["devices"]["drive"]["health"] == "NORMAL"
""")
    pytester.runpytest("-p", "no:cacheprovider").assert_outcomes(passed=2)


def test_bench_file_option_and_factory(pytester):
    pytester.makefile(".yaml", plant="devices:\n  - {name: m, profile: energy-meter}\n")
    pytester.makepyfile("""
def test_from_file(opcua_bench):
    assert list(opcua_bench.devices) == ["m"]
def test_factory(opcua_bench_factory):
    assert list(opcua_bench_factory("drive").devices) == ["drive"]
    assert list(opcua_bench_factory({"devices": [{"name": "x", "profile": "drive"}]}).devices) == ["x"]
    assert list(opcua_bench_factory("plant.yaml").devices) == ["m"]
""")
    pytester.runpytest("-p", "no:cacheprovider", "--opcua-bench", "plant.yaml") \
        .assert_outcomes(passed=2)


def test_ini_option_and_clean_failure(pytester):
    pytester.makeini("[pytest]\nopcua_bench = missing.yaml\n")
    pytester.makepyfile("def test_x(opcua_bench):\n    pass\n")
    result = pytester.runpytest("-p", "no:cacheprovider")
    result.stdout.fnmatch_lines(["*opcua-sim bench: cannot read*"])

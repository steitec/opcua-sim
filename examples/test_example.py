"""What a test of your own client code looks like: run with  pytest examples/test_example.py

``read_speed`` stands for your code. The bench is new for every test and picks a free port.
"""
import asyncio

import pytest
from asyncua import Client, ua


def read_speed(url):
    async def go():
        async with Client(url, timeout=2) as client:
            idx = await client.get_namespace_index("urn:steitec:opcua-sim")
            return await client.get_node(ua.NodeId("drive.Speed", idx)).read_value()
    return asyncio.run(go())


def test_reads_the_speed(opcua_bench):
    assert read_speed(opcua_bench.url) == 0


def test_notices_a_device_failure(opcua_bench):
    opcua_bench.inject("drive", "bad_status", variable="Speed", status="BadDeviceFailure")
    with pytest.raises(ua.uaerrors.BadDeviceFailure):
        read_speed(opcua_bench.url)


def test_gives_up_on_a_dead_link(opcua_bench):
    opcua_bench.inject("server", "blackhole")
    with pytest.raises((asyncio.TimeoutError, ConnectionError)):
        read_speed(opcua_bench.url)

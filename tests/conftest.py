import asyncio

import pytest
from asyncua import Client, ua

from opcua_sim import Bench
from opcua_sim.server import NAMESPACE


class Sync:
    """A blocking view of an asyncua client, so tests read top to bottom."""

    def __init__(self, url, timeout=3):
        self.loop = asyncio.new_event_loop()
        self.client = Client(url, timeout=timeout)
        self.idx = None

    def run(self, coroutine):
        return self.loop.run_until_complete(coroutine)

    def connect(self):
        self.run(self.client.connect())
        self.idx = self.run(self.client.get_namespace_index(NAMESPACE))
        return self

    def close(self):
        try:
            self.run(self.client.disconnect())
        except Exception:
            pass
        pending = asyncio.all_tasks(self.loop)
        for task in pending:
            task.cancel()
        if pending:
            self.run(asyncio.gather(*pending, return_exceptions=True))
        self.loop.close()

    def node(self, name):
        return self.client.get_node(ua.NodeId(name, self.idx))

    def value(self, name):
        return self.run(self.node(name).read_value())

    def data(self, name):
        return self.run(self.node(name).read_data_value(raise_on_bad_status=False))

    def write(self, name, value, vtype=ua.VariantType.Double):
        self.run(self.node(name).write_value(ua.DataValue(ua.Variant(value, vtype))))

    def call(self, device, method, *args):
        parent = self.node(f"{device}.MethodSet")
        return self.run(parent.call_method(self.node(f"{device}.{method}"), *args))

    def pump(self, seconds):
        self.run(asyncio.sleep(seconds))

    def eventually(self, check, timeout=5.0):
        """Poll until ``check()`` is truthy, keeping this client's loop alive."""
        deadline = self.loop.time() + timeout
        while True:
            result = check()
            if result:
                return result
            if self.loop.time() > deadline:
                raise AssertionError("condition not met in time")
            self.pump(0.05)


class Events:
    def __init__(self):
        self.events = []

    def event_notification(self, event):
        self.events.append(event.get_event_props_as_fields_dict())

    def status_change_notification(self, status):
        pass

    def datachange_notification(self, node, value, data):
        self.events.append({"node": node, "value": value, "data": data})


@pytest.fixture
def benches():
    started = []

    def start(source, **options):
        bench = source if isinstance(source, Bench) else Bench.single(source, **options)
        bench.start(port=0, control_port=0)
        started.append(bench)
        return bench

    yield start
    for bench in started:
        bench.stop()


@pytest.fixture
def connect():
    clients = []

    def open_client(bench, **options):
        client = Sync(bench.url, **options).connect()
        clients.append(client)
        return client

    yield open_client
    for client in clients:
        client.close()

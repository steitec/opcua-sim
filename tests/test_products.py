"""Profiles of real products: how a client finds their nodes, through a real client."""
import pytest
from asyncua import ua
from asyncua.ua import uaerrors

from conftest import Sync
from opcua_sim import Bench, BenchError, ProfileError, load_profile
from opcua_sim.cli import main
from opcua_sim.profile import parse_profile

PLCS = {
    "siemens-s7-1500": ("http://www.siemens.com/simatic-s7-opcua", 3,
                        '"DB_Line"."{}"', {}),
    "beckhoff-twincat3": ("urn:BeckhoffAutomation:Ua:PLC1", 4, "MAIN.{}",
                          {"Run": "bRun", "SpeedSetpoint": "fSpeedSetpoint", "Speed": "fSpeed",
                           "Fault": "bFault", "FaultCode": "nFaultCode", "Reset": "bReset"}),
    "phoenix-plcnext": ("http://phoenixcontact.com/OpcUA/PLCnext/GlobalDataSpace/", 2,
                        "Arp.Plc.Eclr/MainInstance.{}",
                        {"Run": "xRun", "SpeedSetpoint": "rSpeedSetpoint", "Speed": "rSpeed",
                         "Fault": "xFault", "FaultCode": "iFaultCode", "Reset": "xReset"}),
    "codesys-control": ("urn:opcua-sim:placeholder:codesys", 4,
                        "|var|CODESYS Control for Linux SL.Application.PLC_PRG.{}",
                        {"Run": "bRun", "SpeedSetpoint": "rSpeedSetpoint", "Speed": "rSpeed",
                         "Fault": "bFault", "FaultCode": "iFaultCode", "Reset": "bReset"}),
    "omron-nx": ("urn:opcua-sim:placeholder:omron", 2, "new_Controller_0.GlobalVars.{}", {}),
}


class Client(Sync):
    def connect(self):
        self.run(self.client.connect())
        return self

    def by_id(self, index, identifier):
        return self.client.get_node(ua.NodeId(identifier, index))

    def get(self, index, identifier):
        return self.run(self.by_id(index, identifier).read_value())

    def put(self, index, identifier, value, vtype):
        self.run(self.by_id(index, identifier).write_value(ua.DataValue(ua.Variant(value, vtype))))


@pytest.fixture
def product(benches):
    clients = []

    def start(profile, **params):
        bench = benches(Bench.single(profile, name="plc", params=params or None))
        client = Client(bench.url).connect()
        clients.append(client)
        return bench, client

    yield start
    for client in clients:
        client.close()


@pytest.mark.parametrize("profile", sorted(PLCS))
def test_plc_namespace_ids_and_program(product, profile):
    uri, index, form, names = PLCS[profile]
    bench, client = product(profile, ramp=5000.0)
    assert client.run(client.client.get_namespace_index(uri)) == index
    node = lambda name: form.format(names.get(name, name))
    assert client.run(client.by_id(index, node("Speed")).read_data_type()) == \
        ua.NodeId(ua.ObjectIds.Float)
    client.put(index, node("SpeedSetpoint"), 600.0, ua.VariantType.Float)
    client.put(index, node("Run"), True, ua.VariantType.Boolean)
    client.eventually(lambda: client.get(index, node("Speed")) == pytest.approx(600.0))
    with pytest.raises(uaerrors.BadNotWritable):
        client.put(index, node("Speed"), 1.0, ua.VariantType.Float)
    bench.inject("plc", "trip", code=12)
    assert client.get(index, node("Fault")) is True
    assert client.get(index, node("FaultCode")) == 12
    assert client.get(index, node("Speed")) == 0
    bench.clear("plc", "trip")
    assert client.get(index, node("Fault")) is True          # latched until the reset bit rises
    client.put(index, node("Reset"), True, ua.VariantType.Boolean)
    assert client.get(index, node("Fault")) is False


def test_siemens_browse_path(product):
    _, client = product("siemens-s7-1500")
    node = client.run(client.client.nodes.objects.get_child(
        ["3:PLC_1", "3:DataBlocksGlobal", "3:DB_Line", "3:Speed"]))
    assert node.nodeid == ua.NodeId('"DB_Line"."Speed"', 3)
    with pytest.raises(uaerrors.BadNodeIdUnknown):
        client.get(3, "DB_Line.Speed")                      # the quotes are part of the ID


def test_omron_status_follows_the_controller(product):
    bench, client = product("omron-nx")
    mode, error = "new_Controller_0.DeviceStatus.Mode", "new_Controller_0.DeviceStatus.ErrorStatus"
    assert client.get(2, mode) == "RUN"
    bench.inject("plc", "trip")
    assert client.get(2, error) == "MajorFault"
    bench.inject("plc", "plc_stop")
    assert client.get(2, mode) == "PROGRAM"


def test_leuze_namespaces_ids_and_browse_names(product):
    _, client = product("leuze-bcl300")
    array = client.run(client.client.get_namespace_array())
    assert array == ["http://opcfoundation.org/UA/", "http://opcfoundation.org/UA/DI/",
                     "http://opcfoundation.org/UA/AutoID/", "http://leuze.com/OpcUa/",
                     "http://leuze.com/OpcUa/BCL300/"]
    assert client.get(4, 6071).Text == "BCL 300"
    name = client.run(client.by_id(4, 6006).read_browse_name())
    assert (name.NamespaceIndex, name.Name) == (2, "DeviceStatus")
    node = client.run(client.by_id(4, 5001).get_child("2:LastScanData"))
    assert node.nodeid == ua.NodeId(6205, 4)
    assert client.run(client.by_id(4, 6027).read_type_definition()) == \
        ua.NodeId(ua.ObjectIds.AnalogItemType)
    units = client.run(client.by_id(4, 6027).get_child("0:EngineeringUnits"))
    assert client.run(units.read_value()).DisplayName.Text == "degC"


def test_leuze_scans_counts_and_fails(product):
    bench, client = product("leuze-bcl300", interval=0.05, codes=["ABC"])
    assert client.get(4, 6006) == 2
    client.eventually(lambda: client.get(4, 6205) == "ABC")
    client.eventually(lambda: client.get(4, 6043) >= 2)
    assert client.get(4, 6044) == 100
    bench.inject("plc", "read_fail")
    client.eventually(lambda: client.get(4, 6042) >= 2)
    assert client.get(4, 6205) == ""
    assert client.get(4, 6044) < 100
    bench.clear("plc")
    client.run(client.by_id(4, 5001).call_method(client.by_id(4, 7002)))
    assert client.get(4, 6006) == 0
    assert client.get(4, 6202) is False
    client.put(4, 6202, True, ua.VariantType.Boolean)
    assert client.get(4, 6006) == 2
    bench.inject("plc", "device_error")
    assert client.get(4, 6006) == 1
    with pytest.raises(uaerrors.BadInvalidState):
        client.run(client.by_id(4, 5035).call_method(client.by_id(4, 7009)))


def test_server_faults_work_on_a_product(product):
    bench, client = product("beckhoff-twincat3")
    bench.inject("server", "restart", downtime=0)
    bench.wait_restarted()
    fresh = Client(bench.url).connect()
    try:
        assert fresh.get(4, "MAIN.bRun") is False
        assert fresh.run(fresh.client.get_namespace_index("urn:BeckhoffAutomation:Ua:PLC1")) == 4
    finally:
        fresh.close()


def test_a_product_is_a_whole_server():
    with pytest.raises(BenchError, match="whole server"):
        Bench.from_dict({"devices": [{"name": "a", "profile": "omron-nx"},
                                     {"name": "b", "profile": "drive"}]})


def test_every_product_names_its_sources_and_limits():
    for name in (*PLCS, "leuze-bcl300"):
        profile = load_profile(name)
        assert profile.sources and profile.limits and profile.address_space


def base(**extra):
    return {"name": "p", "variables": [{"name": "V", "node_id": 1, "path": ["F"]}], **extra}


@pytest.mark.parametrize("data", [
    base(),                                                     # node_id without an address space
    base(address_space={"namespace": "a", "namespaces": ["b"]}),
    base(address_space={"namespace": "a", "namespaces": ["a", "a"]}),
    base(address_space={"namespace": "a"}),
    base(address_space={"namespace": "a", "namespaces": ["a"], "folders": [{}]}),
    base(address_space={"namespace": "a", "namespaces": ["a"], "oops": 1}),
    base(address_space={"namespace": "a", "namespaces": ["a"]},
         alarms=[{"name": "A", "kind": "level", "variable": "V", "high": 1}]),
    {"name": "p", "address_space": {"namespace": "a", "namespaces": ["a"]},
     "variables": [{"name": "V"}]},
    {"name": "p", "address_space": {"namespace": "a", "namespaces": ["a"]},
     "variables": [{"name": "V", "node_id": 1, "path": ["F"]},
                   {"name": "W", "node_id": 1, "path": ["F"]}]},
    {"name": "p", "variables": [{"name": "V", "node_id": True}]},
    {"name": "p", "variables": [{"name": "V", "path": "F"}]},
    {"name": "p", "variables": [{"name": "V", "browse_namespace": "x"}]},
])
def test_invalid_address_spaces(data):
    with pytest.raises(ProfileError):
        parse_profile(data)


def test_cli_with_full_node_ids(product, capsys):
    bench, _ = product("leuze-bcl300")
    args = ["--url", bench.url]
    assert main(["browse", "--depth", "2", *args]) == 0
    out = capsys.readouterr().out
    assert "AutoID" in out and "ns=4;i=5001" in out and "Server " not in out
    assert main(["read", "ns=4;i=6006", *args]) == 0
    assert "2  [Good" in capsys.readouterr().out
    assert main(["write", "ns=4;i=6202", "false", *args]) == 0
    assert main(["call", "ns=4;i=7002", "--object", "ns=4;i=5001", *args]) == 0
    capsys.readouterr()
    assert main(["call", "ns=4;i=7002", *args]) == 1
    assert "--object" in capsys.readouterr().err
    assert main(["read", "plc.Speed", *args]) == 1
    assert "full form" in capsys.readouterr().err


def test_map_shows_ids_sources_and_limits(capsys):
    assert main(["map", "siemens-s7-1500"]) == 0
    out = capsys.readouterr().out
    assert 'ns=3;s="DB_Line"."Run"' in out and "109737901" in out and "Not checked" in out

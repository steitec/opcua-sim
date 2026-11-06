"""Endpoints with signing and encryption, user names, read-only users."""
import asyncio

import pytest
from asyncua import Client, ua
from asyncua.ua import uaerrors

from conftest import Sync
from opcua_sim import Bench, BenchError

USERS = {"anna": {"password": "s3cret"}, "vic": {"password": "look", "role": "viewer"}}


def drive(**options):
    return Bench.from_dict({"devices": [{"name": "pump", "profile": "drive"}], **options})


def test_endpoints_are_offered(benches, connect):
    bench = benches(drive(security=["none", "sign", "encrypt"]))
    client = connect(bench)
    endpoints = client.run(client.client.get_endpoints())
    modes = {e.SecurityMode for e in endpoints}
    assert modes == {ua.MessageSecurityMode.None_, ua.MessageSecurityMode.Sign,
                     ua.MessageSecurityMode.SignAndEncrypt}
    assert all(e.EndpointUrl == bench.url for e in endpoints)


def test_encrypted_connection(benches, tmp_path):
    bench = benches(drive(security=["encrypt"]))

    async def run():
        from asyncua.crypto.cert_gen import setup_self_signed_certificate
        from cryptography.x509.oid import ExtendedKeyUsageOID
        cert, key = tmp_path / "c.der", tmp_path / "k.pem"
        uri = "urn:test:client"
        await setup_self_signed_certificate(key, cert, uri, "localhost",
                                            [ExtendedKeyUsageOID.CLIENT_AUTH], {"commonName": "t"})
        client = Client(bench.url, timeout=5)
        client.application_uri = uri
        await client.set_security_string(f"Basic256Sha256,SignAndEncrypt,{cert},{key}")
        async with client:
            idx = await client.get_namespace_index("urn:steitec:opcua-sim")
            return await client.get_node(ua.NodeId("pump.Speed", idx)).read_value()

    assert asyncio.run(run()) == 0


def test_none_is_refused_on_an_encrypted_only_server(benches):
    bench = benches(drive(security=["encrypt"]))

    async def run():
        async with Client(bench.url, timeout=3):
            pass

    with pytest.raises((uaerrors.UaError, ConnectionError, asyncio.TimeoutError, OSError)):
        asyncio.run(run())


def _with_user(bench, user, password):
    client = Sync(bench.url)
    if user:
        client.client.set_user(user)
        client.client.set_password(password)
    return client


def test_user_write_rights(benches):
    bench = benches(drive(users=USERS))
    operator = _with_user(bench, "anna", "s3cret").connect()
    viewer = _with_user(bench, "vic", "look").connect()
    anonymous = _with_user(bench, None, None).connect()
    try:
        operator.write("pump.SpeedSetpoint", 5.0)
        with pytest.raises(uaerrors.BadUserAccessDenied):
            viewer.write("pump.SpeedSetpoint", 6.0)
        with pytest.raises(uaerrors.BadUserAccessDenied):
            anonymous.write("pump.SpeedSetpoint", 6.0)
        viewer.eventually(lambda: viewer.value("pump.SpeedSetpoint") == 5.0)
    finally:
        for client in (operator, viewer, anonymous):
            client.close()


def test_wrong_password_is_refused(benches):
    bench = benches(drive(users=USERS))
    client = _with_user(bench, "anna", "wrong")
    try:
        with pytest.raises(uaerrors.UaError):
            client.connect()
    finally:
        client.close()


def test_anonymous_can_be_switched_off(benches):
    bench = benches(drive(users=USERS, anonymous=False))
    client = _with_user(bench, None, None)
    try:
        with pytest.raises((uaerrors.UaError, ConnectionError)):
            client.connect()
    finally:
        client.close()


@pytest.mark.parametrize("options", [
    {"security": ["nonsense"]},
    {"users": {"a": {}}},
    {"users": {"a": {"password": "x", "role": "king"}}},
    {"anonymous": False},
    {"tick": 0},
])
def test_invalid_options(options):
    with pytest.raises(BenchError):
        drive(**options)


def _certificate(path):
    from cryptography import x509
    return x509.load_der_x509_certificate(path.read_bytes())


def _dns_names(certificate):
    from cryptography import x509
    extension = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    return extension.value.get_values_for_type(x509.DNSName)


def test_certificate_dir_keeps_the_certificate_across_starts(tmp_path):
    folder = tmp_path / "certs"
    first = drive(security=["encrypt"]).start(port=0, control_port=None,
                                              certificate_dir=folder)
    first.stop()
    certificate = (folder / "server-cert.der").read_bytes()
    assert (folder / "server-key.pem").is_file()
    second = drive(security=["encrypt"]).start(port=0, control_port=None,
                                               certificate_dir=folder)
    second.stop()
    assert (folder / "server-cert.der").read_bytes() == certificate


def test_certificate_is_issued_for_the_endpoint_host(tmp_path):
    bench = drive(security=["encrypt"]).start(host="0.0.0.0", port=0, control_port=None,
                                              endpoint_host="plc.example",
                                              certificate_dir=tmp_path)
    bench.stop()
    assert bench.url.startswith("opc.tcp://plc.example:")
    assert _dns_names(_certificate(tmp_path / "server-cert.der")) == ["plc.example"]


def test_own_certificate_is_used_and_not_replaced(tmp_path):
    async def make():
        from asyncua.crypto.cert_gen import setup_self_signed_certificate
        from cryptography.x509.oid import ExtendedKeyUsageOID
        await setup_self_signed_certificate(
            tmp_path / "server-key.pem", tmp_path / "server-cert.der", "urn:someone:else",
            "elsewhere", [ExtendedKeyUsageOID.SERVER_AUTH], {"commonName": "mine"})

    asyncio.run(make())
    mine = (tmp_path / "server-cert.der").read_bytes()
    bench = drive(security=["none", "encrypt"]).start(port=0, control_port=None,
                                                      certificate_dir=tmp_path)
    try:
        client = Sync(bench.url).connect()
        endpoints = client.run(client.client.get_endpoints())
        client.close()
    finally:
        bench.stop()
    assert (tmp_path / "server-cert.der").read_bytes() == mine
    assert any(e.ServerCertificate == mine for e in endpoints)


def test_half_a_certificate_pair_is_an_error(tmp_path):
    (tmp_path / "server-cert.der").write_bytes(b"not a certificate")
    with pytest.raises(BenchError, match="server-key.pem"):
        drive(security=["encrypt"]).start(port=0, control_port=None, certificate_dir=tmp_path)

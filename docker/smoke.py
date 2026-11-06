"""Smoke test of the Docker image from outside: python docker/smoke.py [url]

Connects without security and with Basic256Sha256 SignAndEncrypt, and checks that the
endpoints carry the URL a client can reach, which is the URL given here
(default opc.tcp://localhost:4840).
"""
import asyncio
import sys
import tempfile
from pathlib import Path

from asyncua import Client, ua
from asyncua.crypto.cert_gen import setup_self_signed_certificate
from cryptography.x509.oid import ExtendedKeyUsageOID

NAMESPACE = "urn:steitec:opcua-sim"


async def main(url: str) -> None:
    host = url.split("//", 1)[1].split(":", 1)[0]
    async with Client(url, timeout=5) as client:
        endpoints = await client.get_endpoints()
        modes = sorted(e.SecurityMode.name for e in endpoints)
        assert modes == ["None_", "SignAndEncrypt"], modes
        assert all(e.EndpointUrl == url for e in endpoints), \
            [e.EndpointUrl for e in endpoints]
        idx = await client.get_namespace_index(NAMESPACE)
        value = await client.get_node(ua.NodeId("oven.ProcessValue", idx)).read_value()
        print(f"none: oven.ProcessValue = {value}")
    with tempfile.TemporaryDirectory() as folder:
        cert, key = Path(folder) / "client.der", Path(folder) / "client.pem"
        await setup_self_signed_certificate(key, cert, "urn:steitec:smoke", host,
                                            [ExtendedKeyUsageOID.CLIENT_AUTH],
                                            {"commonName": "smoke"})
        client = Client(url, timeout=5)
        client.application_uri = "urn:steitec:smoke"
        await client.set_security_string(f"Basic256Sha256,SignAndEncrypt,{cert},{key}")
        async with client:
            idx = await client.get_namespace_index(NAMESPACE)
            value = await client.get_node(ua.NodeId("pump.Speed", idx)).read_value()
            print(f"encrypt: pump.Speed = {value}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "opc.tcp://localhost:4840"))

"""Command line: run a bench, talk to it over OPC UA, inject faults."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import threading
from typing import Any

from asyncua import Client, ua

from . import __version__
from .bench import DEFAULT_PORT, Bench, BenchError
from .control import CONTROL_PORT, ControlError, request
from .faults import DEVICE, SERVER, FaultError
from .models import MODELS
from .profile import ProfileError, bundled_profiles, load_profile
from .server import NAMESPACE

DEFAULT_URL = f"opc.tcp://127.0.0.1:{DEFAULT_PORT}"


def _value(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return text


def _pairs(items: list[str]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for item in items:
        key, separator, value = item.partition("=")
        if not separator or not key:
            raise FaultError(f"expected name=value, got {item!r}")
        params[key] = _value(value)
    return params


def cmd_serve(args: argparse.Namespace) -> int:
    if args.profile:
        bench = Bench.single(args.profile, name=args.name, security=args.security,
                             tick=args.tick)
    else:
        bench = Bench.from_yaml(args.bench)
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    bench.start(host=args.host, port=args.port, control_port=args.control_port,
                endpoint_host=args.endpoint_host, certificate_dir=args.certificate_dir)
    print(f"opcua-sim: {len(bench.devices)} device(s) on {bench.url}, "
          f"control on :{bench.control_port}", flush=True)
    for device in bench.devices.values():
        print(f"  {device.name}  ({device.profile.name})", flush=True)
    try:
        while not stop.wait(0.2):
            pass
    finally:
        bench.stop()
    return 0


def cmd_profiles(args: argparse.Namespace) -> int:
    for name in bundled_profiles():
        profile = load_profile(name)
        faults = sorted(MODELS[profile.model].FAULTS) if profile.model else []
        extra = f"  [faults: {', '.join(faults)}]" if faults else ""
        print(f"{name:24} {profile.description}{extra}")
    print(f"\nfaults of every device: {', '.join(sorted(DEVICE))}")
    print(f"faults of the server:   {', '.join(sorted(SERVER))}")
    return 0


def cmd_map(args: argparse.Namespace) -> int:
    profile = load_profile(args.profile)
    space = profile.address_space
    print(f"{profile.name}: {profile.description}")
    if space:
        index = space.namespaces.index(space.namespace) + 1
        print(f"\nNamespace [{index}] {space.namespace}")
    print(f"\nVariables{'' if space else ' (under ParameterSet)'}")
    for var in profile.variables:
        access = "rw" if var.writable else "r "
        unit = f" [{var.unit}]" if var.unit else ""
        text = f"  {var.description}" if var.description else ""
        print(f"  {access} {var.name:20} {var.type:8}{unit}{text}")
        if space:
            kind = "i" if isinstance(var.node_id, int) else "s"
            print(f"       ns={index};{kind}={var.node_id}   {'/'.join(var.path)}")
    if profile.methods:
        print(f"\nMethods{'' if space else ' (under MethodSet)'}")
        for method in profile.methods:
            if space:
                kind = "i" if isinstance(method.node_id, int) else "s"
                print(f"  ns={index};{kind}={method.node_id}   {'/'.join(method.path)}")
            inputs = ", ".join(f"{a.name}: {a.type}" for a in method.inputs)
            outputs = ", ".join(f"{a.name}: {a.type}" for a in method.outputs)
            arrow = f" -> {outputs}" if outputs else ""
            print(f"  {method.name}({inputs}){arrow}  {method.description}")
    if profile.alarms:
        print("\nAlarms")
        for alarm in profile.alarms:
            if alarm.kind == "level":
                limits = ", ".join(f"{key}={value:g}" for key, value in alarm.limits.items())
                print(f"  {alarm.name:20} level on {alarm.variable}: {limits}")
            else:
                print(f"  {alarm.name:20} discrete, severity {alarm.severity}: {alarm.message}")
    for title, lines in (("Sources", profile.sources), ("Limits", profile.limits)):
        if lines:
            print(f"\n{title}")
            for line in lines:
                print(f"  - {line}")
    return 0


async def _node_id(client: Client, name: str) -> ua.NodeId:
    if name.startswith(("ns=", "i=", "s=", "g=", "b=")):
        return ua.NodeId.from_string(name)
    try:
        index = await client.get_namespace_index(NAMESPACE)
    except ValueError:
        raise FaultError(f"{name}: this server has its own node IDs, use the full form, "
                         "e.g. 'ns=3;s=...' (see opcua-sim browse)") from None
    return ua.NodeId(name, index)


def _show(value: ua.DataValue) -> str:
    status = value.StatusCode
    text = "null" if value.Value is None or value.Value.Value is None else str(value.Value.Value)
    label = status.name if status is not None and not status.is_good() else "Good"
    stamp = value.SourceTimestamp.isoformat(timespec="seconds") if value.SourceTimestamp else "-"
    return f"{text}  [{label}, {stamp}]"


def _client(args: argparse.Namespace) -> Client:
    client = Client(args.url, timeout=args.timeout)
    if args.user:
        client.set_user(args.user)
        client.set_password(args.password or "")
    return client


async def _browse(node: Any, depth: int, indent: int) -> None:
    for child in await node.get_children():
        if child.nodeid == ua.NodeId(ua.ObjectIds.Server):
            continue
        name = (await child.read_browse_name()).Name
        node_class = await child.read_node_class()
        print(f"{' ' * indent}{name}  ({node_class.name}, {child.nodeid.to_string()})")
        if depth > 1 and node_class == ua.NodeClass.Object:
            await _browse(child, depth - 1, indent + 2)


async def _a_browse(args: argparse.Namespace) -> int:
    async with _client(args) as client:
        if args.node:
            start = client.get_node(await _node_id(client, args.node))
        elif NAMESPACE in await client.get_namespace_array():
            start = client.get_node(await _node_id(client, "DeviceSet"))
        else:
            start = client.nodes.objects
        await _browse(start, args.depth, 0)
    return 0


async def _a_read(args: argparse.Namespace) -> int:
    async with _client(args) as client:
        for name in args.nodes:
            node = client.get_node(await _node_id(client, name))
            print(f"{name}: {_show(await node.read_data_value(raise_on_bad_status=False))}")
    return 0


async def _a_write(args: argparse.Namespace) -> int:
    async with _client(args) as client:
        node = client.get_node(await _node_id(client, args.node))
        vtype = await node.read_data_type_as_variant_type()
        value = _value(args.value)
        if vtype == ua.VariantType.Boolean:
            value = bool(value)
        elif vtype in (ua.VariantType.Double, ua.VariantType.Float):
            value = float(value)
        elif vtype == ua.VariantType.String:
            value = str(value)
        else:
            value = int(value)
        await node.write_value(ua.DataValue(ua.Variant(value, vtype)))
        print(f"{args.node}: written {value}")
    return 0


async def _a_call(args: argparse.Namespace) -> int:
    async with _client(args) as client:
        if args.object:
            parent = client.get_node(await _node_id(client, args.object))
        else:
            device, _, _method = args.method.rpartition(".")
            if not device or args.method.startswith("ns="):
                raise FaultError("method must be device.Method, or use --object with a node ID")
            parent = client.get_node(await _node_id(client, f"{device}.MethodSet"))
        node = client.get_node(await _node_id(client, args.method))
        result = await parent.call_method(node, *[_value(a) for a in args.arguments])
        print(f"{args.method}: {'ok' if result is None else result}")
    return 0


class _Printer:
    def datachange_notification(self, node: Any, value: Any, data: Any) -> None:
        print(f"{node.nodeid.Identifier}: {_show(data.monitored_item.Value)}", flush=True)

    def event_notification(self, event: Any) -> None:
        fields = event.get_event_props_as_fields_dict()

        def pick(name: str) -> Any:
            return fields[name].Value if name in fields else None

        message = pick("Message")
        print(f"EVENT {pick('SourceName')}.{pick('ConditionName')} severity={pick('Severity')} "
              f"active={pick('ActiveState/Id')} acked={pick('AckedState/Id')}: "
              f"{message.Text if message else ''}", flush=True)


async def _a_watch(args: argparse.Namespace) -> int:
    async with _client(args) as client:
        subscription = await client.create_subscription(args.interval, _Printer())
        nodes = [client.get_node(await _node_id(client, name)) for name in args.nodes]
        if nodes:
            await subscription.subscribe_data_change(nodes)
        if args.alarms:
            await subscription.subscribe_events(ua.ObjectIds.Server,
                                                ua.ObjectIds.AlarmConditionType)
        await asyncio.sleep(args.seconds if args.seconds else 1e9)
    return 0


def _control(args: argparse.Namespace, message: dict[str, Any]) -> dict[str, Any]:
    return request(args.control_port, message, args.control_host)


def cmd_inject(args: argparse.Namespace) -> int:
    _control(args, {"target": args.target, "inject": args.kind, "params": _pairs(args.params)})
    return 0


def cmd_clear(args: argparse.Namespace) -> int:
    _control(args, {"target": args.target, "clear": args.kind or True})
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    _control(args, {"target": args.target, "set": _pairs(args.values)})
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    names = args.names or True
    print(json.dumps(_control(args, {"target": args.target, "get": names})["values"], indent=2))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    status = _control(args, {"list": True})
    print(status["url"])
    server = status["server"]
    print(f"server  connections so far={server['connections']} "
          f"faults={sorted(server['faults'])}")
    for name, device in status["devices"].items():
        active = [key for key, alarm in device["alarms"].items() if alarm["active"]]
        print(f"{name}  ({device['profile']}) health={device['health']} "
              f"faults={sorted(device['faults'])} alarms={active}")
    return 0


def parser() -> argparse.ArgumentParser:
    main_parser = argparse.ArgumentParser(prog="opcua-sim", description=__doc__)
    main_parser.add_argument("--version", action="version", version=f"opcua-sim {__version__}")
    main_parser.add_argument("-v", "--verbose", action="store_true",
                             help="show asyncua's log messages")
    commands = main_parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="run a bench")
    what = serve.add_mutually_exclusive_group(required=True)
    what.add_argument("--bench", help="bench file (YAML)")
    what.add_argument("--profile", help="one device: a bundled profile or a profile file")
    serve.add_argument("--name", help="device name with --profile")
    serve.add_argument("--host", default="127.0.0.1", help="address to bind (default loopback)")
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve.add_argument("--control-port", type=int, default=CONTROL_PORT)
    serve.add_argument("--endpoint-host", default=os.environ.get("OPCUA_SIM_ENDPOINT_HOST"),
                       help="host name in the endpoint URL and the certificate "
                            "(default $OPCUA_SIM_ENDPOINT_HOST)")
    serve.add_argument("--certificate-dir", default=os.environ.get("OPCUA_SIM_CERTIFICATE_DIR"),
                       help="keep the server certificate here instead of a new one at every "
                            "start (default $OPCUA_SIM_CERTIFICATE_DIR)")
    serve.add_argument("--security", action="append", choices=["none", "sign", "encrypt"],
                       help="with --profile; repeat for several endpoints")
    serve.add_argument("--tick", type=float, default=0.1, help="simulation step in seconds")
    serve.set_defaults(run=cmd_serve)

    profiles = commands.add_parser("profiles", help="list the bundled profiles and faults")
    profiles.set_defaults(run=cmd_profiles)
    mapping = commands.add_parser("map", help="show the address space of a profile")
    mapping.add_argument("profile")
    mapping.set_defaults(run=cmd_map)

    def client_options(command: argparse.ArgumentParser) -> None:
        command.add_argument("--url", default=DEFAULT_URL)
        command.add_argument("--timeout", type=float, default=4.0)
        command.add_argument("--user")
        command.add_argument("--password")

    browse = commands.add_parser("browse", help="show the devices of a server")
    browse.add_argument("node", nargs="?")
    browse.add_argument("--depth", type=int, default=3)
    client_options(browse)
    browse.set_defaults(run=lambda a: asyncio.run(_a_browse(a)))
    read = commands.add_parser("read", help="read variables, e.g. oven.ProcessValue")
    read.add_argument("nodes", nargs="+")
    client_options(read)
    read.set_defaults(run=lambda a: asyncio.run(_a_read(a)))
    write = commands.add_parser("write", help="write a variable")
    write.add_argument("node")
    write.add_argument("value")
    client_options(write)
    write.set_defaults(run=lambda a: asyncio.run(_a_write(a)))
    call = commands.add_parser("call", help="call a method, e.g. pump.Start")
    call.add_argument("method")
    call.add_argument("arguments", nargs="*")
    call.add_argument("--object", help="node ID of the method's object, for real-product profiles")
    client_options(call)
    call.set_defaults(run=lambda a: asyncio.run(_a_call(a)))
    watch = commands.add_parser("watch", help="subscribe to variables and alarms")
    watch.add_argument("nodes", nargs="*")
    watch.add_argument("--alarms", action="store_true")
    watch.add_argument("--interval", type=float, default=500, help="publishing interval in ms")
    watch.add_argument("--seconds", type=float, help="stop after this time")
    client_options(watch)
    watch.set_defaults(run=lambda a: asyncio.run(_a_watch(a)))

    def control_options(command: argparse.ArgumentParser) -> None:
        command.add_argument("--control-port", type=int, default=CONTROL_PORT)
        command.add_argument("--control-host", default="127.0.0.1")

    inject = commands.add_parser("inject", help="inject a fault into a device or the server")
    inject.add_argument("target")
    inject.add_argument("kind")
    inject.add_argument("params", nargs="*", metavar="name=value")
    control_options(inject)
    inject.set_defaults(run=cmd_inject)
    clear = commands.add_parser("clear", help="clear one fault kind or all")
    clear.add_argument("target")
    clear.add_argument("kind", nargs="?")
    control_options(clear)
    clear.set_defaults(run=cmd_clear)
    force = commands.add_parser("set", help="force values from outside (null = back to normal)")
    force.add_argument("target")
    force.add_argument("values", nargs="+", metavar="name=value")
    control_options(force)
    force.set_defaults(run=cmd_set)
    get = commands.add_parser("get", help="read simulated values over the control channel")
    get.add_argument("target")
    get.add_argument("names", nargs="*")
    control_options(get)
    get.set_defaults(run=cmd_get)
    status = commands.add_parser("list", help="show the state of a running bench")
    control_options(status)
    status.set_defaults(run=cmd_list)
    return main_parser


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.ERROR)
    if not args.verbose:
        logging.getLogger("asyncua").setLevel(logging.CRITICAL)
    try:
        return args.run(args)
    except (BenchError, ProfileError, FaultError, ControlError) as error:
        print(f"opcua-sim: {error}", file=sys.stderr)
    except ua.UaStatusCodeError as error:
        print(f"opcua-sim: {error}", file=sys.stderr)
    except asyncio.TimeoutError:
        print("opcua-sim: no answer in time", file=sys.stderr)
    except OSError as error:
        print(f"opcua-sim: no connection: {error}", file=sys.stderr)
    except KeyboardInterrupt:
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

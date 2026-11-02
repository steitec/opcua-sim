"""The Python layer of the browser demo.

A browser has no TCP sockets and the OPC UA stack (asyncua) does not run here. So this layer
answers OPC UA services directly: the devices, models, faults and alarms underneath are the real
opcua-sim code, the wire protocol is not. It keeps the rules a client meets on the real server:
status codes, null for Bad values, refused writes and calls, Acknowledge by EventId, a session
that a dropped connection or a restart takes with it.

The page talks to it in JSON strings, so Python's None arrives in JavaScript as null.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any

import yaml

from opcua_sim import load_profile
from opcua_sim.device import Alarm, Device, coerce
from opcua_sim.faults import FaultError, check_server, check_server_kind
from opcua_sim.models import HEALTH, StatusError
from opcua_sim.profile import NUMERIC

NS0 = "http://opcfoundation.org/UA/"
OWN_NAMESPACE = "urn:steitec:opcua-sim"
HELP = """read NODE ...          read values, e.g. read pump.Speed
write NODE VALUE       write a value
call METHOD            call a method, e.g. call pump.Start
browse                 list what this server offers
namespaces             show the namespace array
alarms                 list the alarms that are still retained
ack DEVICE.ALARM [TEXT]  Acknowledge, e.g. ack pump.Overload seen it
refresh                ConditionRefresh: send the retained alarms again
inject TARGET KIND [name=value ...]   a fault on a device or on "server"
clear TARGET [KIND]    clear faults
Node IDs: the full form ns=3;s="DB_Line"."Speed" or ns=4;i=6205 always works; on the plant
also the short form pump.Speed."""
_MAX_STEP = 5.0
_MAX_DELAY = 5.0


class Refused(Exception):
    """An OPC UA service answers with this status code."""


def _value_text(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.4g}" if abs(value) < 10000 else f"{value:.1f}"
    return str(value)


class Endpoint:
    """One simulated server with the node IDs a client would use there."""

    def __init__(self, name: str, url: str, namespaces: list[str], devices: dict[str, Device]):
        self.name = name
        self.url = url
        self.namespaces = namespaces
        self.devices = devices
        self.nodes: dict[str, tuple[Device, str, str]] = {}   # node ID -> device, kind, name
        self.connected = False
        self.faults: dict[str, dict[str, Any]] = {}
        self.down = 0.0                 # seconds until a restart is over
        self.missed = 0
        self._index_nodes()

    def _index_nodes(self) -> None:
        for device in self.devices.values():
            space = device.profile.address_space
            if space is None:
                index = self.namespaces.index(OWN_NAMESPACE)
                for name in device.variables:
                    self.nodes[f"ns={index};s={device.name}.{name}"] = (device, "variable", name)
                for name in device.methods:
                    self.nodes[f"ns={index};s={device.name}.{name}"] = (device, "method", name)
                self.nodes[f"ns={index};s={device.name}.DeviceHealth"] = (device, "health", "")
                labels = {"manufacturer": "Manufacturer", "model": "Model",
                          "serial_number": "SerialNumber"}
                for key, label in labels.items():
                    self.nodes[f"ns={index};s={device.name}.{label}"] = (device, "text", key)
            else:
                index = space.namespaces.index(space.namespace) + 1
                for items, kind in ((device.variables, "variable"), (device.methods, "method")):
                    for name, spec in items.items():
                        form = "i" if isinstance(spec.node_id, int) else "s"
                        self.nodes[f"ns={index};{form}={spec.node_id}"] = (device, kind, name)

    def resolve(self, text: str) -> tuple[str, Device, str, str]:
        if not text.startswith("ns="):
            if OWN_NAMESPACE not in self.namespaces:
                raise Refused("BadNodeIdUnknown")
            text = f"ns={self.namespaces.index(OWN_NAMESPACE)};s={text}"
        if text not in self.nodes:
            raise Refused("BadNodeIdUnknown")
        return (text, *self.nodes[text])


class Demo:
    def __init__(self, bench_file: str):
        self.bench_file = bench_file
        self.events: list[dict[str, Any]] = []
        self._build()
        self.last = time.monotonic()

    def _build(self) -> None:
        with open(self.bench_file, encoding="utf-8") as stream:
            bench = yaml.safe_load(stream)
        plant = {item["name"]: Device(item["name"], load_profile(item["profile"]),
                                      item.get("params"), item.get("serial_number"))
                 for item in bench["devices"]}
        self.endpoints = {
            "plant": Endpoint("plant", "opc.tcp://127.0.0.1:4840",
                              [NS0, "urn:opcua-sim:server", OWN_NAMESPACE], plant),
            "s7": self._product("s7", "plc", "siemens-s7-1500"),
            "reader": self._product("reader", "reader", "leuze-bcl300"),
        }

    @staticmethod
    def _product(name: str, device: str, profile: str) -> Endpoint:
        loaded = load_profile(profile)
        return Endpoint(name, "opc.tcp://127.0.0.1:4840", [NS0, *loaded.address_space.namespaces],
                        {device: Device(device, loaded)})

    # -- time and events -------------------------------------------------------------------

    def advance(self, seconds: float) -> None:
        for endpoint in self.endpoints.values():
            endpoint.down = max(0.0, endpoint.down - seconds)
            for device in endpoint.devices.values():
                device.step(seconds)
            self._collect(endpoint)

    def _tick(self) -> None:
        now = time.monotonic()
        step, self.last = min(_MAX_STEP, now - self.last), now
        if step > 0:
            self.advance(step)

    def _collect(self, endpoint: Endpoint) -> None:
        for device in endpoint.devices.values():
            pending, device.pending = device.pending, []
            for alarm in pending:
                self._event(endpoint, device, alarm)

    def _event(self, endpoint: Endpoint, device: Device, alarm: Alarm) -> None:
        # Like a subscription: what happens without a session never reaches the client.
        if not endpoint.connected:
            endpoint.missed += 1
            return
        self.events.append({"endpoint": endpoint.name, "text": self._alarm_line(device, alarm)})

    @staticmethod
    def _alarm_line(device: Device, alarm: Alarm) -> str:
        return (f"{device.name}.{alarm.spec.name} severity={alarm.severity if alarm.active else 1} "
                f"active={'yes' if alarm.active == 'active' else alarm.active or 'no'} acked={'yes' if alarm.acked else 'no'} "
                f"retain={'yes' if alarm.retain else 'no'}: {alarm.message or alarm.spec.message}")

    # -- the connection --------------------------------------------------------------------

    def _session(self, endpoint: Endpoint, lines: list[dict[str, Any]]) -> None:
        """What the network and the server do to a request before any service runs."""
        if endpoint.down > 0:
            lines.append({"kind": "error", "text": "no connection: the server is restarting"})
            raise Refused("")
        if "blackhole" in endpoint.faults:
            lines.append({"kind": "timeout", "text": "no answer in time: the connection is "
                                                     "open, but nothing gets through"})
            raise Refused("")
        if not endpoint.connected:
            if "refuse" in endpoint.faults:
                lines.append({"kind": "error", "text": "no connection: closed by the server"})
                raise Refused("")
            endpoint.connected = True
            note = "# new session"
            if endpoint.missed:
                note += f": {endpoint.missed} alarm event(s) were missed, try refresh"
                endpoint.missed = 0
            lines.append({"kind": "note", "text": note})

    def _drop(self, endpoint: Endpoint) -> None:
        endpoint.connected = False

    # -- commands --------------------------------------------------------------------------

    def run(self, target: str, line: str) -> str:
        """Run one terminal line against an endpoint; returns JSON with lines and state."""
        self._tick()
        endpoint = self.endpoints[target]
        lines: list[dict[str, Any]] = []
        words = line.split()
        wait = 0.0
        try:
            if not words:
                raise FaultError("empty command")
            command, args = words[0].lower(), words[1:]
            if command == "help":
                lines.extend({"kind": "note", "text": text} for text in HELP.splitlines())
            elif command == "inject":
                self._inject(endpoint, args, lines)
            elif command == "clear":
                self._clear(endpoint, args, lines)
            elif command in ("read", "write", "call", "browse", "namespaces", "alarms", "ack",
                             "refresh"):
                self._session(endpoint, lines)
                wait = float(endpoint.faults.get("delay", {}).get("seconds", 0.0))
                getattr(self, f"_{command}")(endpoint, args, lines, line)
                self._collect(endpoint)
            else:
                raise FaultError(f"unknown command {words[0]!r}, try help")
        except Refused as refused:
            if str(refused):
                lines.append({"kind": "error", "text": str(refused)})
        except (FaultError, ValueError) as error:
            lines.append({"kind": "error", "text": str(error)})
        return json.dumps({"lines": lines, "wait": min(wait, _MAX_DELAY), "state": self._state()})

    def _read(self, endpoint: Endpoint, args: list[str], lines: list[dict[str, Any]],
              _line: str) -> None:
        if not args:
            raise FaultError("read needs a node ID")
        for text in args:
            lines.append({"kind": "tx", "text": f"tx  Read {text}"})
            try:
                node, device, kind, name = endpoint.resolve(text)
                if kind == "method":
                    raise Refused("BadAttributeIdInvalid")
            except Refused as refused:
                lines.append({"kind": "error", "text": f"{text}: {refused}"})
                continue
            device.requests += 1
            lines.append({"kind": "value", "text": f"{text}: {self._shown(device, kind, name)}"})

    @staticmethod
    def _shown(device: Device, kind: str, name: str) -> str:
        if kind == "health":
            return f"{HEALTH.index(device.health)} ({device.health})  [Good]"
        if kind == "text":
            return f"{device.identification.get(name, '')}  [Good]"
        sample = device.samples[name]
        status = sample.status or "Good"
        bad = status.startswith("Bad")
        stamp = sample.timestamp.strftime("%H:%M:%S") if sample.timestamp else "-"
        return f"{'null' if bad else _value_text(sample.value)}  [{status}, {stamp}]"

    def _write(self, endpoint: Endpoint, args: list[str], lines: list[dict[str, Any]],
               _line: str) -> None:
        if len(args) != 2:
            raise FaultError("write needs a node ID and a value")
        lines.append({"kind": "tx", "text": f"tx  Write {args[0]} = {args[1]}"})
        _, device, kind, name = endpoint.resolve(args[0])
        if kind != "variable":
            raise Refused("BadNotWritable")
        spec = device.variables[name]
        try:
            if spec.type == "Boolean":
                if args[1].lower() not in ("true", "false", "0", "1"):
                    raise ValueError
                value: Any = args[1].lower() in ("true", "1")
            elif spec.type in NUMERIC:
                value = coerce(float(args[1]), spec.type)
            else:
                value = args[1]
        except ValueError:
            raise Refused("BadTypeMismatch") from None
        refused = device.write(name, value)
        if refused:
            raise Refused(refused)
        device.step(0.0)
        lines.append({"kind": "value", "text": f"{args[0]}: written, Good"})

    def _call(self, endpoint: Endpoint, args: list[str], lines: list[dict[str, Any]],
              _line: str) -> None:
        if len(args) != 1:
            raise FaultError("call needs a method; the methods here take no arguments")
        lines.append({"kind": "tx", "text": f"tx  Call {args[0]}"})
        _, device, kind, name = endpoint.resolve(args[0])
        if kind != "method":
            raise Refused("BadMethodInvalid")
        try:
            result = device.call(name, [])
        except StatusError as error:
            raise Refused(error.status) from None
        device.step(0.0)
        shown = ", ".join(_value_text(value) for value in result)
        lines.append({"kind": "value", "text": f"{args[0]}: Good{' -> ' + shown if shown else ''}"})

    def _browse(self, endpoint: Endpoint, _args: list[str], lines: list[dict[str, Any]],
                _line: str) -> None:
        lines.append({"kind": "tx", "text": "tx  Browse"})
        for node, (_device, kind, _name) in endpoint.nodes.items():
            lines.append({"kind": "value", "text": f"{kind:9}{node}"})

    def _namespaces(self, endpoint: Endpoint, _args: list[str], lines: list[dict[str, Any]],
                    _line: str) -> None:
        for index, uri in enumerate(endpoint.namespaces):
            lines.append({"kind": "value", "text": f"[{index}] {uri}"})

    def _retained(self, endpoint: Endpoint) -> list[tuple[Device, Alarm]]:
        return [(device, alarm) for device in endpoint.devices.values()
                for alarm in device.alarms.values() if alarm.retain]

    def _alarms(self, endpoint: Endpoint, _args: list[str], lines: list[dict[str, Any]],
                _line: str) -> None:
        retained = self._retained(endpoint)
        for device, alarm in retained:
            lines.append({"kind": "value", "text": self._alarm_line(device, alarm)})
        if not retained:
            lines.append({"kind": "note", "text": "# no alarm is retained"})

    def _ack(self, endpoint: Endpoint, args: list[str], lines: list[dict[str, Any]],
             line: str) -> None:
        if not args or "." not in args[0]:
            raise FaultError("ack needs DEVICE.ALARM, e.g. ack pump.Overload")
        device_name, _, alarm_name = args[0].partition(".")
        device = endpoint.devices.get(device_name)
        if device is None or alarm_name not in device.alarms:
            raise Refused("BadNodeIdInvalid")
        alarm = device.alarms[alarm_name]
        comment = line.split(None, 2)[2] if len(args) > 1 else ""
        lines.append({"kind": "tx", "text": f"tx  Acknowledge {args[0]} EventId "
                                            f"{alarm.event_id.hex()[:8] or '-'}"})
        refused = device.acknowledge(alarm_name, alarm.event_id, comment)
        if refused:
            raise Refused(refused)
        lines.append({"kind": "event", "text": f"event  {self._alarm_line(device, alarm)}"})

    def _refresh(self, endpoint: Endpoint, _args: list[str], lines: list[dict[str, Any]],
                 _line: str) -> None:
        lines.append({"kind": "tx", "text": "tx  ConditionRefresh"})
        lines.append({"kind": "event", "text": "event  RefreshStartEvent"})
        for device, alarm in self._retained(endpoint):
            lines.append({"kind": "event", "text": f"event  {self._alarm_line(device, alarm)}"})
        lines.append({"kind": "event", "text": "event  RefreshEndEvent"})

    # -- faults ----------------------------------------------------------------------------

    def _inject(self, endpoint: Endpoint, args: list[str], lines: list[dict[str, Any]]) -> None:
        if len(args) < 2:
            raise FaultError("inject needs a target and a fault, e.g. inject pump trip code=4")
        params: dict[str, Any] = {}
        for item in args[2:]:
            key, separator, value = item.partition("=")
            if not separator or not key:
                raise FaultError(f"expected name=value, got {item!r}")
            try:
                params[key] = json.loads(value)
            except ValueError:
                params[key] = value
        if args[0] == "server":
            self._server_fault(endpoint, args[1], params, lines)
        else:
            device = self._device(endpoint, args[0])
            device.inject(args[1], params)
            device.step(0.0)
            self._collect(endpoint)
            lines.append({"kind": "note", "text": f"# fault on {args[0]}: {args[1]}"})

    def _server_fault(self, endpoint: Endpoint, kind: str, params: dict[str, Any],
                      lines: list[dict[str, Any]]) -> None:
        full = check_server(kind, params)
        if kind == "drop_connections":
            self._drop(endpoint)
            lines.append({"kind": "note", "text": "# all connections were cut, the session and "
                                                  "its subscriptions are gone"})
        elif kind == "restart":
            self._drop(endpoint)
            endpoint.down = full["downtime"]
            lines.append({"kind": "note", "text": f"# the server restarts, back in "
                                                  f"{full['downtime']:g} s without any session"})
        else:
            endpoint.faults[kind] = full
            lines.append({"kind": "note", "text": f"# fault on the server: {kind}"})

    def _clear(self, endpoint: Endpoint, args: list[str], lines: list[dict[str, Any]]) -> None:
        if not args:
            raise FaultError("clear needs a target, e.g. clear pump")
        kind = args[1] if len(args) > 1 else None
        if args[0] == "server":
            if kind is not None:
                check_server_kind(kind)
                endpoint.faults.pop(kind, None)
            else:
                endpoint.faults.clear()
        else:
            device = self._device(endpoint, args[0])
            device.clear(kind)
            device.step(0.0)
            self._collect(endpoint)
        lines.append({"kind": "note", "text": f"# cleared {kind or 'all faults'} on {args[0]}"})

    @staticmethod
    def _device(endpoint: Endpoint, name: str) -> Device:
        if name not in endpoint.devices:
            raise FaultError(f"no device {name!r} on this server, it has "
                             f"{', '.join(endpoint.devices)} and server")
        return endpoint.devices[name]

    def reset(self) -> str:
        self.events.clear()
        self._build()
        self.last = time.monotonic()
        return json.dumps(self._state())

    # -- state for the page ----------------------------------------------------------------

    def _state(self) -> dict[str, Any]:
        events, self.events = self.events, []
        return {
            "time": datetime.now(timezone.utc).strftime("%H:%M:%S"),
            "events": events,
            "endpoints": {
                name: {
                    "connected": endpoint.connected,
                    "faults": sorted(endpoint.faults),
                    "restarting": endpoint.down > 0,
                    "alarms": [{"id": f"{device.name}.{alarm.spec.name}",
                                "severity": alarm.severity if alarm.active else 1,
                                "active": alarm.active is not None, "acked": alarm.acked,
                                "message": alarm.spec.message}
                               for device, alarm in self._retained(endpoint)],
                } for name, endpoint in self.endpoints.items()},
            "devices": {
                device.name: {
                    "health": device.health,
                    "faults": sorted(device.active_faults()),
                    "values": {name: None if (sample.status or "").startswith("Bad")
                               else sample.value for name, sample in device.samples.items()},
                    "status": {name: sample.status for name, sample in device.samples.items()
                               if sample.status},
                } for endpoint in self.endpoints.values() for device in endpoint.devices.values()},
        }

    def state(self) -> str:
        self._tick()
        return json.dumps(self._state())

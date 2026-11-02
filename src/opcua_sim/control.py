"""Control channel of a running bench: faults and values from outside the OPC UA connection.

One JSON object per line in, one per line out::

    {"target": "oven", "inject": "sensor_break"}                 -> {"ok": true}
    {"target": "pump", "inject": "trip", "params": {"code": 7}}  -> {"ok": true}
    {"target": "server", "inject": "delay", "params": {"seconds": 2}}
    {"target": "oven", "clear": true}          (or "clear": "sensor_break")
    {"target": "oven", "set": {"Setpoint": 80}}                  (null = back to the simulation)
    {"target": "oven", "get": ["ProcessValue"]}   (or "get": true)
        -> {"ok": true, "values": {"ProcessValue": 21.5}}
    {"list": true} -> {"ok": true, "url": ..., "server": {...}, "devices": {...}}

Anything invalid gets ``{"ok": false, "error": "..."}``; the connection stays open. The
channel has no authentication, so it listens on loopback unless the bench binds to all
interfaces.
"""
from __future__ import annotations

import asyncio
import json
import socket
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .bench import Bench

CONTROL_PORT = 4841
_MAX_LINE = 65536


class ControlError(Exception):
    """The control channel answered with an error, or cannot be reached."""


class ControlServer:
    def __init__(self, bench: "Bench"):
        self.bench = bench
        self._server: asyncio.AbstractServer | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self._tasks: set[asyncio.Task] = set()

    async def start(self, host: str, port: int) -> int:
        self._server = await asyncio.start_server(self._accept, host, port, limit=_MAX_LINE)
        return self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
        for writer in list(self._writers):
            writer.transport.abort()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._server is not None:
            await self._server.wait_closed()
            self._server = None

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.ensure_future(self._serve(reader, writer))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        try:
            while True:
                try:
                    raw = await reader.readline()
                except ValueError:
                    writer.write(json.dumps({"ok": False, "error": "line too long"}).encode()
                                 + b"\n")
                    await writer.drain()
                    return
                if not raw:
                    return
                if not raw.strip():
                    continue
                writer.write(json.dumps(await self.handle(raw)).encode() + b"\n")
                await writer.drain()
        except OSError:
            pass
        finally:
            self._writers.discard(writer)
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, asyncio.CancelledError):
                pass

    async def handle(self, raw: bytes) -> dict[str, Any]:
        try:
            message = json.loads(raw)
            if not isinstance(message, dict):
                raise ValueError("expected a JSON object")
            return {"ok": True, **await self._dispatch(message)}
        except (ValueError, TypeError) as error:
            return {"ok": False, "error": str(error)}

    async def _dispatch(self, message: dict[str, Any]) -> dict[str, Any]:
        bench = self.bench
        if message.get("list"):
            return await bench.async_status()
        target = message.get("target")
        if not isinstance(target, str):
            raise ValueError("'target' (a device name or \"server\") is required")
        if "inject" in message:
            params = message.get("params") or {}
            if not isinstance(params, dict):
                raise ValueError("'params' must be an object")
            await bench.async_inject(target, str(message["inject"]), params)
            return {}
        if "clear" in message:
            kind = message["clear"]
            await bench.async_clear(target, None if kind is True else str(kind))
            return {}
        if "set" in message:
            values = message["set"]
            if not isinstance(values, dict):
                raise ValueError("'set' must be an object")
            for name, value in values.items():
                await bench.async_set(target, name, value)
            return {}
        if "get" in message:
            names = message["get"]
            if names is True:
                return {"values": await bench.async_get(target)}
            if not isinstance(names, list):
                raise ValueError("'get' must be true or a list of names")
            return {"values": {name: await bench.async_get(target, name) for name in names}}
        raise ValueError("expected one of inject, clear, set, get, list")


def request(port: int, message: dict[str, Any], host: str = "127.0.0.1",
            timeout: float = 5.0) -> dict[str, Any]:
    """Send one request to a running bench."""
    try:
        with socket.create_connection((host, port), timeout) as connection:
            connection.sendall(json.dumps(message).encode() + b"\n")
            with connection.makefile("rb") as stream:
                line = stream.readline()
    except OSError as error:
        raise ControlError(f"no bench on control port {port}: {error}") from error
    if not line:
        raise ControlError("control channel closed the connection")
    answer = json.loads(line)
    if not answer.get("ok"):
        raise ControlError(answer.get("error", "unknown error"))
    return answer

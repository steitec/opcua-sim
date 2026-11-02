"""A bench: several devices behind one OPC UA server, running in a background thread."""
from __future__ import annotations

import asyncio
import logging
import socket
import tempfile
import threading
from pathlib import Path
from typing import Any

import yaml

from .control import CONTROL_PORT, ControlServer
from .device import Device
from .faults import SERVER_STATES, FaultError, check_server, check_server_kind
from .link import Link
from .profile import Profile, ProfileError, bundled_profiles, load_profile, parse_profile
from .server import APPLICATION_URI, ROLES, SECURITY, UaServer

DEFAULT_PORT = 4840
SERVER_TARGET = "server"


class BenchError(ValueError):
    """A bench description is invalid or the bench cannot start."""


class Bench:
    def __init__(self, devices: list[Device], *, name: str = "opcua-sim",
                 security: list[str] | None = None, users: dict[str, dict[str, str]] | None = None,
                 anonymous: bool = True, tick: float = 0.1):
        if not devices:
            raise BenchError("a bench needs at least one device")
        by_name = {device.name: device for device in devices}
        if len(by_name) != len(devices):
            raise BenchError("device names must be unique")
        if SERVER_TARGET in by_name:
            raise BenchError(f"{SERVER_TARGET!r} is reserved, call the device something else")
        if len(devices) > 1 and any(d.profile.address_space for d in devices):
            raise BenchError("a profile of a real product is a whole server: it must be the "
                             "only device of its bench")
        security = list(security or ["none"])
        for level in security:
            if level not in SECURITY:
                raise BenchError(f"unknown security level {level!r}, expected one of "
                                 f"{', '.join(SECURITY)}")
        users = {user: dict(entry) if isinstance(entry, dict) else entry
                 for user, entry in (users or {}).items()}
        for user, entry in users.items():
            if not isinstance(entry, dict) or not isinstance(entry.get("password"), str):
                raise BenchError(f"user {user}: needs a password")
            if entry.setdefault("role", "operator") not in ROLES:
                raise BenchError(f"user {user}: role must be one of {', '.join(ROLES)}")
        if not anonymous and not users:
            raise BenchError("anonymous: false needs at least one user")
        if not 0 < tick <= 10:
            raise BenchError("tick must be between 0 and 10 seconds")
        self.devices = by_name
        self.name = name
        self.security = security
        self.users = users
        self.anonymous = anonymous
        self.tick = tick
        self.url: str | None = None
        self.port: int | None = None
        self.control_port: int | None = None
        self.server_faults: dict[str, dict[str, Any]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stopping: asyncio.Event | None = None
        self._ua: UaServer | None = None
        self._link: Link | None = None
        self._control: ControlServer | None = None
        self._restart: asyncio.Task | None = None
        self._certificate_dir: tempfile.TemporaryDirectory | None = None
        self._certificate: tuple[Path, Path] | None = None
        self._endpoint = ""

    # -- construction ----------------------------------------------------------------------

    @classmethod
    def single(cls, profile: str | Path | Profile, name: str | None = None,
               params: dict[str, Any] | None = None, **options: Any) -> "Bench":
        """A bench with one device."""
        loaded = profile if isinstance(profile, Profile) else load_profile(profile)
        device_name = name or loaded.name.replace("-", "_").replace(".", "_")
        try:
            return cls([Device(device_name, loaded, params)], **options)
        except BenchError:
            raise
        except ValueError as error:
            raise BenchError(f"device {device_name}: {error}") from error

    @classmethod
    def from_yaml(cls, path: str | Path) -> "Bench":
        path = Path(path)
        try:
            data = yaml.safe_load(path.read_text("utf-8"))
        except OSError as error:
            raise BenchError(f"cannot read {path}: {error}") from error
        except yaml.YAMLError as error:
            raise BenchError(f"{path}: invalid YAML: {error}") from error
        return cls.from_dict(data, base=path.parent)

    @classmethod
    def from_dict(cls, data: Any, base: Path | None = None) -> "Bench":
        if not isinstance(data, dict):
            raise BenchError("a bench must be a mapping")
        extra = set(data) - {"name", "devices", "security", "users", "anonymous", "tick", "seed"}
        if extra:
            raise BenchError(f"unknown bench key(s): {', '.join(sorted(extra))}")
        items = data.get("devices")
        if not isinstance(items, list) or not items:
            raise BenchError("a bench needs a non-empty 'devices' list")
        seed = int(data.get("seed", 0))
        devices = [_device(item, base, seed) for item in items]
        return cls(devices, name=str(data.get("name", "opcua-sim")),
                   security=data.get("security"), users=data.get("users"),
                   anonymous=bool(data.get("anonymous", True)), tick=float(data.get("tick", 0.1)))

    # -- life cycle ------------------------------------------------------------------------

    def start(self, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
              control_port: int | None = CONTROL_PORT, endpoint_host: str | None = None) -> "Bench":
        """Start in a background thread and return once clients can connect.

        ``port=0`` and ``control_port=0`` pick free ports; ``control_port=None`` has no
        control channel. ``endpoint_host`` is the host name in the endpoint URL, needed when
        binding to all interfaces.
        """
        if self._thread is not None:
            raise BenchError("bench is already running")
        started: dict[str, Any] = {}
        ready = threading.Event()

        def run() -> None:
            asyncio.run(self._main(host, port, control_port, endpoint_host, ready, started))

        self._thread = threading.Thread(target=run, name="opcua-sim", daemon=True)
        self._thread.start()
        ready.wait()
        if "error" in started:
            self._thread.join()
            self._thread = None
            raise started["error"]
        return self

    def stop(self) -> None:
        if self._thread is None:
            return
        loop, stopping = self._loop, self._stopping
        if loop is not None and stopping is not None and not loop.is_closed():
            loop.call_soon_threadsafe(stopping.set)
        self._thread.join()
        self._thread = None
        self._loop = None

    def __enter__(self) -> "Bench":
        if self._thread is None:
            self.start(port=0, control_port=0)
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.stop()

    async def _main(self, host: str, port: int, control_port: int | None,
                    endpoint_host: str | None, ready: threading.Event,
                    started: dict[str, Any]) -> None:
        self._loop = asyncio.get_running_loop()
        self._stopping = asyncio.Event()
        all_interfaces = host in ("", "0.0.0.0")
        try:
            await self._prepare_certificate()
            self._link = Link()
            await self._link.start(None if all_interfaces else host, port)
            self.port = self._link.port
            shown = endpoint_host or (socket.gethostname() if all_interfaces else host)
            self._endpoint = f"opc.tcp://{shown}:{self.port}"
            self.url = self._endpoint
            await self._start_server()
            if control_port is not None:
                self._control = ControlServer(self)
                self.control_port = await self._control.start(
                    "0.0.0.0" if all_interfaces else "127.0.0.1", control_port)
        except Exception as error:  # reported to the caller of start()
            started["error"] = error
            await self._shutdown()
            ready.set()
            return
        ready.set()
        ticker = asyncio.ensure_future(self._ticker())
        await self._stopping.wait()
        ticker.cancel()
        await asyncio.gather(ticker, return_exceptions=True)
        await self._shutdown()

    async def _prepare_certificate(self) -> None:
        if self.security == ["none"] or self._certificate is not None:
            return
        from asyncua.crypto.cert_gen import setup_self_signed_certificate
        from cryptography.x509.oid import ExtendedKeyUsageOID

        self._certificate_dir = tempfile.TemporaryDirectory(prefix="opcua-sim-")
        folder = Path(self._certificate_dir.name)
        pair = (folder / "server-cert.der", folder / "server-key.pem")
        await setup_self_signed_certificate(
            pair[1], pair[0], APPLICATION_URI, socket.gethostname(),
            [ExtendedKeyUsageOID.CLIENT_AUTH, ExtendedKeyUsageOID.SERVER_AUTH],
            {"organizationName": "steitec", "commonName": "opcua-sim"})
        self._certificate = pair

    async def _start_server(self) -> None:
        self._ua = UaServer(self.devices, self._endpoint, name=self.name, security=self.security,
                            users=self.users, anonymous=self.anonymous,
                            certificate=self._certificate)
        inner = await self._ua.start()
        self._link.target = ("127.0.0.1", inner)
        await self._apply_status_faults()

    async def _apply_status_faults(self) -> None:
        state = self.server_faults.get("server_state")
        if state:
            await self._ua.set_server_state(SERVER_STATES[state["state"]])
        level = self.server_faults.get("service_level")
        if level:
            await self._ua.set_service_level(level["value"])

    async def _shutdown(self) -> None:
        if self._restart is not None:
            self._restart.cancel()
            await asyncio.gather(self._restart, return_exceptions=True)
            self._restart = None
        if self._control is not None:
            await self._control.stop()
            self._control = None
        if self._link is not None:
            await self._link.stop()
            self._link = None
        if self._ua is not None:
            await self._ua.stop()
            self._ua = None
        if self._certificate_dir is not None:
            self._certificate_dir.cleanup()
            self._certificate_dir = None
            self._certificate = None

    async def _ticker(self) -> None:
        loop = asyncio.get_running_loop()
        last = loop.time()
        while True:
            await asyncio.sleep(self.tick)
            now = loop.time()
            dt, last = now - last, now
            try:
                for device in self.devices.values():
                    device.step(dt)
                if self._ua is not None and not self._link.down:
                    await self._ua.flush()
            except Exception:  # keep the bench alive, a test will notice the wrong value
                logging.getLogger(__name__).exception("simulation step failed")

    async def _do_restart(self, downtime: float) -> None:
        link = self._link
        link.down = True
        link.drop_all()
        old, self._ua = self._ua, None
        await old.stop()
        await asyncio.sleep(downtime)
        await self._start_server()
        link.down = False

    # -- thread-safe API -------------------------------------------------------------------

    def _run(self, coroutine: Any) -> Any:
        if self._loop is None or self._thread is None:
            coroutine.close()
            raise BenchError("bench is not running")
        if threading.current_thread() is self._thread:
            coroutine.close()
            raise BenchError("use the async_ methods on the bench thread")
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop).result(60)

    def _device(self, name: str) -> Device:
        try:
            return self.devices[name]
        except KeyError:
            raise FaultError(f"no device {name!r}, the bench has "
                             f"{', '.join(self.devices)} and {SERVER_TARGET!r}") from None

    def inject(self, target: str, kind: str, **params: Any) -> None:
        """Inject a fault into a device, or into ``"server"``. See :mod:`opcua_sim.faults`."""
        self._run(self.async_inject(target, kind, params))

    def clear(self, target: str, kind: str | None = None) -> None:
        """Clear one fault kind, or all of them, on a device or on ``"server"``."""
        self._run(self.async_clear(target, kind))

    def set(self, device: str, variable: str, value: Any) -> None:
        """Force a value from outside; ``None`` hands it back to the simulation."""
        self._run(self.async_set(device, variable, value))

    def get(self, device: str, variable: str) -> Any:
        """The simulated value, regardless of status codes or client access."""
        return self._run(self.async_get(device, variable))

    def status(self) -> dict[str, Any]:
        return self._run(self.async_status())

    def wait_restarted(self) -> None:
        """Block until a ``restart`` fault has finished."""
        self._run(self.async_wait_restarted())

    async def async_wait_restarted(self) -> None:
        if self._restart is not None:
            await asyncio.shield(self._restart)

    async def async_inject(self, target: str, kind: str, params: dict[str, Any]) -> None:
        if target != SERVER_TARGET:
            device = self._device(target)
            device.inject(kind, params)
            await self._refresh(device)
            return
        full = check_server(kind, params)
        link = self._link
        if kind == "drop_connections":
            link.drop_all()
            return
        if kind == "restart":
            if self._restart is not None and not self._restart.done():
                raise FaultError("a restart is already running")
            self._restart = asyncio.ensure_future(self._do_restart(full["downtime"]))
            return
        if kind == "delay":
            link.delay = full["seconds"]
        elif kind == "blackhole":
            link.blackhole = True
        elif kind == "refuse":
            link.refuse = True
        self.server_faults[kind] = full
        if self._ua is not None and kind in ("server_state", "service_level"):
            await self._apply_status_faults()

    async def async_clear(self, target: str, kind: str | None) -> None:
        if target != SERVER_TARGET:
            device = self._device(target)
            device.clear(kind)
            await self._refresh(device)
            return
        if kind is not None:
            check_server_kind(kind)
        link = self._link
        for item in [kind] if kind else list(self.server_faults):
            self.server_faults.pop(item, None)
            if item == "delay":
                link.delay = 0.0
            elif item == "blackhole":
                link.blackhole = False
            elif item == "refuse":
                link.refuse = False
            elif item == "server_state" and self._ua is not None:
                await self._ua.set_server_state(SERVER_STATES["Running"])
            elif item == "service_level" and self._ua is not None:
                await self._ua.set_service_level(255)

    async def async_set(self, device: str, variable: str, value: Any) -> None:
        target = self._device(device)
        target.set(variable, value)
        await self._refresh(target)

    async def _refresh(self, device: Device) -> None:
        if self._ua is not None and not self._link.down:
            await self._ua.refresh(device)
        else:
            device.step(0.0)

    async def async_get(self, device: str, variable: str | None = None) -> Any:
        target = self._device(device)
        if variable is None:
            return {name: target.get(name) for name in target.variables}
        return target.get(variable)

    async def async_status(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "server": {"faults": self.server_faults, "connections": self._link.connections,
                       "restarting": self._link.down},
            "devices": {
                name: {
                    "profile": device.profile.name,
                    "health": device.health,
                    "faults": device.active_faults(),
                    "fault_kinds": device.fault_kinds(),
                    "alarms": {alarm.spec.name: {"active": alarm.active, "acked": alarm.acked}
                               for alarm in device.alarms.values()},
                    "requests": device.requests,
                } for name, device in self.devices.items()},
        }


def _device(item: Any, base: Path | None, seed: int) -> Device:
    if not isinstance(item, dict):
        raise BenchError("every device must be a mapping")
    extra = set(item) - {"name", "profile", "params", "serial_number", "inline"}
    if extra:
        raise BenchError(f"unknown device key(s): {', '.join(sorted(extra))}")
    name = item.get("name")
    if not isinstance(name, str) or not name or "." in name or "/" in name or " " in name:
        raise BenchError("a device needs a 'name' without '.', '/' or spaces")
    try:
        if "inline" in item:
            profile = parse_profile(item["inline"], f"device {name}")
        else:
            source = item.get("profile")
            if not isinstance(source, str):
                raise BenchError(f"device {name}: needs 'profile' or 'inline'")
            if source not in bundled_profiles() and base is not None:
                source = str(base / source)
            profile = load_profile(source)
        return Device(name, profile, item.get("params"), item.get("serial_number"), seed)
    except (BenchError, ProfileError):
        raise
    except ValueError as error:
        raise BenchError(f"device {name}: {error}") from error

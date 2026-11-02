"""The OPC UA side: builds the address space with asyncua and keeps it in step with the devices."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from asyncua import Server, ua
from asyncua.common.instantiate_util import instantiate
from asyncua.common.structures104 import new_enum
from asyncua.crypto.permission_rules import User, UserRole
from asyncua.server.event_generator import EventGenerator
from asyncua.server.user_managers import UserManager

from .device import Alarm, Device, Sample, StatusError
from .faults import status_code
from .models import HEALTH
from .profile import ArgumentSpec, VariableSpec

NAMESPACE = "urn:steitec:opcua-sim"
APPLICATION_URI = "urn:steitec:opcua-sim:server"
_LIMIT_TEXT = {"high_high": "HighHigh", "high": "High", "low": "Low", "low_low": "LowLow"}
SECURITY = {
    "none": ua.SecurityPolicyType.NoSecurity,
    "sign": ua.SecurityPolicyType.Basic256Sha256_Sign,
    "encrypt": ua.SecurityPolicyType.Basic256Sha256_SignAndEncrypt,
}
ROLES = ("operator", "viewer")
ANONYMOUS = "anonymous"


def variant_type(name: str) -> ua.VariantType:
    return getattr(ua.VariantType, name)


def _status(name: str | None) -> ua.StatusCode:
    return ua.StatusCode(status_code(name)) if name else ua.StatusCode()


class _Users(UserManager):
    def __init__(self, users: dict[str, dict[str, str]]):
        self.users = users

    def get_user(self, iserver: Any, username: str | None = None, password: str | None = None,
                 certificate: Any = None) -> User | None:
        if username is None:
            return User(role=UserRole.User, name=ANONYMOUS)
        entry = self.users.get(username)
        if entry is None or entry["password"] != password:
            return None
        return User(role=UserRole.User, name=username)


class UaServer:
    """One running asyncua server. A restart builds a new one around the same devices."""

    def __init__(self, devices: dict[str, Device], endpoint: str, *, name: str,
                 security: list[str], users: dict[str, dict[str, str]], anonymous: bool,
                 certificate: tuple[Path, Path] | None = None):
        self.devices = devices
        self.endpoint = endpoint
        self.name = name
        self.security = security
        self.users = users
        self.anonymous = anonymous
        self.certificate = certificate
        self.server = Server(user_manager=_Users(users))
        self.idx = 0
        self.health_type: ua.NodeId | None = None
        self.variable_nodes: dict[ua.NodeId, tuple[Device, str]] = {}
        self.nodes: dict[tuple[str, str], ua.NodeId] = {}
        self.health_nodes: dict[str, ua.NodeId] = {}
        self.conditions: dict[ua.NodeId, tuple[Device, Alarm]] = {}
        self.device_nodes: dict[str, ua.NodeId] = {}
        self.published: dict[ua.NodeId, Any] = {}

    async def start(self) -> int:
        """Start listening on a private loopback port and return it."""
        server = self.server
        await server.init()
        server.set_endpoint(self.endpoint)
        server.socket_address = ("127.0.0.1", 0)
        server.set_server_name(self.name)
        await server.set_application_uri(APPLICATION_URI)
        await server.set_build_info("https://github.com/steitec/opcua-sim", "steitec", "opcua-sim",
                                    _version(), "0", datetime.now(timezone.utc))
        server.set_security_policy([SECURITY[level] for level in self.security])
        tokens: list[type] = []
        if self.anonymous:
            tokens.append(ua.AnonymousIdentityToken)
        if self.users:
            tokens.append(ua.UserNameIdentityToken)
        server.set_identity_tokens(tokens)
        if self.certificate is not None:
            await server.load_certificate(str(self.certificate[0]))
            await server.load_private_key(str(self.certificate[1]))
        self.idx = await server.register_namespace(NAMESPACE)
        health_type = await new_enum(server, self.idx, "DeviceHealthEnumeration", list(HEALTH))
        # DataTypeDefinition alone is OPC UA 1.04; many clients still look for EnumStrings.
        await health_type.add_property(
            ua.NodeId("DeviceHealthEnumeration.EnumStrings", self.idx),
            ua.QualifiedName("EnumStrings", 0), [ua.LocalizedText(text) for text in HEALTH],
            ua.VariantType.LocalizedText)
        self.health_type = health_type.nodeid
        device_set = await server.nodes.objects.add_folder(ua.NodeId("DeviceSet", self.idx),
                                                           ua.QualifiedName("DeviceSet", self.idx))
        for device in self.devices.values():
            await self._add_device(device_set, device)
        self._hook_writes()
        server.link_method(server.get_node(ua.ObjectIds.AcknowledgeableConditionType_Acknowledge),
                           self._acknowledge)
        server.link_method(server.get_node(ua.ObjectIds.ConditionType_ConditionRefresh),
                           self._condition_refresh)
        await server.start()
        await self.flush()
        return server.bserver.port

    async def stop(self) -> None:
        await self.server.stop()

    # -- address space ---------------------------------------------------------------------

    async def _add_device(self, parent: Any, device: Device) -> None:
        idx = self.idx
        name = device.name
        node = await parent.add_object(ua.NodeId(name, idx), ua.QualifiedName(name, idx))
        await node.set_event_notifier([ua.EventNotifier.SubscribeToEvents])
        self.device_nodes[name] = node.nodeid
        await self.server.iserver.isession.add_references([_notifier_ref(node.nodeid)])
        names = {"manufacturer": "Manufacturer", "model": "Model", "serial_number": "SerialNumber",
                 "hardware_revision": "HardwareRevision", "software_revision": "SoftwareRevision",
                 "device_revision": "DeviceRevision"}
        for key, label in names.items():
            if key in device.identification:
                await node.add_property(ua.NodeId(f"{name}.{label}", idx),
                                        ua.QualifiedName(label, idx), device.identification[key],
                                        ua.VariantType.String)
        health = await node.add_variable(ua.NodeId(f"{name}.DeviceHealth", idx),
                                         ua.QualifiedName("DeviceHealth", idx), 0,
                                         ua.VariantType.Int32, datatype=self.health_type)
        self.health_nodes[name] = health.nodeid
        params = await node.add_object(ua.NodeId(f"{name}.ParameterSet", idx),
                                       ua.QualifiedName("ParameterSet", idx))
        for spec in device.variables.values():
            await self._add_variable(params, device, spec)
        if device.methods:
            methods = await node.add_object(ua.NodeId(f"{name}.MethodSet", idx),
                                            ua.QualifiedName("MethodSet", idx))
            for method in device.methods.values():
                created = await methods.add_method(
                    ua.NodeId(f"{name}.{method.name}", idx),
                    ua.QualifiedName(method.name, idx), self._method(device, method.name),
                    [_argument(arg) for arg in method.inputs],
                    [_argument(arg) for arg in method.outputs])
                if method.description:
                    await created.write_attribute(ua.AttributeIds.Description, ua.DataValue(
                        ua.Variant(ua.LocalizedText(method.description))))
        if device.alarms:
            alarms = await node.add_object(ua.NodeId(f"{name}.Alarms", idx),
                                           ua.QualifiedName("Alarms", idx))
            for alarm in device.alarms.values():
                condition = await alarms.add_object(
                    ua.NodeId(f"{name}.Alarms.{alarm.spec.name}", idx),
                    ua.QualifiedName(alarm.spec.name, idx))
                self.conditions[condition.nodeid] = (device, alarm)

    async def _add_variable(self, parent: Any, device: Device, spec: VariableSpec) -> None:
        idx = self.idx
        nodeid = ua.NodeId(f"{device.name}.{spec.name}", idx)
        qname = ua.QualifiedName(spec.name, idx)
        vtype = variant_type(spec.type)
        if spec.unit is not None or spec.range is not None:
            created = await instantiate(parent, self.server.get_node(ua.ObjectIds.AnalogItemType),
                                        nodeid=nodeid, bname=qname,
                                        dname=ua.LocalizedText(spec.name),
                                        instantiate_optional=False)
            node = created[0]
            await node.write_attribute(ua.AttributeIds.DataType,
                                       ua.DataValue(ua.Variant(ua.NodeId(vtype.value),
                                                               ua.VariantType.NodeId)))
            await node.write_value(ua.DataValue(ua.Variant(device.samples[spec.name].value, vtype)))
            low, high = spec.range if spec.range else (float("-inf"), float("inf"))
            eu_range = await node.get_child("EURange")
            await eu_range.write_value(ua.DataValue(ua.Variant(ua.Range(low, high))))
            if spec.unit is not None:
                await node.add_property(
                    ua.NodeId(f"{device.name}.{spec.name}.EngineeringUnits", idx),
                    ua.QualifiedName("EngineeringUnits", 0),
                    ua.EUInformation(DisplayName=ua.LocalizedText(spec.unit),
                                     Description=ua.LocalizedText(spec.unit)),
                    ua.VariantType.ExtensionObject)
        else:
            node = await parent.add_variable(nodeid, qname, device.samples[spec.name].value, vtype)
        if spec.writable:
            await node.set_writable()
        if spec.description:
            await node.write_attribute(ua.AttributeIds.Description,
                                       ua.DataValue(ua.Variant(ua.LocalizedText(spec.description))))
        self.variable_nodes[nodeid] = (device, spec.name)
        self.nodes[(device.name, spec.name)] = nodeid

    # -- simulation to address space -------------------------------------------------------

    async def flush(self) -> None:
        """Publish changed values, then the alarm events they caused."""
        await self.sync()
        for device in self.devices.values():
            pending, device.pending = device.pending, []
            for alarm in pending:
                await self.fire(device, alarm)

    async def refresh(self, device: Device) -> None:
        """Make the effect of a write, call or fault visible at once, not at the next tick."""
        device.step(0.0)
        await self.flush()

    async def sync(self) -> None:
        """Publish what changed since the last call."""
        server = self.server
        for nodeid, (device, name) in self.variable_nodes.items():
            sample: Sample = device.samples[name]
            key = (sample.value, sample.status, sample.timestamp)
            if self.published.get(nodeid) == key:
                continue
            self.published[nodeid] = key
            spec = device.variables[name]
            bad = bool(sample.status) and sample.status.startswith("Bad")
            variant = ua.Variant(None, ua.VariantType.Null) if bad else \
                ua.Variant(sample.value, variant_type(spec.type))
            await server.write_attribute_value(nodeid, ua.DataValue(
                variant, StatusCode=_status(sample.status), SourceTimestamp=sample.timestamp,
                ServerTimestamp=datetime.now(timezone.utc)))
        for name, nodeid in self.health_nodes.items():
            value = HEALTH.index(self.devices[name].health)
            if self.published.get(nodeid) != value:
                self.published[nodeid] = value
                now = datetime.now(timezone.utc)
                await server.write_attribute_value(nodeid, ua.DataValue(
                    ua.Variant(value, ua.VariantType.Int32), SourceTimestamp=now,
                    ServerTimestamp=now))

    async def set_server_state(self, state: int) -> None:
        node = self.server.get_node(ua.ObjectIds.Server_ServerStatus_State)
        await node.write_value(ua.DataValue(ua.Variant(ua.ServerState(state))))

    async def set_service_level(self, value: int) -> None:
        node = self.server.get_node(ua.ObjectIds.Server_ServiceLevel)
        await node.write_value(ua.DataValue(ua.Variant(value, ua.VariantType.Byte)))

    # -- alarms ----------------------------------------------------------------------------

    async def fire(self, device: Device, alarm: Alarm, subscription_id: int | None = None) -> None:
        """Send the present state of a condition as an event, from the device and from Server."""
        spec = alarm.spec
        level = spec.kind == "level"
        etype = ua.ObjectIds.ExclusiveLevelAlarmType if level else ua.ObjectIds.OffNormalAlarmType
        source = self.device_nodes[device.name]
        generator = EventGenerator(self.server.iserver.isession)
        await generator.init(etype, emitting_node=source, add_generates_event=False)
        event = generator.event
        active = alarm.active is not None
        event.SourceNode = source
        event.SourceName = device.name
        event.NodeId = ua.NodeId(f"{device.name}.Alarms.{spec.name}", self.idx)
        event.ConditionName = spec.name
        event.ConditionClassId = ua.NodeId(ua.ObjectIds.ProcessConditionClassType)
        event.ConditionClassName = ua.LocalizedText("ProcessConditionClassType")
        event.BranchId = ua.NodeId()
        event.Retain = alarm.retain
        event.Severity = alarm.severity if active else 1
        event.LastSeverity = alarm.previous_severity or alarm.severity
        event.Quality = ua.StatusCode()
        event.Comment = ua.LocalizedText(alarm.comment)
        event.ClientUserId = ""
        setattr(event, "EnabledState", ua.LocalizedText("Enabled"))
        setattr(event, "EnabledState/Id", True)
        text = _LIMIT_TEXT.get(alarm.active, "Active") if active else "Inactive"
        setattr(event, "ActiveState", ua.LocalizedText(text))
        setattr(event, "ActiveState/Id", active)
        setattr(event, "AckedState",
                ua.LocalizedText("Acknowledged" if alarm.acked else "Unacknowledged"))
        setattr(event, "AckedState/Id", alarm.acked)
        if level:
            for limit, label in (("high_high", "HighHighLimit"), ("high", "HighLimit"),
                                 ("low", "LowLimit"), ("low_low", "LowLowLimit")):
                if limit in spec.limits:
                    setattr(event, label, spec.limits[limit])
            event.add_property("LimitState/CurrentState",
                               ua.LocalizedText(_LIMIT_TEXT.get(alarm.active, "")),
                               ua.VariantType.LocalizedText)
            event.InputNode = self.nodes[(device.name, spec.variable)]
        else:
            event.InputNode = source
        event.EventId = ua.Variant(alarm.event_id, ua.VariantType.ByteString)
        event.Time = alarm.time or datetime.now(timezone.utc)
        event.ReceiveTime = datetime.now(timezone.utc)
        event.Message = ua.LocalizedText(alarm.message or spec.message)
        service = generator.isession.subscription_service
        await service.trigger_event(event, subscription_id=subscription_id)
        event.emitting_node = ua.NodeId(ua.ObjectIds.Server)
        await service.trigger_event(event, subscription_id=subscription_id)

    async def _acknowledge(self, parent: ua.NodeId, *args: Any) -> Any:
        entry = self.conditions.get(parent)
        if entry is None:
            return ua.StatusCode(ua.StatusCodes.BadNodeIdInvalid)
        if len(args) < 2:
            return ua.StatusCode(ua.StatusCodes.BadArgumentsMissing)
        device, alarm = entry
        event_id, comment = args[0].Value, args[1].Value
        text = comment.Text if isinstance(comment, ua.LocalizedText) else ""
        refused = device.acknowledge(alarm.spec.name, event_id, text or "")
        if refused:
            return ua.StatusCode(status_code(refused))
        await self.fire(device, alarm)
        return []

    async def _condition_refresh(self, parent: ua.NodeId, *args: Any) -> Any:
        if not args:
            return ua.StatusCode(ua.StatusCodes.BadArgumentsMissing)
        subscription_id = args[0].Value
        service = self.server.iserver.isession.subscription_service
        if subscription_id not in service.subscriptions:
            return ua.StatusCode(ua.StatusCodes.BadSubscriptionIdInvalid)
        await self._refresh_marker(ua.ObjectIds.RefreshStartEventType, subscription_id)
        for device, alarm in self.conditions.values():
            if alarm.retain:
                await self.fire(device, alarm, subscription_id)
        await self._refresh_marker(ua.ObjectIds.RefreshEndEventType, subscription_id)
        return []

    async def _refresh_marker(self, etype: int, subscription_id: int) -> None:
        generator = EventGenerator(self.server.iserver.isession)
        await generator.init(etype, emitting_node=ua.ObjectIds.Server, add_generates_event=False)
        await generator.trigger(message="ConditionRefresh", subscription_id=subscription_id)

    # -- client writes and method calls ----------------------------------------------------

    def _hook_writes(self) -> None:
        """Answer writes to device variables here: asyncua's setters cannot return a status."""
        service = self.server.iserver.attribute_service
        original = service.write

        async def write(params: ua.WriteParameters, user: User = User(role=UserRole.Admin)):
            results = []
            for item in params.NodesToWrite:
                entry = self.variable_nodes.get(item.NodeId)
                if entry is None or item.AttributeId != ua.AttributeIds.Value:
                    results.extend(await original(ua.WriteParameters(NodesToWrite=[item]), user))
                else:
                    results.append(self._client_write(entry[0], entry[1], item.Value, user))
                    if results[-1].is_good():
                        await self.refresh(entry[0])
            return results

        service.write = write

    def _may_write(self, user: User) -> bool:
        if not self.users:
            return True
        name = getattr(user, "name", None)
        if name == ANONYMOUS:
            return False
        entry = self.users.get(name or "")
        return entry is None or entry.get("role") != "viewer"

    def _client_write(self, device: Device, name: str, value: ua.DataValue,
                      user: User) -> ua.StatusCode:
        if value.Value is None or value.Value.Value is None:
            return ua.StatusCode(ua.StatusCodes.BadTypeMismatch)
        spec = device.variables[name]
        if value.Value.VariantType != variant_type(spec.type):
            return ua.StatusCode(ua.StatusCodes.BadTypeMismatch)
        if spec.writable and not self._may_write(user):
            return ua.StatusCode(ua.StatusCodes.BadUserAccessDenied)
        return _status(device.write(name, value.Value.Value))

    def _method(self, device: Device, name: str) -> Any:
        spec = device.methods[name]

        async def call(parent: ua.NodeId, *args: Any) -> Any:
            if len(args) < len(spec.inputs):
                return ua.StatusCode(ua.StatusCodes.BadArgumentsMissing)
            if len(args) > len(spec.inputs):
                return ua.StatusCode(ua.StatusCodes.BadTooManyArguments)
            for arg, wanted in zip(args, spec.inputs):
                if arg.VariantType != variant_type(wanted.type):
                    return ua.StatusCode(ua.StatusCodes.BadTypeMismatch)
            try:
                results = device.call(name, [arg.Value for arg in args])
            except StatusError as error:
                return ua.StatusCode(status_code(error.status))
            await self.refresh(device)
            return [ua.Variant(value, variant_type(out.type))
                    for value, out in zip(results, spec.outputs)]

        return call


def _notifier_ref(device: ua.NodeId) -> ua.AddReferencesItem:
    """Server -HasNotifier-> device, so browsers see where the events come from."""
    ref = ua.AddReferencesItem()
    ref.IsForward = True
    ref.ReferenceTypeId = ua.NodeId(ua.ObjectIds.HasNotifier)
    ref.SourceNodeId = ua.NodeId(ua.ObjectIds.Server)
    ref.TargetNodeClass = ua.NodeClass.Object
    ref.TargetNodeId = device
    return ref


def _argument(spec: ArgumentSpec) -> ua.Argument:
    argument = ua.Argument()
    argument.Name = spec.name
    argument.DataType = ua.NodeId(variant_type(spec.type).value)
    argument.ValueRank = -1
    argument.ArrayDimensions = []
    argument.Description = ua.LocalizedText(spec.description)
    return argument


def _version() -> str:
    from . import __version__
    return __version__

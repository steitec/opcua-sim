"""Device profiles: YAML files describing the address space of one simulated device."""
from __future__ import annotations

from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from .models import MODELS
from .signals import KINDS

TYPES = ("Boolean", "Byte", "Int16", "UInt16", "Int32", "UInt32", "Int64", "UInt64",
         "Float", "Double", "String", "LocalizedText")
NUMERIC = tuple(t for t in TYPES if t not in ("Boolean", "String", "LocalizedText"))
LIMITS = ("high_high", "high", "low", "low_low")
DEFAULT_SEVERITY = {"high_high": 900, "high": 700, "low": 700, "low_low": 900}
IDENTIFICATION = ("manufacturer", "model", "serial_number", "hardware_revision",
                  "software_revision", "device_revision")


class ProfileError(ValueError):
    """A profile is missing, malformed or inconsistent."""


@dataclass
class VariableSpec:
    name: str
    type: str = "Double"
    description: str = ""
    unit: str | None = None
    range: tuple[float, float] | None = None
    writable: bool = False
    min: float | None = None
    max: float | None = None
    model: str | None = None
    source: dict[str, Any] | None = None
    value: Any = None
    node_id: str | int | None = None        # with an address_space: the published identifier
    path: list[str] = field(default_factory=list)
    browse_namespace: int | None = None


@dataclass
class ArgumentSpec:
    name: str
    type: str = "Double"
    description: str = ""


@dataclass
class MethodSpec:
    name: str
    model: str
    description: str = ""
    inputs: list[ArgumentSpec] = field(default_factory=list)
    outputs: list[ArgumentSpec] = field(default_factory=list)
    node_id: str | int | None = None
    path: list[str] = field(default_factory=list)
    browse_namespace: int | None = None


@dataclass
class FolderSpec:
    path: list[str]
    node_id: str | int | None = None
    browse_namespace: int | None = None


@dataclass
class AddressSpaceSpec:
    """The server of one product: its namespaces and folders instead of DeviceSet/<device>."""
    namespace: str
    namespaces: list[str]                   # NamespaceArray from index 1
    folders: list[FolderSpec] = field(default_factory=list)


@dataclass
class AlarmSpec:
    name: str
    kind: str                       # "level" or "discrete"
    message: str
    variable: str | None = None     # level
    limits: dict[str, float] = field(default_factory=dict)
    severities: dict[str, int] = field(default_factory=dict)
    deadband: float = 0.0
    when: str | None = None         # discrete: name of a model flag
    severity: int = 700


@dataclass
class Profile:
    name: str
    description: str = ""
    identification: dict[str, str] = field(default_factory=dict)
    model: str | None = None
    model_params: dict[str, Any] = field(default_factory=dict)
    variables: list[VariableSpec] = field(default_factory=list)
    methods: list[MethodSpec] = field(default_factory=list)
    alarms: list[AlarmSpec] = field(default_factory=list)
    address_space: AddressSpaceSpec | None = None
    sources: list[str] = field(default_factory=list)
    limits: list[str] = field(default_factory=list)


def _mapping(data: Any, where: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ProfileError(f"{where}: expected a mapping")
    return data


def _unknown(data: dict[str, Any], known: set[str], where: str) -> None:
    extra = set(data) - known
    if extra:
        raise ProfileError(f"{where}: unknown key(s) {', '.join(sorted(map(str, extra)))}")


def _name(data: dict[str, Any], where: str) -> str:
    name = data.get("name")
    if not isinstance(name, str) or not name or "." in name or "/" in name or " " in name:
        raise ProfileError(f"{where}: 'name' must be a non-empty string without '.', '/' or spaces")
    return name


def _number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProfileError(f"{where}: expected a number, got {value!r}")
    return float(value)


def _placement(data: dict[str, Any], where: str) -> dict[str, Any]:
    node_id = data.get("node_id")
    if node_id is not None and (isinstance(node_id, bool) or not isinstance(node_id, (str, int))):
        raise ProfileError(f"{where}: 'node_id' must be a string or a number")
    path = data.get("path") or []
    if not isinstance(path, list) or not all(isinstance(item, str) and item for item in path):
        raise ProfileError(f"{where}: 'path' must be a list of folder names")
    browse = data.get("browse_namespace")
    if browse is not None and (isinstance(browse, bool) or not isinstance(browse, int)):
        raise ProfileError(f"{where}: 'browse_namespace' must be a namespace index")
    return {"node_id": node_id, "path": path, "browse_namespace": browse}


def _address_space(data: Any, where: str) -> AddressSpaceSpec:
    where = f"{where} address_space"
    data = _mapping(data, where)
    _unknown(data, {"namespace", "namespaces", "folders"}, where)
    namespace, namespaces = data.get("namespace"), data.get("namespaces")
    if not isinstance(namespaces, list) or not all(isinstance(uri, str) for uri in namespaces) \
            or len(set(namespaces)) != len(namespaces):
        raise ProfileError(f"{where}: 'namespaces' must be a list of different URIs from index 1")
    if namespace not in namespaces:
        raise ProfileError(f"{where}: 'namespace' must be one of 'namespaces'")
    folders = []
    for item in data.get("folders") or []:
        item = _mapping(item, f"{where} folder")
        _unknown(item, {"node_id", "path", "browse_namespace"}, f"{where} folder")
        placed = _placement(item, f"{where} folder")
        if not placed["path"]:
            raise ProfileError(f"{where} folder: 'path' is required")
        folders.append(FolderSpec(**placed))
    return AddressSpaceSpec(namespace=namespace, namespaces=namespaces, folders=folders)


def _variable(data: Any, where: str) -> VariableSpec:
    data = _mapping(data, where)
    _unknown(data, {"name", "type", "description", "unit", "range", "writable", "min", "max",
                    "model", "source", "value", "node_id", "path", "browse_namespace"}, where)
    name = _name(data, where)
    where = f"{where} ({name})"
    vtype = data.get("type", "Double")
    if vtype not in TYPES:
        raise ProfileError(f"{where}: unknown type {vtype!r}, expected one of {', '.join(TYPES)}")
    drivers = [key for key in ("model", "source", "value") if data.get(key) is not None]
    if len(drivers) > 1:
        raise ProfileError(f"{where}: use only one of {', '.join(drivers)}")
    span = data.get("range")
    if span is not None:
        if vtype not in NUMERIC:
            raise ProfileError(f"{where}: 'range' needs a numeric type")
        if not isinstance(span, (list, tuple)) or len(span) != 2:
            raise ProfileError(f"{where}: 'range' must be [low, high]")
        span = (_number(span[0], where), _number(span[1], where))
        if span[0] >= span[1]:
            raise ProfileError(f"{where}: 'range' low must be below high")
    source = data.get("source")
    if source is not None:
        source = _mapping(source, f"{where} source")
        if source.get("kind", "constant") not in KINDS:
            raise ProfileError(f"{where}: unknown source kind {source.get('kind')!r}")
    low = None if data.get("min") is None else _number(data["min"], where)
    high = None if data.get("max") is None else _number(data["max"], where)
    return VariableSpec(name=name, type=vtype, description=str(data.get("description", "")),
                        unit=data.get("unit"), range=span, writable=bool(data.get("writable")),
                        min=low, max=high, model=data.get("model"), source=source,
                        value=data.get("value"), **_placement(data, where))


def _arguments(items: Any, where: str) -> list[ArgumentSpec]:
    result = []
    for item in items or []:
        item = _mapping(item, where)
        _unknown(item, {"name", "type", "description"}, where)
        atype = item.get("type", "Double")
        if atype not in TYPES:
            raise ProfileError(f"{where}: unknown argument type {atype!r}")
        result.append(ArgumentSpec(_name(item, where), atype, str(item.get("description", ""))))
    return result


def _method(data: Any, where: str) -> MethodSpec:
    data = _mapping(data, where)
    _unknown(data, {"name", "model", "description", "inputs", "outputs", "node_id", "path",
                    "browse_namespace"}, where)
    name = _name(data, where)
    if not isinstance(data.get("model"), str):
        raise ProfileError(f"{where} ({name}): 'model' (the model method) is required")
    return MethodSpec(name=name, model=data["model"], description=str(data.get("description", "")),
                      inputs=_arguments(data.get("inputs"), f"{where} ({name}) inputs"),
                      outputs=_arguments(data.get("outputs"), f"{where} ({name}) outputs"),
                      **_placement(data, f"{where} ({name})"))


def _alarm(data: Any, where: str, variables: dict[str, VariableSpec]) -> AlarmSpec:
    data = _mapping(data, where)
    _unknown(data, {"name", "kind", "message", "variable", "deadband", "when", "severity",
                    *LIMITS}, where)
    name = _name(data, where)
    where = f"{where} ({name})"
    kind = data.get("kind")
    message = str(data.get("message", name))
    if kind == "level":
        variable = data.get("variable")
        if variable not in variables:
            raise ProfileError(f"{where}: 'variable' must name a variable of this profile")
        if variables[variable].type not in NUMERIC:
            raise ProfileError(f"{where}: a level alarm needs a numeric variable")
        limits: dict[str, float] = {}
        severities = dict(DEFAULT_SEVERITY)
        for limit in LIMITS:
            raw = data.get(limit)
            if raw is None:
                continue
            if isinstance(raw, dict):
                _unknown(raw, {"value", "severity"}, f"{where} {limit}")
                severities[limit] = int(raw.get("severity", severities[limit]))
                raw = raw.get("value")
            limits[limit] = _number(raw, f"{where} {limit}")
        if not limits:
            raise ProfileError(f"{where}: a level alarm needs at least one limit "
                               f"({', '.join(LIMITS)})")
        ordered = [limits[limit] for limit in LIMITS if limit in limits]
        if ordered != sorted(ordered, reverse=True) or len(set(ordered)) != len(ordered):
            raise ProfileError(f"{where}: limits must fall from high_high to low_low")
        for limit, severity in severities.items():
            if limit in limits and not 1 <= severity <= 1000:
                raise ProfileError(f"{where}: severity must be 1-1000")
        return AlarmSpec(name=name, kind="level", message=message, variable=variable, limits=limits,
                         severities=severities,
                         deadband=_number(data.get("deadband", 0.0), f"{where} deadband"))
    if kind == "discrete":
        if not isinstance(data.get("when"), str):
            raise ProfileError(f"{where}: a discrete alarm needs 'when' (a model flag)")
        severity = int(data.get("severity", 700))
        if not 1 <= severity <= 1000:
            raise ProfileError(f"{where}: severity must be 1-1000")
        return AlarmSpec(name=name, kind="discrete", message=message, when=data["when"],
                         severity=severity)
    raise ProfileError(f"{where}: 'kind' must be level or discrete")


def parse_profile(data: Any, where: str = "profile") -> Profile:
    """Validate a profile mapping and return it as a :class:`Profile`."""
    data = _mapping(data, where)
    _unknown(data, {"name", "description", "identification", "model", "model_params", "variables",
                    "methods", "alarms", "address_space", "sources", "limits"}, where)
    name = data.get("name")
    if not isinstance(name, str) or not name:
        raise ProfileError(f"{where}: 'name' is required")
    identification = _mapping(data.get("identification") or {}, f"{where} identification")
    _unknown(identification, set(IDENTIFICATION), f"{where} identification")
    model = data.get("model")
    if model is not None and model not in MODELS:
        raise ProfileError(f"{where}: unknown model {model!r}, "
                           f"expected one of {', '.join(sorted(MODELS))}")
    model_params = _mapping(data.get("model_params") or {}, f"{where} model_params")
    if not isinstance(data.get("variables"), list) or not data["variables"]:
        raise ProfileError(f"{where}: 'variables' must be a non-empty list")
    variables = [_variable(item, f"{where} variable") for item in data["variables"]]
    by_name = {variable.name: variable for variable in variables}
    if len(by_name) != len(variables):
        raise ProfileError(f"{where}: duplicate variable names")
    methods = [_method(item, f"{where} method") for item in data.get("methods") or []]
    if len({method.name for method in methods}) != len(methods):
        raise ProfileError(f"{where}: duplicate method names")
    alarms = [_alarm(item, f"{where} alarm", by_name) for item in data.get("alarms") or []]
    if len({alarm.name for alarm in alarms}) != len(alarms):
        raise ProfileError(f"{where}: duplicate alarm names")
    # What a profile binds must exist in the model it names.
    model_class = MODELS.get(model) if model else None
    for variable in variables:
        if variable.model is None:
            continue
        if model_class is None:
            raise ProfileError(f"{where}: variable {variable.name} uses 'model' but the "
                               "profile has no model")
        if variable.model not in model_class.VALUES:
            raise ProfileError(f"{where}: model {model} has no value {variable.model!r}")
    for method in methods:
        if model_class is None or method.model not in model_class.METHODS:
            raise ProfileError(f"{where}: method {method.name}: no model method {method.model!r}")
    for alarm in alarms:
        if alarm.when is not None and (model_class is None or alarm.when not in model_class.FLAGS):
            raise ProfileError(f"{where}: alarm {alarm.name}: no model flag {alarm.when!r}")
    address_space = None
    if data.get("address_space") is not None:
        address_space = _address_space(data["address_space"], where)
        if alarms:
            raise ProfileError(f"{where}: a profile with an address_space cannot have alarms yet")
        seen: set[str | int] = set()
        for item in [*variables, *methods]:
            if item.node_id is None or not item.path:
                raise ProfileError(f"{where}: {item.name} needs 'node_id' and 'path'")
            if item.node_id in seen:
                raise ProfileError(f"{where}: duplicate node_id {item.node_id!r}")
            seen.add(item.node_id)
    else:
        for item in [*variables, *methods]:
            if item.node_id is not None or item.path or item.browse_namespace is not None:
                raise ProfileError(f"{where}: {item.name}: 'node_id', 'path' and "
                                   "'browse_namespace' need an address_space")
    return Profile(name=name, description=str(data.get("description", "")),
                   identification={key: str(value) for key, value in identification.items()},
                   model=model, model_params=model_params, variables=variables, methods=methods,
                   alarms=alarms, address_space=address_space,
                   sources=[str(item) for item in data.get("sources") or []],
                   limits=[str(item) for item in data.get("limits") or []])


def bundled_profiles() -> list[str]:
    """Names of the profiles shipped with the package."""
    folder = resources.files(__package__) / "profiles"
    return sorted(item.name[:-5] for item in folder.iterdir() if item.name.endswith(".yaml"))


def load_profile(source: str | Path) -> Profile:
    """Load a bundled profile by name, or a profile file by path."""
    if isinstance(source, str) and source in bundled_profiles():
        text = (resources.files(__package__) / "profiles" / f"{source}.yaml").read_text("utf-8")
        where = f"profile {source}"
    else:
        path = Path(source)
        if not path.is_file():
            raise ProfileError(f"no such profile: {source} (bundled: "
                               f"{', '.join(bundled_profiles())})")
        text = path.read_text("utf-8")
        where = str(path)
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise ProfileError(f"{where}: invalid YAML: {error}") from error
    return parse_profile(data, where)

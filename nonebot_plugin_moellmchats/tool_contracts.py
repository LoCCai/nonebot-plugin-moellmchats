from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
import re
from types import MappingProxyType
from typing import Any

_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_JSON_TYPES = {"array", "boolean", "integer", "null", "number", "object", "string"}
_CAPABILITY_FIELDS = frozenset(
    {"network", "process", "workspace", "host_filesystem", "secrets"}
)


class ToolEffect(str, Enum):
    READ_ONLY = "read_only"
    MUTATING = "mutating"


@dataclass(frozen=True)
class ToolCapability:
    """A small, deny-by-default capability set for isolated tools.

    ``workspace`` remains enabled by default because generated tools receive a
    private, bounded working directory. Network, child-process, host filesystem,
    and secret access must be requested and approved explicitly. ``secrets`` is
    reserved for a future broker; the current runner never injects host secrets.
    """

    network: bool = False
    process: bool = False
    workspace: bool = True
    host_filesystem: bool = False
    secrets: bool = False

    def __post_init__(self) -> None:
        for name in _CAPABILITY_FIELDS:
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"工具 capability.{name} 必须是布尔值")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> ToolCapability:
        """Parse a manifest/config mapping without accepting unknown rights."""

        if value is None:
            return cls()
        if not isinstance(value, Mapping):
            raise ValueError("工具 capabilities 必须是映射")
        unknown = [key for key in value if key not in _CAPABILITY_FIELDS]
        if unknown:
            raise ValueError(
                f"工具 capabilities 包含未知字段: {sorted(map(str, unknown))}"
            )
        return cls(**dict(value))

    def restrict(self, ceiling: ToolCapability) -> ToolCapability:
        """Return the strict intersection of requested and administrative rights."""

        if not isinstance(ceiling, ToolCapability):
            raise ValueError("工具 admin capabilities 必须是 ToolCapability")
        return ToolCapability(
            network=self.network and ceiling.network,
            process=self.process and ceiling.process,
            workspace=self.workspace and ceiling.workspace,
            host_filesystem=(
                self.host_filesystem and ceiling.host_filesystem
            ),
            secrets=self.secrets and ceiling.secrets,
        )

    def as_dict(self) -> dict[str, bool]:
        return {
            "network": self.network,
            "process": self.process,
            "workspace": self.workspace,
            "host_filesystem": self.host_filesystem,
            "secrets": self.secrets,
        }


@dataclass(frozen=True)
class ToolPolicy:
    """Requested rights bounded by an administrator-controlled ceiling.

    ``effective`` is derived internally so a manifest cannot provide or widen
    it. ``ToolPolicy.generated()`` is the safe default for generated tools:
    private workspace access only, with network and process access denied.
    """

    requested: ToolCapability = field(default_factory=ToolCapability)
    admin: ToolCapability = field(default_factory=ToolCapability)
    effective: ToolCapability = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.requested, ToolCapability):
            raise ValueError("工具 requested capabilities 必须是 ToolCapability")
        if not isinstance(self.admin, ToolCapability):
            raise ValueError("工具 admin capabilities 必须是 ToolCapability")
        object.__setattr__(self, "effective", self.requested.restrict(self.admin))

    @classmethod
    def generated(
        cls,
        requested: ToolCapability | Mapping[str, Any] | None = None,
        *,
        admin: ToolCapability | Mapping[str, Any] | None = None,
    ) -> ToolPolicy:
        """Build a generated-tool policy with a conservative admin ceiling."""

        requested_value = (
            requested
            if isinstance(requested, ToolCapability)
            else ToolCapability.from_mapping(requested)
        )
        admin_value = (
            admin
            if isinstance(admin, ToolCapability)
            else ToolCapability.from_mapping(admin)
        )
        return cls(requested=requested_value, admin=admin_value)


@dataclass(frozen=True)
class ToolContext:
    bot: Any
    event: Any
    request_id: int | None = None
    confirmed: bool = False


@dataclass(frozen=True)
class ToolResult:
    text: str = ""
    images: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


ToolHandler = Callable[..., Awaitable[Any] | Any]


def _freeze_schema_value(value: Any) -> Any:
    """Detach and recursively freeze the JSON values owned by a ToolSpec."""

    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("工具 parameters 字段名必须是字符串")
        return MappingProxyType(
            {key: _freeze_schema_value(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_schema_value(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError(
        f"工具 parameters 包含不可序列化类型: {type(value).__name__}"
    )


def _mutable_schema_value(value: Any) -> Any:
    """Return a detached JSON-compatible copy for model and legacy payloads."""

    if isinstance(value, Mapping):
        return {key: _mutable_schema_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_mutable_schema_value(item) for item in value]
    return value


def _schema_types(schema: Mapping[str, Any], path: str) -> tuple[str, ...]:
    raw_type = schema.get("type")
    if raw_type is None:
        return ()
    values = (raw_type,) if isinstance(raw_type, str) else raw_type
    if not isinstance(values, (list, tuple)) or not values or not all(
        isinstance(item, str) and item in _JSON_TYPES for item in values
    ):
        raise ValueError(f"{path}.type 不是受支持的 JSON Schema 类型")
    return tuple(values)


def _validate_schema_node(schema: Any, path: str) -> None:
    if not isinstance(schema, Mapping):
        raise ValueError(f"{path} 必须是 JSON Schema 映射")
    schema_types = _schema_types(schema, path)
    properties = schema.get("properties")
    if properties is not None:
        if not isinstance(properties, Mapping):
            raise ValueError(f"{path}.properties 必须是映射")
        for name, child in properties.items():
            if not isinstance(name, str) or not name:
                raise ValueError(f"{path}.properties 字段名必须是非空字符串")
            _validate_schema_node(child, f"{path}.properties.{name}")
    required = schema.get("required", [])
    if not isinstance(required, (list, tuple)) or not all(
        isinstance(item, str) and properties is not None and item in properties
        for item in required
    ):
        raise ValueError(f"{path}.required 必须引用已声明的字段")
    if len(set(required)) != len(required):
        raise ValueError(f"{path}.required 不得包含重复字段")
    if "object" in schema_types and properties is None:
        raise ValueError(f"{path}.properties 必须是映射")
    items = schema.get("items")
    if items is not None:
        _validate_schema_node(items, f"{path}.items")
    additional = schema.get("additionalProperties", True)
    if not isinstance(additional, (bool, Mapping)):
        raise ValueError(f"{path}.additionalProperties 必须是布尔值或 Schema")
    if isinstance(additional, Mapping):
        _validate_schema_node(additional, f"{path}.additionalProperties")
    enum = schema.get("enum")
    if enum is not None and (not isinstance(enum, (list, tuple)) or not enum):
        raise ValueError(f"{path}.enum 必须是非空数组")


def validate_parameters_schema(parameters: Any) -> None:
    if not isinstance(parameters, Mapping):
        raise ValueError("工具 parameters 必须是 JSON Schema 映射")
    if parameters.get("type") != "object":
        raise ValueError("工具 parameters.type 必须是 object")
    _validate_schema_node(parameters, "工具 parameters")


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "null":
        return value is None
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "string":
        return isinstance(value, str)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return False


def _validate_value(value: Any, schema: Mapping[str, Any], path: str) -> str | None:
    try:
        expected_types = _schema_types(schema, path)
    except ValueError as error:
        return str(error)
    if expected_types and not any(_matches_type(value, item) for item in expected_types):
        return f"{path} 类型错误，应为 {' 或 '.join(expected_types)}"
    enum = schema.get("enum")
    if enum is not None and value not in enum:
        return f"{path} 不在允许值范围内"
    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        for name in schema.get("required") or ():
            if name not in value:
                return f"缺少必填参数: {path}.{name}" if path else f"缺少必填参数: {name}"
        additional = schema.get("additionalProperties", True)
        for name, child in value.items():
            child_schema = properties.get(name)
            if child_schema is None:
                if additional is False:
                    return f"{path}.{name} 是未声明参数" if path else f"未声明参数: {name}"
                child_schema = additional if isinstance(additional, Mapping) else None
            if child_schema is not None:
                error = _validate_value(
                    child,
                    child_schema,
                    f"{path}.{name}" if path else name,
                )
                if error:
                    return error
    if isinstance(value, list) and isinstance(schema.get("items"), Mapping):
        for index, item in enumerate(value):
            error = _validate_value(item, schema["items"], f"{path}[{index}]")
            if error:
                return error
    return None


def validate_tool_arguments(arguments: Any, parameters: Mapping[str, Any] | None) -> str | None:
    if not isinstance(arguments, dict):
        return "工具参数必须是 JSON 对象"
    if not parameters:
        return None
    return _validate_value(arguments, parameters, "")


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: Mapping[str, Any]
    handler: ToolHandler
    effect: ToolEffect = ToolEffect.READ_ONLY
    permission: str = "user"
    timeout_seconds: float | None = None
    result_limit: int | None = None
    dependencies: tuple[str, ...] = ()
    policy: ToolPolicy | None = None

    def __post_init__(self) -> None:
        if not _TOOL_NAME_RE.fullmatch(self.name):
            raise ValueError("工具名必须是 1 到 64 个字母、数字、下划线或连字符")
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError("工具描述不能为空")
        if not isinstance(self.effect, ToolEffect):
            raise ValueError("工具 effect 仅支持 read_only 或 mutating")
        if self.permission not in {"user", "superuser"}:
            raise ValueError("工具 permission 仅支持 user 或 superuser")
        frozen_parameters = _freeze_schema_value(self.parameters)
        validate_parameters_schema(frozen_parameters)
        object.__setattr__(self, "parameters", frozen_parameters)
        if not callable(self.handler):
            raise ValueError("工具 handler 必须可调用")
        if self.timeout_seconds is not None and (
            not isinstance(self.timeout_seconds, (int, float))
            or isinstance(self.timeout_seconds, bool)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("工具 timeout_seconds 必须大于 0")
        if self.result_limit is not None and (
            not isinstance(self.result_limit, int)
            or isinstance(self.result_limit, bool)
            or self.result_limit <= 0
        ):
            raise ValueError("工具 result_limit 必须大于 0")
        if not isinstance(self.dependencies, tuple) or not all(
            isinstance(item, str) and _TOOL_NAME_RE.fullmatch(item)
            for item in self.dependencies
        ):
            raise ValueError("工具 dependencies 必须是安全工具名元组")
        if len(set(self.dependencies)) != len(self.dependencies):
            raise ValueError("工具 dependencies 不得重复")
        if self.policy is not None and not isinstance(self.policy, ToolPolicy):
            raise ValueError("工具 policy 必须是 ToolPolicy")

    def as_legacy_schema(self) -> dict[str, Any]:
        parameters = _mutable_schema_value(self.parameters)
        parameters["properties"] = dict(parameters.get("properties") or {})
        parameters["required"] = list(parameters.get("required") or [])
        return {
            "name": self.name,
            "description": self.description,
            "parameters": parameters,
            "func": self.handler,
            "tool_spec": self,
        }

    def __deepcopy__(self, _memo: dict[int, Any]) -> ToolSpec:
        return self


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec, *, replace: bool = False) -> ToolSpec:
        if spec.name in self._tools and not replace:
            raise ValueError(f"工具已注册: {spec.name}")
        self._tools[spec.name] = spec
        return spec

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def snapshot(self) -> dict[str, ToolSpec]:
        return dict(self._tools)


tool_registry = ToolRegistry()


def register_tool(spec: ToolSpec, *, replace: bool = False) -> ToolSpec:
    """Register a first-class MoEllm tool without synthesizing a NoneBot event."""
    return tool_registry.register(spec, replace=replace)

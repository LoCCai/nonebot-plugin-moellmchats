from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
import re
from types import MappingProxyType
from typing import Any

_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_JSON_TYPES = {"array", "boolean", "integer", "null", "number", "object", "string"}
_CAPABILITY_FIELDS = frozenset(
    {"network", "process", "workspace", "host_filesystem", "secrets"}
)
_CAPABILITY_V2_FIELDS = frozenset(
    {"network", "process", "filesystem", "database", "bot", "secrets"}
)
_NETWORK_TARGET_RE = re.compile(
    r"^(?:\*|(?:\*\.)?(?=.{1,253}$)"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)$"
)
_SECRET_NAME_RE = re.compile(r"^(?:\*|[A-Za-z_][A-Za-z0-9_]{0,127})$")

CAPABILITY_SCHEMA_VERSION = 2
CAPABILITY_DETECTOR_VERSION = 1


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

    @classmethod
    def none(cls) -> ToolCapability:
        """Return an empty set for detector evidence.

        The normal constructor intentionally keeps the historical private
        workspace default. Static analysis evidence must not inherit that
        operational default, otherwise an unobserved capability would appear
        to have been detected.
        """

        return cls(workspace=False)

    def union(self, other: ToolCapability) -> ToolCapability:
        if not isinstance(other, ToolCapability):
            raise ValueError("工具 capabilities union 必须是 ToolCapability")
        return ToolCapability(
            network=self.network or other.network,
            process=self.process or other.process,
            workspace=self.workspace or other.workspace,
            host_filesystem=(
                self.host_filesystem or other.host_filesystem
            ),
            secrets=self.secrets or other.secrets,
        )

    def is_subset_of(self, ceiling: ToolCapability) -> bool:
        if not isinstance(ceiling, ToolCapability):
            raise ValueError("工具 capability ceiling 必须是 ToolCapability")
        return all(
            not getattr(self, name) or getattr(ceiling, name)
            for name in _CAPABILITY_FIELDS
        )

    def missing_from(self, ceiling: ToolCapability) -> tuple[str, ...]:
        if not isinstance(ceiling, ToolCapability):
            raise ValueError("工具 capability ceiling 必须是 ToolCapability")
        return tuple(
            sorted(
                name
                for name in _CAPABILITY_FIELDS
                if getattr(self, name) and not getattr(ceiling, name)
            )
        )

    def as_dict(self) -> dict[str, bool]:
        return {
            "network": self.network,
            "process": self.process,
            "workspace": self.workspace,
            "host_filesystem": self.host_filesystem,
            "secrets": self.secrets,
        }


def _normalize_capability_names(
    value: Any,
    *,
    label: str,
    pattern: re.Pattern[str],
) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(item, str) and pattern.fullmatch(item) for item in value
    ):
        raise ValueError(f"工具 {label} 必须是安全字符串数组")
    if len(set(value)) != len(value):
        raise ValueError(f"工具 {label} 不得包含重复项")
    if "*" in value and len(value) != 1:
        raise ValueError(f"工具 {label} 的通配符不得与其他项混用")
    return tuple(sorted(value))


def _bool_access(value: Any, *, label: str) -> tuple[bool, bool]:
    if type(value) is bool:
        return value, value
    if not isinstance(value, Mapping) or set(value) - {"read", "write"}:
        raise ValueError(f"工具 {label} 必须是布尔值或 read/write 映射")
    read = value.get("read", False)
    write = value.get("write", False)
    if type(read) is not bool or type(write) is not bool:
        raise ValueError(f"工具 {label}.read/write 必须是布尔值")
    if write and not read:
        raise ValueError(f"工具 {label}.write 不能在 read=false 时启用")
    return read, write


def _intersect_allowlists(
    requested: tuple[str, ...],
    ceiling: tuple[str, ...],
) -> tuple[str, ...]:
    if not requested or not ceiling:
        return ()
    if requested == ("*",):
        return ceiling
    if ceiling == ("*",):
        return requested
    return tuple(sorted(set(requested) & set(ceiling)))


@dataclass(frozen=True)
class ToolCapabilityV2:
    """Structured, deny-by-default capability profile.

    Version 2 can express network and secret allow-lists, separate workspace
    and host read/write access, and reserved database/bot rights. The current
    isolated runner still accepts only profiles that have an exact legacy-v1
    projection; scoped or new rights remain fail closed until their consumer is
    migrated in D-08.
    """

    network_allow: tuple[str, ...] = ()
    process: bool = False
    workspace_read: bool = False
    workspace_write: bool = False
    host_filesystem_read: bool = False
    host_filesystem_write: bool = False
    database_read: bool = False
    database_write: bool = False
    bot_read: bool = False
    bot_send: bool = False
    bot_manage: bool = False
    secret_names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "process",
            "workspace_read",
            "workspace_write",
            "host_filesystem_read",
            "host_filesystem_write",
            "database_read",
            "database_write",
            "bot_read",
            "bot_send",
            "bot_manage",
        ):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"工具 capability v2.{name} 必须是布尔值")
        network_allow = _normalize_capability_names(
            self.network_allow,
            label="capability v2 network.allow",
            pattern=_NETWORK_TARGET_RE,
        )
        secret_names = _normalize_capability_names(
            self.secret_names,
            label="capability v2 secrets.allow",
            pattern=_SECRET_NAME_RE,
        )
        if self.workspace_write and not self.workspace_read:
            raise ValueError("工具 capability v2 workspace.write 需要 read")
        if self.host_filesystem_write and not self.host_filesystem_read:
            raise ValueError("工具 capability v2 host filesystem.write 需要 read")
        if self.database_write and not self.database_read:
            raise ValueError("工具 capability v2 database.write 需要 read")
        if self.bot_manage and not self.bot_send:
            raise ValueError("工具 capability v2 bot.manage 需要 send")
        if self.bot_send and not self.bot_read:
            raise ValueError("工具 capability v2 bot.send 需要 read")
        object.__setattr__(self, "network_allow", network_allow)
        object.__setattr__(self, "secret_names", secret_names)

    @classmethod
    def from_legacy(cls, value: ToolCapability) -> ToolCapabilityV2:
        if not isinstance(value, ToolCapability):
            raise ValueError("工具 legacy capability 必须是 ToolCapability")
        return cls(
            network_allow=("*",) if value.network else (),
            process=value.process,
            workspace_read=value.workspace,
            workspace_write=value.workspace,
            host_filesystem_read=value.host_filesystem,
            host_filesystem_write=value.host_filesystem,
            secret_names=("*",) if value.secrets else (),
        )

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any] | None,
    ) -> ToolCapabilityV2:
        """Parse either the strict legacy mapping or the structured v2 form."""

        if value is None:
            return cls.from_legacy(ToolCapability())
        if not isinstance(value, Mapping):
            raise ValueError("工具 capabilities 必须是映射")
        if "workspace" in value or "host_filesystem" in value:
            if "filesystem" in value or "database" in value or "bot" in value:
                raise ValueError("工具 capabilities 不得混用 v1 与 v2 filesystem 字段")
            return cls.from_legacy(ToolCapability.from_mapping(value))
        unknown = set(value) - _CAPABILITY_V2_FIELDS
        if unknown:
            raise ValueError(
                f"工具 capabilities 包含未知字段: {sorted(map(str, unknown))}"
            )

        network = value.get("network", False)
        if type(network) is bool:
            network_allow = ("*",) if network else ()
        elif isinstance(network, Mapping) and set(network) == {"allow"}:
            network_allow = _normalize_capability_names(
                network["allow"],
                label="capability network.allow",
                pattern=_NETWORK_TARGET_RE,
            )
        else:
            raise ValueError("工具 capability network 必须是布尔值或 allow 映射")

        process = value.get("process", False)
        if type(process) is not bool:
            raise ValueError("工具 capability process 必须是布尔值")

        filesystem = value.get("filesystem")
        if filesystem is None:
            workspace_read = True
            workspace_write = True
            host_read = False
            host_write = False
        else:
            if not isinstance(filesystem, Mapping) or set(filesystem) - {
                "workspace",
                "host",
            }:
                raise ValueError("工具 capability filesystem 字段非法")
            workspace_read, workspace_write = _bool_access(
                filesystem.get("workspace", False),
                label="capability filesystem.workspace",
            )
            host_read, host_write = _bool_access(
                filesystem.get("host", False),
                label="capability filesystem.host",
            )

        database_read, database_write = _bool_access(
            value.get("database", False),
            label="capability database",
        )
        bot = value.get("bot", False)
        if type(bot) is bool:
            bot_read = bot
            bot_send = bot
            bot_manage = bot
        elif isinstance(bot, Mapping) and not set(bot) - {
            "read",
            "send",
            "manage",
        }:
            bot_read = bot.get("read", False)
            bot_send = bot.get("send", False)
            bot_manage = bot.get("manage", False)
            if not all(type(item) is bool for item in (bot_read, bot_send, bot_manage)):
                raise ValueError("工具 capability bot 字段必须是布尔值")
        else:
            raise ValueError("工具 capability bot 必须是布尔值或权限映射")

        secrets = value.get("secrets", False)
        if type(secrets) is bool:
            secret_names = ("*",) if secrets else ()
        elif isinstance(secrets, Mapping) and set(secrets) == {"allow"}:
            secret_names = _normalize_capability_names(
                secrets["allow"],
                label="capability secrets.allow",
                pattern=_SECRET_NAME_RE,
            )
        else:
            raise ValueError("工具 capability secrets 必须是布尔值或 allow 映射")

        return cls(
            network_allow=network_allow,
            process=process,
            workspace_read=workspace_read,
            workspace_write=workspace_write,
            host_filesystem_read=host_read,
            host_filesystem_write=host_write,
            database_read=database_read,
            database_write=database_write,
            bot_read=bot_read,
            bot_send=bot_send,
            bot_manage=bot_manage,
            secret_names=secret_names,
        )

    def restrict(self, ceiling: ToolCapabilityV2) -> ToolCapabilityV2:
        if not isinstance(ceiling, ToolCapabilityV2):
            raise ValueError("工具 admin capability v2 必须是 ToolCapabilityV2")
        return ToolCapabilityV2(
            network_allow=_intersect_allowlists(
                self.network_allow,
                ceiling.network_allow,
            ),
            process=self.process and ceiling.process,
            workspace_read=self.workspace_read and ceiling.workspace_read,
            workspace_write=self.workspace_write and ceiling.workspace_write,
            host_filesystem_read=(
                self.host_filesystem_read and ceiling.host_filesystem_read
            ),
            host_filesystem_write=(
                self.host_filesystem_write and ceiling.host_filesystem_write
            ),
            database_read=self.database_read and ceiling.database_read,
            database_write=self.database_write and ceiling.database_write,
            bot_read=self.bot_read and ceiling.bot_read,
            bot_send=self.bot_send and ceiling.bot_send,
            bot_manage=self.bot_manage and ceiling.bot_manage,
            secret_names=_intersect_allowlists(
                self.secret_names,
                ceiling.secret_names,
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "network": {"allow": list(self.network_allow)},
            "process": self.process,
            "filesystem": {
                "workspace": {
                    "read": self.workspace_read,
                    "write": self.workspace_write,
                },
                "host": {
                    "read": self.host_filesystem_read,
                    "write": self.host_filesystem_write,
                },
            },
            "database": {
                "read": self.database_read,
                "write": self.database_write,
            },
            "bot": {
                "read": self.bot_read,
                "send": self.bot_send,
                "manage": self.bot_manage,
            },
            "secrets": {"allow": list(self.secret_names)},
        }

    def to_legacy(self) -> ToolCapability:
        return ToolCapability(
            network=bool(self.network_allow),
            process=self.process,
            workspace=self.workspace_write,
            host_filesystem=(
                self.host_filesystem_read or self.host_filesystem_write
            ),
            secrets=bool(self.secret_names),
        )

    @property
    def legacy_runner_compatible(self) -> bool:
        return (
            self.network_allow in {(), ("*",)}
            and self.workspace_read == self.workspace_write
            and self.host_filesystem_read == self.host_filesystem_write
            and not self.database_read
            and not self.database_write
            and not self.bot_read
            and not self.bot_send
            and not self.bot_manage
            and self.secret_names in {(), ("*",)}
        )


def parse_capability_profile(
    value: ToolCapability | ToolCapabilityV2 | Mapping[str, Any] | None,
) -> tuple[ToolCapability, ToolCapabilityV2]:
    if isinstance(value, ToolCapability):
        return value, ToolCapabilityV2.from_legacy(value)
    if isinstance(value, ToolCapabilityV2):
        return value.to_legacy(), value
    structured = ToolCapabilityV2.from_mapping(value)
    return structured.to_legacy(), structured


@dataclass(frozen=True)
class ToolPolicy:
    """Requested rights bounded by an administrator-controlled ceiling.

    ``effective`` is derived internally so a manifest cannot provide or widen
    it. ``ToolPolicy.generated()`` is the safe default for generated tools:
    private workspace access only, with network and process access denied.
    """

    requested: ToolCapability = field(default_factory=ToolCapability)
    admin: ToolCapability = field(default_factory=ToolCapability)
    detected: ToolCapability = field(default_factory=ToolCapability.none)
    requested_v2: ToolCapabilityV2 | None = None
    admin_v2: ToolCapabilityV2 | None = None
    effective: ToolCapability = field(init=False)
    effective_v2: ToolCapabilityV2 = field(init=False)

    capability_schema_version = CAPABILITY_SCHEMA_VERSION
    detector_version = CAPABILITY_DETECTOR_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.requested, ToolCapability):
            raise ValueError("工具 requested capabilities 必须是 ToolCapability")
        if not isinstance(self.admin, ToolCapability):
            raise ValueError("工具 admin capabilities 必须是 ToolCapability")
        if not isinstance(self.detected, ToolCapability):
            raise ValueError("工具 detected capabilities 必须是 ToolCapability")
        requested_v2 = (
            ToolCapabilityV2.from_legacy(self.requested)
            if self.requested_v2 is None
            else self.requested_v2
        )
        admin_v2 = (
            ToolCapabilityV2.from_legacy(self.admin)
            if self.admin_v2 is None
            else self.admin_v2
        )
        if not isinstance(requested_v2, ToolCapabilityV2):
            raise ValueError("工具 requested capability v2 必须是 ToolCapabilityV2")
        if not isinstance(admin_v2, ToolCapabilityV2):
            raise ValueError("工具 admin capability v2 必须是 ToolCapabilityV2")
        if requested_v2.to_legacy() != self.requested:
            raise ValueError("工具 requested v1/v2 capability 投影不一致")
        if admin_v2.to_legacy() != self.admin:
            raise ValueError("工具 admin v1/v2 capability 投影不一致")
        effective_v2 = requested_v2.restrict(admin_v2)
        effective = effective_v2.to_legacy()
        missing = self.detected.missing_from(effective)
        if missing:
            raise ValueError(
                "工具 detected capabilities 未同时获得 requested/admin 授权: "
                f"{list(missing)}"
            )
        object.__setattr__(self, "requested_v2", requested_v2)
        object.__setattr__(self, "admin_v2", admin_v2)
        object.__setattr__(self, "effective_v2", effective_v2)
        object.__setattr__(self, "effective", effective)

    @classmethod
    def generated(
        cls,
        requested: (
            ToolCapability | ToolCapabilityV2 | Mapping[str, Any] | None
        ) = None,
        *,
        admin: (
            ToolCapability | ToolCapabilityV2 | Mapping[str, Any] | None
        ) = None,
    ) -> ToolPolicy:
        """Build a generated-tool policy with a conservative admin ceiling."""

        requested_value, requested_v2 = parse_capability_profile(requested)
        admin_value, admin_v2 = parse_capability_profile(admin)
        return cls(
            requested=requested_value,
            admin=admin_value,
            requested_v2=requested_v2,
            admin_v2=admin_v2,
        )

    @classmethod
    def configured(
        cls,
        requested: (
            ToolCapability | ToolCapabilityV2 | Mapping[str, Any] | None
        ) = None,
    ) -> ToolPolicy:
        """Use one administrator-maintained profile as request and ceiling."""

        requested_value, requested_v2 = parse_capability_profile(requested)
        return cls(
            requested=requested_value,
            admin=requested_value,
            requested_v2=requested_v2,
            admin_v2=requested_v2,
        )

    def with_detected(self, detected: ToolCapability) -> ToolPolicy:
        """Bind static evidence without allowing it to grant a capability."""

        if not isinstance(detected, ToolCapability):
            raise ValueError("工具 detected capabilities 必须是 ToolCapability")
        return replace(self, detected=detected)

    def capability_contract(self) -> dict[str, Any]:
        assert self.requested_v2 is not None
        assert self.admin_v2 is not None
        return {
            "schema_version": self.capability_schema_version,
            "detector_version": self.detector_version,
            "requested": self.requested_v2.as_dict(),
            "detected": self.detected.as_dict(),
            "admin": self.admin_v2.as_dict(),
            "effective": self.effective_v2.as_dict(),
        }


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

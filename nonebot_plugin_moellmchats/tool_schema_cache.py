from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable, Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
import re
from typing import Any, Protocol, runtime_checkable

from .tool_catalog_cache import ToolCatalogPermission, ToolCatalogRenderContext

_POSTGRES_BIGINT_MAX = (1 << 63) - 1
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_SELECTED_PLUGINS = 4_096
_MAX_TOOL_NAME_CHARS = 512
_MAX_SELECTED_PAYLOAD_BYTES = 1_048_576
_MAX_SCHEMA_TOOLS = 4_096
_MAX_SCHEMA_BYTES = 16_777_216
_MAX_SCHEMA_DEPTH = 64
_MAX_SCHEMA_NODES = 131_072
_MAX_RECORD_BYTES = _MAX_SELECTED_PAYLOAD_BYTES + _MAX_SCHEMA_BYTES

SchemaBuilder = Callable[[], "ToolSchemaRecord"]
LoopProvider = Callable[[], asyncio.AbstractEventLoop]
PidProvider = Callable[[], int]


class ToolSchemaCacheError(RuntimeError):
    """Base error for a replaceable, generation-bound tool schema cache."""


class ToolSchemaCacheUnavailableError(ToolSchemaCacheError):
    """The cache could not establish a trustworthy result."""


class ToolSchemaCacheConflictError(ToolSchemaCacheError):
    """The same complete cache identity produced different schema values."""


class ToolSchemaCacheOwnershipError(ToolSchemaCacheError):
    """A process-local cache was reused by another process or event loop."""


def _validate_generation(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= _POSTGRES_BIGINT_MAX:
        raise ValueError("tool schema generation 必须是非负 BIGINT 整数")
    return value


def _validate_positive_integer(
    value: object,
    *,
    label: str,
    maximum: int,
) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= maximum:
        raise ValueError(f"{label} 必须是 1 到 {maximum} 的整数")
    return value


def _validate_tool_name(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value or len(value) > _MAX_TOOL_NAME_CHARS:
        raise ValueError(f"{label} 必须是非空安全工具名")
    return value


def _canonical_tool_names(
    values: tuple[str, ...],
    *,
    label: str,
) -> tuple[tuple[str, ...], str, int]:
    if not isinstance(values, tuple):
        raise TypeError(f"{label} 必须是元组")
    if len(values) > _MAX_SELECTED_PLUGINS:
        raise ValueError(f"{label} 数量超过安全上限")
    normalized = tuple(sorted({_validate_tool_name(value, label=label) for value in values}))
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > _MAX_SELECTED_PAYLOAD_BYTES:
        raise ValueError(f"{label} payload 超过安全上限")
    return normalized, hashlib.sha256(encoded).hexdigest(), len(encoded)


def _validate_json_value(
    value: Any,
    *,
    path: str,
    depth: int,
    nodes: list[int],
) -> None:
    if depth > _MAX_SCHEMA_DEPTH:
        raise ValueError("tool schema JSON 嵌套超过安全上限")
    nodes[0] += 1
    if nodes[0] > _MAX_SCHEMA_NODES:
        raise ValueError("tool schema JSON 节点超过安全上限")
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} 包含非有限数字")
        return
    if isinstance(value, str):
        if "\x00" in value:
            raise ValueError(f"{path} 包含 NUL")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str) or not key or "\x00" in key:
                raise ValueError(f"{path} 包含非法字段名")
            _validate_json_value(
                item,
                path=f"{path}.{key}",
                depth=depth + 1,
                nodes=nodes,
            )
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _validate_json_value(
                item,
                path=f"{path}[{index}]",
                depth=depth + 1,
                nodes=nodes,
            )
        return
    raise ValueError(f"{path} 包含不可序列化类型: {type(value).__name__}")


def _validate_schema_payload(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError("tool schema payload 顶层必须是数组")
    if len(value) > _MAX_SCHEMA_TOOLS:
        raise ValueError("tool schema 工具数量超过安全上限")
    _validate_json_value(value, path="tool schema", depth=0, nodes=[0])

    names: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping) or item.get("type") != "function":
            raise ValueError(f"tool schema[{index}] 必须是 function 对象")
        function = item.get("function")
        if not isinstance(function, Mapping):
            raise ValueError(f"tool schema[{index}].function 必须是对象")
        name = _validate_tool_name(
            function.get("name"),
            label=f"tool schema[{index}].function.name",
        )
        description = function.get("description")
        if not isinstance(description, str) or not description.strip() or "\x00" in description:
            raise ValueError(f"tool schema[{index}].function.description 必须是非空安全字符串")
        parameters = function.get("parameters")
        if not isinstance(parameters, Mapping) or parameters.get("type") != "object":
            raise ValueError(f"tool schema[{index}].function.parameters 必须是 object Schema")
        names.append(name)
    if len(set(names)) != len(names):
        raise ValueError("tool schema 不得包含重复工具名")
    return tuple(names)


def _canonical_schema_json(schema: list[dict[str, Any]]) -> str:
    _validate_schema_payload(schema)
    try:
        payload = json.dumps(
            schema,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise ValueError(f"tool schema 无法规范化 ({type(error).__name__})") from None
    if len(payload.encode("utf-8")) > _MAX_SCHEMA_BYTES:
        raise ValueError("tool schema payload 超过绝对安全上限")
    return payload


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("tool schema JSON 包含重复字段")
        value[key] = item
    return value


def _decode_canonical_schema(payload: str) -> tuple[list[dict[str, Any]], tuple[str, ...]]:
    if not isinstance(payload, str) or "\x00" in payload:
        raise ValueError("ToolSchemaRecord.schema_json 必须是安全 JSON 字符串")
    encoded = payload.encode("utf-8")
    if len(encoded) > _MAX_SCHEMA_BYTES:
        raise ValueError("ToolSchemaRecord.schema_json 超过绝对安全上限")
    try:
        value = json.loads(payload, object_pairs_hook=_reject_duplicate_pairs)
    except (json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"ToolSchemaRecord.schema_json 非法 ({type(error).__name__})") from None
    names = _validate_schema_payload(value)
    canonical = _canonical_schema_json(value)
    if canonical != payload:
        raise ValueError("ToolSchemaRecord.schema_json 必须是 canonical JSON")
    return value, names


@dataclass(frozen=True)
class ToolSchemaCacheKey:
    """Complete identity for one generation-bound LLM tool schema view."""

    generation: int
    permission: ToolCatalogPermission
    provider_cutover: bool
    tools_enabled: bool
    search_enabled: bool
    blacklist_digest: str
    selected_plugins_digest: str

    def __post_init__(self) -> None:
        _validate_generation(self.generation)
        if not isinstance(self.permission, ToolCatalogPermission):
            raise TypeError("tool schema permission 必须是受支持的权限级别")
        for field_name in (
            "provider_cutover",
            "tools_enabled",
            "search_enabled",
        ):
            if type(getattr(self, field_name)) is not bool:
                raise TypeError(f"tool schema {field_name} 必须是布尔值")
        for field_name in ("blacklist_digest", "selected_plugins_digest"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
                raise ValueError(f"tool schema {field_name} 必须是 SHA-256")

    @property
    def toolset_hash(self) -> str:
        payload = {
            "blacklist_digest": self.blacklist_digest,
            "permission": self.permission.value,
            "provider_cutover": self.provider_cutover,
            "search_enabled": self.search_enabled,
            "selected_plugins_digest": self.selected_plugins_digest,
            "tools_enabled": self.tools_enabled,
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        return hashlib.sha256(encoded).hexdigest()

    @property
    def safe_cache_key(self) -> str:
        return f"schema:{self.generation}:{self.toolset_hash}"


@dataclass(frozen=True)
class ToolSchemaRenderContext:
    """Explicit request and policy inputs used to build one schema record."""

    generation: int
    permission: ToolCatalogPermission
    provider_cutover: bool
    tools_enabled: bool
    search_enabled: bool
    selected_plugins: tuple[str, ...] = field(repr=False)
    blacklist_patterns: tuple[str, ...] = field(repr=False)
    selected_plugins_digest: str = field(init=False)
    blacklist_digest: str = field(init=False)

    def __post_init__(self) -> None:
        _validate_generation(self.generation)
        if not isinstance(self.permission, ToolCatalogPermission):
            raise TypeError("tool schema permission 必须是受支持的权限级别")
        for field_name in (
            "provider_cutover",
            "tools_enabled",
            "search_enabled",
        ):
            if type(getattr(self, field_name)) is not bool:
                raise TypeError(f"tool schema {field_name} 必须是布尔值")

        selected, selected_digest, _selected_bytes = _canonical_tool_names(
            self.selected_plugins,
            label="tool schema selected_plugins",
        )
        policy = ToolCatalogRenderContext(
            generation=self.generation,
            permission=self.permission,
            provider_cutover=self.provider_cutover,
            tools_enabled=self.tools_enabled,
            web_search_enabled=self.search_enabled,
            blacklist_patterns=self.blacklist_patterns,
        )
        object.__setattr__(self, "selected_plugins", selected)
        object.__setattr__(self, "selected_plugins_digest", selected_digest)
        object.__setattr__(self, "blacklist_patterns", policy.blacklist_patterns)
        object.__setattr__(self, "blacklist_digest", policy.blacklist_digest)

    @classmethod
    def capture(
        cls,
        *,
        generation: int,
        selected_plugins: AbstractSet[str],
        is_superuser: bool,
        provider_cutover: bool,
        tools_enabled: bool,
        search_enabled: bool,
        blacklist_patterns: tuple[str, ...],
    ) -> ToolSchemaRenderContext:
        if not isinstance(selected_plugins, AbstractSet) or not all(isinstance(name, str) for name in selected_plugins):
            raise TypeError("tool schema selected_plugins 必须是字符串集合")
        return cls(
            generation=generation,
            permission=ToolCatalogPermission.from_superuser(is_superuser),
            provider_cutover=provider_cutover,
            tools_enabled=tools_enabled,
            search_enabled=search_enabled,
            selected_plugins=tuple(selected_plugins),
            blacklist_patterns=blacklist_patterns,
        )

    @property
    def is_superuser(self) -> bool:
        return self.permission is ToolCatalogPermission.SUPERUSER

    @property
    def cache_key(self) -> ToolSchemaCacheKey:
        return ToolSchemaCacheKey(
            generation=self.generation,
            permission=self.permission,
            provider_cutover=self.provider_cutover,
            tools_enabled=self.tools_enabled,
            search_enabled=self.search_enabled,
            blacklist_digest=self.blacklist_digest,
            selected_plugins_digest=self.selected_plugins_digest,
        )

    def is_blacklisted(self, tool_name: str) -> bool:
        _validate_tool_name(tool_name, label="tool schema 工具名")
        for pattern in self.blacklist_patterns:
            if pattern == tool_name:
                return True
            if pattern.endswith("*") and tool_name.startswith(pattern[:-1]):
                return True
            if tool_name.startswith(pattern + "__"):
                return True
        return False


@dataclass(frozen=True)
class ToolSchemaRecord:
    """Immutable canonical schema payload paired with its complete identity."""

    key: ToolSchemaCacheKey
    expanded_plugins: tuple[str, ...] = field(repr=False)
    schema_json: str = field(repr=False)
    schema_digest: str = field(init=False)
    schema_bytes: int = field(init=False)
    record_bytes: int = field(init=False)
    tool_names: tuple[str, ...] = field(init=False, repr=False)
    tool_count: int = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.key, ToolSchemaCacheKey):
            raise TypeError("ToolSchemaRecord.key 必须是 ToolSchemaCacheKey")
        expanded, _expanded_digest, expanded_bytes = _canonical_tool_names(
            self.expanded_plugins,
            label="ToolSchemaRecord.expanded_plugins",
        )
        _schema, tool_names = _decode_canonical_schema(self.schema_json)
        missing = sorted(set(tool_names) - set(expanded))
        if missing:
            raise ValueError("ToolSchemaRecord schema 工具不在 expanded_plugins 中")
        encoded = self.schema_json.encode("utf-8")
        object.__setattr__(self, "expanded_plugins", expanded)
        object.__setattr__(
            self,
            "schema_digest",
            hashlib.sha256(encoded).hexdigest(),
        )
        object.__setattr__(self, "schema_bytes", len(encoded))
        object.__setattr__(self, "record_bytes", len(encoded) + expanded_bytes)
        object.__setattr__(self, "tool_names", tool_names)
        object.__setattr__(self, "tool_count", len(tool_names))

    @classmethod
    def from_schema(
        cls,
        key: ToolSchemaCacheKey,
        expanded_plugins: AbstractSet[str],
        schema: list[dict[str, Any]],
    ) -> ToolSchemaRecord:
        if not isinstance(expanded_plugins, AbstractSet) or not all(isinstance(name, str) for name in expanded_plugins):
            raise TypeError("expanded_plugins 必须是字符串集合")
        return cls(
            key=key,
            expanded_plugins=tuple(expanded_plugins),
            schema_json=_canonical_schema_json(schema),
        )

    def materialize(self) -> tuple[set[str], list[dict[str, Any]]]:
        schema = json.loads(self.schema_json)
        if not isinstance(schema, list):
            raise ToolSchemaCacheUnavailableError("tool schema record 无法物化")
        return set(self.expanded_plugins), schema


@runtime_checkable
class ToolSchemaCacheProtocol(Protocol):
    """Backend-neutral cache contract for immutable schema records."""

    async def lookup(
        self,
        key: ToolSchemaCacheKey,
    ) -> ToolSchemaRecord | None: ...

    async def publish(
        self,
        record: ToolSchemaRecord,
    ) -> ToolSchemaRecord:
        """Publish only a record whose full build and parity checks succeeded."""
        ...


@dataclass(frozen=True)
class MemoryToolSchemaCacheSettings:
    """Bounded process-local schema cache policy."""

    max_entries: int = 256
    max_record_bytes: int = 1_048_576
    max_total_bytes: int = 16_777_216

    def __post_init__(self) -> None:
        _validate_positive_integer(
            self.max_entries,
            label="max_entries",
            maximum=65_536,
        )
        _validate_positive_integer(
            self.max_record_bytes,
            label="max_record_bytes",
            maximum=_MAX_RECORD_BYTES,
        )
        _validate_positive_integer(
            self.max_total_bytes,
            label="max_total_bytes",
            maximum=536_870_912,
        )
        if self.max_total_bytes < self.max_record_bytes:
            raise ValueError("max_total_bytes 不得小于 max_record_bytes")

    def safe_diagnostics(self) -> dict[str, int]:
        return {
            "max_entries": self.max_entries,
            "max_record_bytes": self.max_record_bytes,
            "max_total_bytes": self.max_total_bytes,
        }


class MemoryToolSchemaCache:
    """Generation-keyed LRU schema cache bound to one PID and event loop."""

    def __init__(
        self,
        *,
        settings: MemoryToolSchemaCacheSettings | None = None,
        pid_provider: PidProvider | None = None,
        loop_provider: LoopProvider | None = None,
    ) -> None:
        if settings is not None and not isinstance(
            settings,
            MemoryToolSchemaCacheSettings,
        ):
            raise TypeError("settings 必须是 MemoryToolSchemaCacheSettings")
        for dependency, label in (
            (pid_provider, "pid_provider"),
            (loop_provider, "loop_provider"),
        ):
            if dependency is not None and not callable(dependency):
                raise TypeError(f"{label} 必须可调用")
        self._settings = MemoryToolSchemaCacheSettings() if settings is None else settings
        self._pid_provider = pid_provider or os.getpid
        self._loop_provider = loop_provider or asyncio.get_running_loop
        self._records: OrderedDict[ToolSchemaCacheKey, ToolSchemaRecord] = OrderedDict()
        self._total_bytes = 0
        self._owner_pid: int | None = None
        self._owner_loop: asyncio.AbstractEventLoop | None = None
        self._lock = asyncio.Lock()

    @property
    def settings(self) -> MemoryToolSchemaCacheSettings:
        return self._settings

    def __repr__(self) -> str:
        return f"MemoryToolSchemaCache(entries={len(self._records)!r}, total_bytes={self._total_bytes!r})"

    def safe_diagnostics(self) -> dict[str, bool | int | str]:
        return {
            "backend": "memory",
            "configured": True,
            **self._settings.safe_diagnostics(),
        }

    def _claim_owner(self) -> None:
        try:
            pid = self._pid_provider()
        except Exception as error:
            raise ToolSchemaCacheUnavailableError(f"tool schema cache 无法确认进程身份 ({type(error).__name__})") from None
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise ToolSchemaCacheUnavailableError("tool schema cache 进程身份无效")
        try:
            loop = self._loop_provider()
        except Exception as error:
            raise ToolSchemaCacheUnavailableError(f"tool schema cache 无法确认 event loop ({type(error).__name__})") from None
        if not isinstance(loop, asyncio.AbstractEventLoop):
            raise ToolSchemaCacheUnavailableError("tool schema cache event loop 身份无效")

        if self._owner_pid is None:
            self._owner_pid = pid
            self._owner_loop = loop
            return
        if pid != self._owner_pid:
            raise ToolSchemaCacheOwnershipError("MemoryToolSchemaCache 不得跨进程复用")
        if loop is not self._owner_loop:
            raise ToolSchemaCacheOwnershipError("MemoryToolSchemaCache 不得跨 event loop 复用")

    async def lookup(
        self,
        key: ToolSchemaCacheKey,
    ) -> ToolSchemaRecord | None:
        if not isinstance(key, ToolSchemaCacheKey):
            raise TypeError("key 必须是 ToolSchemaCacheKey")
        self._claim_owner()
        async with self._lock:
            record = self._records.get(key)
            if record is not None:
                self._records.move_to_end(key)
            return record

    async def publish(
        self,
        record: ToolSchemaRecord,
    ) -> ToolSchemaRecord:
        if not isinstance(record, ToolSchemaRecord):
            raise TypeError("record 必须是 ToolSchemaRecord")
        if record.record_bytes > self._settings.max_record_bytes:
            raise ToolSchemaCacheUnavailableError("tool schema cache record 超过配置大小上限")
        self._claim_owner()
        async with self._lock:
            current = self._records.get(record.key)
            if current is not None:
                self._records.move_to_end(record.key)
                if current != record:
                    raise ToolSchemaCacheConflictError("相同 tool schema cache identity 产生不同 schema")
                return current

            self._records[record.key] = record
            self._total_bytes += record.record_bytes
            while len(self._records) > self._settings.max_entries or self._total_bytes > self._settings.max_total_bytes:
                _key, evicted = self._records.popitem(last=False)
                self._total_bytes -= evicted.record_bytes
            return record

    async def clear(self) -> None:
        self._claim_owner()
        async with self._lock:
            self._records.clear()
            self._total_bytes = 0


async def resolve_tool_schema(
    cache: ToolSchemaCacheProtocol,
    key: ToolSchemaCacheKey,
    builder: SchemaBuilder,
) -> ToolSchemaRecord:
    """Resolve one schema without treating cache failure as an implicit bypass."""

    if not isinstance(key, ToolSchemaCacheKey):
        raise TypeError("key 必须是 ToolSchemaCacheKey")
    if not callable(builder):
        raise TypeError("builder 必须可调用")
    try:
        cached = await cache.lookup(key)
    except TimeoutError:
        raise ToolSchemaCacheUnavailableError("tool schema cache lookup 超时") from None
    if cached is not None:
        if not isinstance(cached, ToolSchemaRecord) or cached.key != key:
            raise ToolSchemaCacheUnavailableError("tool schema cache 返回了错误 identity")
        return cached

    record = builder()
    if not isinstance(record, ToolSchemaRecord):
        raise TypeError("tool schema builder 必须返回 ToolSchemaRecord")
    if record.key != key:
        raise ValueError("tool schema builder 返回了错误 identity")
    try:
        published = await cache.publish(record)
    except TimeoutError:
        raise ToolSchemaCacheUnavailableError("tool schema cache publish 超时") from None
    if not isinstance(published, ToolSchemaRecord) or published != record:
        raise ToolSchemaCacheUnavailableError("tool schema cache 未确认精确发布结果")
    return published

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
import os
import re
from typing import Protocol, runtime_checkable

_POSTGRES_BIGINT_MAX = (1 << 63) - 1
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_BLACKLIST_PATTERNS = 4_096
_MAX_BLACKLIST_PATTERN_CHARS = 512
_MAX_BLACKLIST_PAYLOAD_BYTES = 1_048_576
_MAX_CATALOG_BYTES = 16_777_216

CatalogBuilder = Callable[[], "ToolCatalogRecord"]
LoopProvider = Callable[[], asyncio.AbstractEventLoop]
PidProvider = Callable[[], int]


class ToolCatalogCacheError(RuntimeError):
    """Base error for a replaceable, generation-bound tool catalog cache."""


class ToolCatalogCacheUnavailableError(ToolCatalogCacheError):
    """The cache could not establish a trustworthy result."""


class ToolCatalogCacheConflictError(ToolCatalogCacheError):
    """The same complete cache identity produced different catalog values."""


class ToolCatalogCacheOwnershipError(ToolCatalogCacheError):
    """A process-local cache was reused by another process or event loop."""


class ToolCatalogPermission(str, Enum):
    USER = "user"
    SUPERUSER = "superuser"

    @classmethod
    def from_superuser(cls, is_superuser: bool) -> ToolCatalogPermission:
        if type(is_superuser) is not bool:
            raise TypeError("is_superuser 必须是布尔值")
        return cls.SUPERUSER if is_superuser else cls.USER


def _validate_generation(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= _POSTGRES_BIGINT_MAX:
        raise ValueError("tool catalog generation 必须是非负 BIGINT 整数")
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


def _canonical_blacklist(
    patterns: tuple[str, ...],
) -> tuple[tuple[str, ...], str]:
    if not isinstance(patterns, tuple):
        raise TypeError("tool catalog blacklist_patterns 必须是元组")
    if len(patterns) > _MAX_BLACKLIST_PATTERNS:
        raise ValueError("tool catalog blacklist_patterns 数量超过安全上限")

    normalized: set[str] = set()
    for pattern in patterns:
        if not isinstance(pattern, str):
            raise TypeError("tool catalog blacklist_patterns 只能包含字符串")
        item = pattern.strip()
        if not item:
            continue
        if "\x00" in item or len(item) > _MAX_BLACKLIST_PATTERN_CHARS:
            raise ValueError("tool catalog blacklist pattern 非法或过长")
        normalized.add(item)

    ordered = tuple(sorted(normalized))
    encoded = json.dumps(
        ordered,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    if len(encoded) > _MAX_BLACKLIST_PAYLOAD_BYTES:
        raise ValueError("tool catalog blacklist payload 超过安全上限")
    return ordered, hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ToolCatalogCacheKey:
    """The complete identity of one permission-filtered catalog rendering."""

    generation: int
    permission: ToolCatalogPermission
    provider_cutover: bool
    tools_enabled: bool
    web_search_enabled: bool
    blacklist_digest: str
    protocol_scope_digest: str = "0" * 64

    def __post_init__(self) -> None:
        _validate_generation(self.generation)
        if not isinstance(self.permission, ToolCatalogPermission):
            raise TypeError("tool catalog permission 必须是受支持的权限级别")
        for field_name in (
            "provider_cutover",
            "tools_enabled",
            "web_search_enabled",
        ):
            if type(getattr(self, field_name)) is not bool:
                raise TypeError(f"tool catalog {field_name} 必须是布尔值")
        if not isinstance(self.blacklist_digest, str) or not _SHA256_RE.fullmatch(self.blacklist_digest):
            raise ValueError("tool catalog blacklist_digest 必须是 SHA-256")
        if not isinstance(self.protocol_scope_digest, str) or not _SHA256_RE.fullmatch(self.protocol_scope_digest):
            raise ValueError("tool catalog protocol_scope_digest 必须是 SHA-256")

    @property
    def policy_digest(self) -> str:
        payload = {
            "blacklist_digest": self.blacklist_digest,
            "provider_cutover": self.provider_cutover,
            "protocol_scope_digest": self.protocol_scope_digest,
            "tools_enabled": self.tools_enabled,
            "web_search_enabled": self.web_search_enabled,
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
        return f"catalog:{self.permission.value}:{self.generation}:{self.policy_digest}"


@dataclass(frozen=True)
class ToolCatalogRenderContext:
    """Explicit dynamic inputs used to render one generation-bound catalog."""

    generation: int
    permission: ToolCatalogPermission
    provider_cutover: bool
    tools_enabled: bool
    web_search_enabled: bool
    blacklist_patterns: tuple[str, ...] = field(repr=False)
    protocol_scope_digest: str = "0" * 64
    blacklist_digest: str = field(init=False)

    def __post_init__(self) -> None:
        _validate_generation(self.generation)
        if not isinstance(self.permission, ToolCatalogPermission):
            raise TypeError("tool catalog permission 必须是受支持的权限级别")
        for field_name in (
            "provider_cutover",
            "tools_enabled",
            "web_search_enabled",
        ):
            if type(getattr(self, field_name)) is not bool:
                raise TypeError(f"tool catalog {field_name} 必须是布尔值")
        patterns, digest = _canonical_blacklist(self.blacklist_patterns)
        if not isinstance(self.protocol_scope_digest, str) or not _SHA256_RE.fullmatch(self.protocol_scope_digest):
            raise ValueError("tool catalog protocol_scope_digest 必须是 SHA-256")
        object.__setattr__(self, "blacklist_patterns", patterns)
        object.__setattr__(self, "blacklist_digest", digest)

    @classmethod
    def capture(
        cls,
        *,
        generation: int,
        is_superuser: bool,
        provider_cutover: bool,
        tools_enabled: bool,
        web_search_enabled: bool,
        blacklist_patterns: tuple[str, ...],
        protocol_scope_digest: str = "0" * 64,
    ) -> ToolCatalogRenderContext:
        return cls(
            generation=generation,
            permission=ToolCatalogPermission.from_superuser(is_superuser),
            provider_cutover=provider_cutover,
            tools_enabled=tools_enabled,
            web_search_enabled=web_search_enabled,
            blacklist_patterns=blacklist_patterns,
            protocol_scope_digest=protocol_scope_digest,
        )

    @property
    def is_superuser(self) -> bool:
        return self.permission is ToolCatalogPermission.SUPERUSER

    @property
    def cache_key(self) -> ToolCatalogCacheKey:
        return ToolCatalogCacheKey(
            generation=self.generation,
            permission=self.permission,
            provider_cutover=self.provider_cutover,
            tools_enabled=self.tools_enabled,
            web_search_enabled=self.web_search_enabled,
            blacklist_digest=self.blacklist_digest,
            protocol_scope_digest=self.protocol_scope_digest,
        )

    def is_blacklisted(self, tool_name: str) -> bool:
        if not isinstance(tool_name, str) or not tool_name:
            raise ValueError("tool catalog 工具名不能为空")
        for pattern in self.blacklist_patterns:
            if pattern == tool_name:
                return True
            if pattern.endswith("*") and tool_name.startswith(pattern[:-1]):
                return True
            if tool_name.startswith(pattern + "__"):
                return True
        return False


@dataclass(frozen=True)
class ToolCatalogRecord:
    """An immutable, digest-stamped catalog value paired with its complete key."""

    key: ToolCatalogCacheKey
    catalog: str
    catalog_digest: str = field(init=False)
    catalog_bytes: int = field(init=False)
    entry_count: int = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.key, ToolCatalogCacheKey):
            raise TypeError("ToolCatalogRecord.key 必须是 ToolCatalogCacheKey")
        if not isinstance(self.catalog, str) or not self.catalog or "\x00" in self.catalog:
            raise ValueError("ToolCatalogRecord.catalog 必须是非空安全字符串")
        encoded = self.catalog.encode("utf-8")
        if len(encoded) > _MAX_CATALOG_BYTES:
            raise ValueError("ToolCatalogRecord.catalog 超过绝对安全上限")
        object.__setattr__(
            self,
            "catalog_digest",
            hashlib.sha256(encoded).hexdigest(),
        )
        object.__setattr__(self, "catalog_bytes", len(encoded))
        object.__setattr__(self, "entry_count", self.catalog.count("\n") + 1)


@runtime_checkable
class ToolCatalogCacheProtocol(Protocol):
    """Backend-neutral cache contract; the generation snapshot remains authoritative."""

    async def lookup(
        self,
        key: ToolCatalogCacheKey,
    ) -> ToolCatalogRecord | None: ...

    async def publish(
        self,
        record: ToolCatalogRecord,
    ) -> ToolCatalogRecord:
        """Publish only a record whose full render and parity checks succeeded."""
        ...


@dataclass(frozen=True)
class MemoryToolCatalogCacheSettings:
    """Bounded process-local catalog cache policy."""

    max_entries: int = 256
    max_catalog_bytes: int = 262_144
    max_total_bytes: int = 8_388_608

    def __post_init__(self) -> None:
        _validate_positive_integer(
            self.max_entries,
            label="max_entries",
            maximum=65_536,
        )
        _validate_positive_integer(
            self.max_catalog_bytes,
            label="max_catalog_bytes",
            maximum=_MAX_CATALOG_BYTES,
        )
        _validate_positive_integer(
            self.max_total_bytes,
            label="max_total_bytes",
            maximum=268_435_456,
        )
        if self.max_total_bytes < self.max_catalog_bytes:
            raise ValueError("max_total_bytes 不得小于 max_catalog_bytes")

    def safe_diagnostics(self) -> dict[str, int]:
        return {
            "max_entries": self.max_entries,
            "max_catalog_bytes": self.max_catalog_bytes,
            "max_total_bytes": self.max_total_bytes,
        }


class MemoryToolCatalogCache:
    """Generation-keyed LRU cache bound to one PID and event loop."""

    def __init__(
        self,
        *,
        settings: MemoryToolCatalogCacheSettings | None = None,
        pid_provider: PidProvider | None = None,
        loop_provider: LoopProvider | None = None,
    ) -> None:
        if settings is not None and not isinstance(
            settings,
            MemoryToolCatalogCacheSettings,
        ):
            raise TypeError("settings 必须是 MemoryToolCatalogCacheSettings")
        for dependency, label in (
            (pid_provider, "pid_provider"),
            (loop_provider, "loop_provider"),
        ):
            if dependency is not None and not callable(dependency):
                raise TypeError(f"{label} 必须可调用")
        self._settings = MemoryToolCatalogCacheSettings() if settings is None else settings
        self._pid_provider = pid_provider or os.getpid
        self._loop_provider = loop_provider or asyncio.get_running_loop
        self._records: OrderedDict[
            ToolCatalogCacheKey,
            ToolCatalogRecord,
        ] = OrderedDict()
        self._total_bytes = 0
        self._owner_pid: int | None = None
        self._owner_loop: asyncio.AbstractEventLoop | None = None
        self._lock = asyncio.Lock()

    @property
    def settings(self) -> MemoryToolCatalogCacheSettings:
        return self._settings

    def __repr__(self) -> str:
        return f"MemoryToolCatalogCache(entries={len(self._records)!r}, total_bytes={self._total_bytes!r})"

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
            raise ToolCatalogCacheUnavailableError(f"tool catalog cache 无法确认进程身份 ({type(error).__name__})") from None
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise ToolCatalogCacheUnavailableError("tool catalog cache 进程身份无效")
        try:
            loop = self._loop_provider()
        except Exception as error:
            raise ToolCatalogCacheUnavailableError(f"tool catalog cache 无法确认 event loop ({type(error).__name__})") from None
        if not isinstance(loop, asyncio.AbstractEventLoop):
            raise ToolCatalogCacheUnavailableError("tool catalog cache event loop 身份无效")

        if self._owner_pid is None:
            self._owner_pid = pid
            self._owner_loop = loop
            return
        if pid != self._owner_pid:
            raise ToolCatalogCacheOwnershipError("MemoryToolCatalogCache 不得跨进程复用")
        if loop is not self._owner_loop:
            raise ToolCatalogCacheOwnershipError("MemoryToolCatalogCache 不得跨 event loop 复用")

    async def lookup(
        self,
        key: ToolCatalogCacheKey,
    ) -> ToolCatalogRecord | None:
        if not isinstance(key, ToolCatalogCacheKey):
            raise TypeError("key 必须是 ToolCatalogCacheKey")
        self._claim_owner()
        async with self._lock:
            record = self._records.get(key)
            if record is not None:
                self._records.move_to_end(key)
            return record

    async def publish(
        self,
        record: ToolCatalogRecord,
    ) -> ToolCatalogRecord:
        if not isinstance(record, ToolCatalogRecord):
            raise TypeError("record 必须是 ToolCatalogRecord")
        if record.catalog_bytes > self._settings.max_catalog_bytes:
            raise ToolCatalogCacheUnavailableError("tool catalog cache record 超过配置大小上限")
        self._claim_owner()
        async with self._lock:
            current = self._records.get(record.key)
            if current is not None:
                self._records.move_to_end(record.key)
                if current != record:
                    raise ToolCatalogCacheConflictError("相同 tool catalog cache identity 产生不同目录")
                return current

            self._records[record.key] = record
            self._total_bytes += record.catalog_bytes
            while len(self._records) > self._settings.max_entries or self._total_bytes > self._settings.max_total_bytes:
                _key, evicted = self._records.popitem(last=False)
                self._total_bytes -= evicted.catalog_bytes
            return record

    async def clear(self) -> None:
        self._claim_owner()
        async with self._lock:
            self._records.clear()
            self._total_bytes = 0


async def resolve_tool_catalog(
    cache: ToolCatalogCacheProtocol,
    key: ToolCatalogCacheKey,
    builder: CatalogBuilder,
) -> ToolCatalogRecord:
    """Resolve one catalog without treating cache failure as an implicit bypass."""

    if not isinstance(key, ToolCatalogCacheKey):
        raise TypeError("key 必须是 ToolCatalogCacheKey")
    if not callable(builder):
        raise TypeError("builder 必须可调用")
    try:
        cached = await cache.lookup(key)
    except TimeoutError:
        raise ToolCatalogCacheUnavailableError("tool catalog cache lookup 超时") from None
    if cached is not None:
        if not isinstance(cached, ToolCatalogRecord) or cached.key != key:
            raise ToolCatalogCacheUnavailableError("tool catalog cache 返回了错误 identity")
        return cached

    record = builder()
    if not isinstance(record, ToolCatalogRecord):
        raise TypeError("tool catalog builder 必须返回 ToolCatalogRecord")
    if record.key != key:
        raise ValueError("tool catalog builder 返回了错误 identity")
    try:
        published = await cache.publish(record)
    except TimeoutError:
        raise ToolCatalogCacheUnavailableError("tool catalog cache publish 超时") from None
    if not isinstance(published, ToolCatalogRecord) or published != record:
        raise ToolCatalogCacheUnavailableError("tool catalog cache 未确认精确发布结果")
    return published

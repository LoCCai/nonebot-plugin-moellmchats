from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
import hashlib
import inspect
import json
import math
import os
import re
import time
from typing import Any, Protocol, runtime_checkable
import unicodedata

from .tool_catalog_cache import (
    ToolCatalogPermission,
    ToolCatalogRecord,
)

_POSTGRES_BIGINT_MAX = (1 << 63) - 1
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_POLICY_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_CAPABILITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
_NORMALIZATION_VERSION = "nfkc-whitespace-v1"
_MAX_PROMPT_CHARS = 131_072
_MAX_PROMPT_BYTES = 262_144
_MAX_MODEL_IDENTITY_CHARS = 4_096
_MAX_CAPABILITIES = 256
_MAX_CAPABILITY_PAYLOAD_BYTES = 65_536
_MAX_REQUIRED_PLUGINS = 512
_MAX_TOOL_NAME_CHARS = 512
_MAX_RESULT_BYTES = 1_048_576
_MAX_CACHE_TTL_SECONDS = 300.0

ClassificationBuilder = Callable[[], Awaitable["ClassificationCacheRecord"]]
Clock = Callable[[], float]
LoopProvider = Callable[[], asyncio.AbstractEventLoop]
PidProvider = Callable[[], int]


class ClassificationCacheError(RuntimeError):
    """Base error for the bounded classification-result cache."""


class ClassificationCacheIneligibleError(ClassificationCacheError):
    """The request depends on context that must never be cached."""


class ClassificationCacheUnavailableError(ClassificationCacheError):
    """The cache could not establish a trustworthy result."""


class ClassificationCacheConflictError(ClassificationCacheError):
    """The same complete identity produced different classification results."""


class ClassificationCacheOwnershipError(ClassificationCacheError):
    """A process-local cache was reused by another process or event loop."""


class ClassificationDifficulty(str, Enum):
    SIMPLE = "0"
    MEDIUM = "1"
    HARD = "2"

    @classmethod
    def parse(cls, value: object) -> ClassificationDifficulty:
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            try:
                return cls(value)
            except ValueError:
                pass
        raise ValueError("classification difficulty 必须是 0、1 或 2")


class ClassificationResultSource(str, Enum):
    MODEL_SUCCESS = "model_success"
    TIMEOUT_FALLBACK = "timeout_fallback"
    PARSE_FALLBACK = "parse_fallback"
    CONTENT_BLOCKED = "content_blocked"

    @classmethod
    def parse(cls, value: object) -> ClassificationResultSource:
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            try:
                return cls(value)
            except ValueError:
                pass
        raise ValueError("classification result source 非法")

    @property
    def cacheable(self) -> bool:
        return self is self.MODEL_SUCCESS


def _validate_generation(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= _POSTGRES_BIGINT_MAX:
        raise ValueError("classification generation 必须是非负 BIGINT 整数")
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


def _validate_seconds(
    value: object,
    *,
    label: str,
    minimum: float,
    maximum: float,
) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or not minimum <= value <= maximum
    ):
        raise ValueError(f"{label} 必须是 {minimum:g} 到 {maximum:g} 的有限秒数")
    return float(value)


def _validate_digest(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{label} 必须是 SHA-256")
    return value


def _validate_boolean(value: object, *, label: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{label} 必须是布尔值")
    return value


def _validate_identity_text(
    value: object,
    *,
    label: str,
    maximum: int,
) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value or len(value) > maximum:
        raise ValueError(f"{label} 必须是非空安全字符串")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ValueError(f"{label} 必须是有效 UTF-8 文本") from None
    return value


def _normalized_prompt(prompt: object) -> tuple[str, int]:
    if not isinstance(prompt, str):
        raise TypeError("classification prompt 必须是字符串")
    if len(prompt) > _MAX_PROMPT_CHARS:
        raise ValueError("classification prompt 超过字符安全上限")
    if "\x00" in prompt:
        raise ValueError("classification prompt 不得包含 NUL")
    normalized = " ".join(unicodedata.normalize("NFKC", prompt).split())
    if not normalized:
        raise ValueError("classification prompt 规范化后不能为空")
    try:
        encoded = normalized.encode("utf-8")
    except UnicodeError:
        raise ValueError("classification prompt 必须是有效 UTF-8 文本") from None
    if len(encoded) > _MAX_PROMPT_BYTES:
        raise ValueError("classification prompt 超过字节安全上限")
    return hashlib.sha256(encoded).hexdigest(), len(encoded)


def normalized_classification_prompt_hash(prompt: str) -> str:
    """Return the stable digest used for whitespace-equivalent standard prompts."""

    digest, _size = _normalized_prompt(prompt)
    return digest


def _canonical_capabilities(
    permission: ToolCatalogPermission,
    additional_capabilities: tuple[str, ...],
) -> tuple[str, int]:
    if not isinstance(permission, ToolCatalogPermission):
        raise TypeError("classification permission 必须是受支持的权限级别")
    if not isinstance(additional_capabilities, tuple):
        raise TypeError("classification additional_capabilities 必须是元组")
    if len(additional_capabilities) > _MAX_CAPABILITIES:
        raise ValueError("classification additional_capabilities 数量超过安全上限")

    capabilities = {f"permission:{permission.value}"}
    for capability in additional_capabilities:
        if not isinstance(capability, str) or not _CAPABILITY_RE.fullmatch(capability):
            raise ValueError("classification additional_capabilities 必须是安全 capability token")
        capabilities.add(capability)
    ordered = tuple(sorted(capabilities))
    encoded = json.dumps(
        ordered,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    if len(encoded) > _MAX_CAPABILITY_PAYLOAD_BYTES:
        raise ValueError("classification capability payload 超过安全上限")
    return hashlib.sha256(encoded).hexdigest(), len(ordered)


def _validate_tool_name(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > _MAX_TOOL_NAME_CHARS
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("classification required_plugins 必须包含安全工具名")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ValueError("classification required_plugins 必须包含有效 UTF-8 工具名") from None
    return value


def _canonical_plugins(values: object) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise TypeError("classification required_plugins 必须是数组或元组")
    if len(values) > _MAX_REQUIRED_PLUGINS:
        raise ValueError("classification required_plugins 数量超过安全上限")
    normalized = tuple(sorted({_validate_tool_name(value) for value in values}))
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > _MAX_RESULT_BYTES:
        raise ValueError("classification required_plugins payload 超过安全上限")
    return normalized


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("classification result JSON 包含重复字段")
        value[key] = item
    return value


def _canonical_result_json(
    difficulty: ClassificationDifficulty,
    vision_required: bool,
    required_plugins: tuple[str, ...],
    source: ClassificationResultSource,
) -> str:
    value = {
        "difficulty": difficulty.value,
        "required_plugins": list(required_plugins),
        "source": source.value,
        "vision_required": vision_required,
    }
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(payload.encode("utf-8")) > _MAX_RESULT_BYTES:
        raise ValueError("classification result payload 超过绝对安全上限")
    return payload


def _decode_canonical_result(
    payload: object,
) -> tuple[
    ClassificationDifficulty,
    bool,
    tuple[str, ...],
    ClassificationResultSource,
]:
    if not isinstance(payload, str) or "\x00" in payload:
        raise ValueError("ClassificationCacheRecord.result_json 必须是安全 JSON 字符串")
    try:
        encoded = payload.encode("utf-8")
    except UnicodeError:
        raise ValueError("ClassificationCacheRecord.result_json 必须是有效 UTF-8") from None
    if len(encoded) > _MAX_RESULT_BYTES:
        raise ValueError("ClassificationCacheRecord.result_json 超过绝对安全上限")
    try:
        value = json.loads(payload, object_pairs_hook=_reject_duplicate_pairs)
    except json.JSONDecodeError as error:
        raise ValueError(f"ClassificationCacheRecord.result_json 非法 ({type(error).__name__})") from None
    except ValueError as error:
        raise ValueError(str(error)) from None
    if not isinstance(value, dict) or set(value) != {
        "difficulty",
        "required_plugins",
        "source",
        "vision_required",
    }:
        raise ValueError("ClassificationCacheRecord.result_json 字段集合非法")
    difficulty = ClassificationDifficulty.parse(value["difficulty"])
    source = ClassificationResultSource.parse(value["source"])
    if not source.cacheable:
        raise ValueError("ClassificationCacheRecord 只能保存成功模型分类")
    if type(value["vision_required"]) is not bool:
        raise ValueError("classification vision_required 必须是布尔值")
    vision_required = value["vision_required"]
    try:
        required_plugins = _canonical_plugins(value["required_plugins"])
    except TypeError as error:
        raise ValueError(str(error)) from None
    canonical = _canonical_result_json(
        difficulty,
        vision_required,
        required_plugins,
        source,
    )
    if canonical != payload:
        raise ValueError("ClassificationCacheRecord.result_json 必须是 canonical JSON")
    return difficulty, vision_required, required_plugins, source


@dataclass(frozen=True, repr=False)
class ClassificationModelIdentity:
    """A digest-only identity for the classifier endpoint and request mode."""

    digest: str

    def __post_init__(self) -> None:
        _validate_digest(self.digest, label="classification model digest")

    @classmethod
    def capture(
        cls,
        *,
        model: str,
        endpoint: str,
        json_mode: bool,
        api_family: str = "openai-chat-completions",
    ) -> ClassificationModelIdentity:
        model = _validate_identity_text(
            model,
            label="classification model",
            maximum=_MAX_MODEL_IDENTITY_CHARS,
        )
        endpoint = _validate_identity_text(
            endpoint,
            label="classification endpoint",
            maximum=_MAX_MODEL_IDENTITY_CHARS,
        )
        _validate_boolean(json_mode, label="classification json_mode")
        if not isinstance(api_family, str) or not _POLICY_VERSION_RE.fullmatch(api_family):
            raise ValueError("classification api_family 必须是安全版本标识")
        encoded = json.dumps(
            {
                "api_family": api_family,
                "endpoint": endpoint,
                "json_mode": json_mode,
                "model": model,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return cls(hashlib.sha256(encoded).hexdigest())

    def __repr__(self) -> str:
        return f"ClassificationModelIdentity(digest={self.digest!r})"


@dataclass(frozen=True)
class ClassificationRequestScope:
    """Explicit evidence that a classification request is context independent."""

    conversation_bound: bool
    attachment_bound: bool
    actor_identity_bound: bool
    session_state_bound: bool
    external_state_bound: bool

    def __post_init__(self) -> None:
        for field_name in (
            "conversation_bound",
            "attachment_bound",
            "actor_identity_bound",
            "session_state_bound",
            "external_state_bound",
        ):
            _validate_boolean(
                getattr(self, field_name),
                label=f"classification scope {field_name}",
            )

    @classmethod
    def standard_prompt(cls) -> ClassificationRequestScope:
        return cls(
            conversation_bound=False,
            attachment_bound=False,
            actor_identity_bound=False,
            session_state_bound=False,
            external_state_bound=False,
        )

    @property
    def cacheable(self) -> bool:
        return not any(
            (
                self.conversation_bound,
                self.attachment_bound,
                self.actor_identity_bound,
                self.session_state_bound,
                self.external_state_bound,
            )
        )

    def require_cacheable(self) -> None:
        if not self.cacheable:
            raise ClassificationCacheIneligibleError("上下文相关 classification request 禁止缓存")


@dataclass(frozen=True, repr=False)
class ClassificationCacheKey:
    """Complete identity for one short-lived standard classification."""

    generation: int
    permission: ToolCatalogPermission
    provider_cutover: bool
    tools_enabled: bool
    web_search_enabled: bool
    blacklist_digest: str
    catalog_digest: str
    normalized_prompt_hash: str
    capability_digest: str
    classifier_digest: str
    policy_version: str
    ttl_seconds: float

    def __post_init__(self) -> None:
        _validate_generation(self.generation)
        if not isinstance(self.permission, ToolCatalogPermission):
            raise TypeError("classification permission 必须是受支持的权限级别")
        for field_name in (
            "provider_cutover",
            "tools_enabled",
            "web_search_enabled",
        ):
            _validate_boolean(
                getattr(self, field_name),
                label=f"classification {field_name}",
            )
        for field_name in (
            "blacklist_digest",
            "catalog_digest",
            "normalized_prompt_hash",
            "capability_digest",
            "classifier_digest",
        ):
            _validate_digest(
                getattr(self, field_name),
                label=f"classification {field_name}",
            )
        if not isinstance(self.policy_version, str) or not _POLICY_VERSION_RE.fullmatch(self.policy_version):
            raise ValueError("classification policy_version 必须是安全版本标识")
        ttl_seconds = _validate_seconds(
            self.ttl_seconds,
            label="classification ttl_seconds",
            minimum=1.0,
            maximum=_MAX_CACHE_TTL_SECONDS,
        )
        object.__setattr__(self, "ttl_seconds", ttl_seconds)

    @property
    def identity_digest(self) -> str:
        encoded = json.dumps(
            {
                "blacklist_digest": self.blacklist_digest,
                "capability_digest": self.capability_digest,
                "catalog_digest": self.catalog_digest,
                "classifier_digest": self.classifier_digest,
                "generation": self.generation,
                "normalization_version": _NORMALIZATION_VERSION,
                "normalized_prompt_hash": self.normalized_prompt_hash,
                "permission": self.permission.value,
                "policy_version": self.policy_version,
                "provider_cutover": self.provider_cutover,
                "tools_enabled": self.tools_enabled,
                "ttl_seconds": self.ttl_seconds,
                "web_search_enabled": self.web_search_enabled,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        return hashlib.sha256(encoded).hexdigest()

    @property
    def safe_cache_key(self) -> str:
        return f"classification:{self.generation}:{self.identity_digest}"

    def safe_diagnostics(self) -> dict[str, bool | float | int | str]:
        return {
            "generation": self.generation,
            "permission": self.permission.value,
            "policy_version": self.policy_version,
            "provider_cutover": self.provider_cutover,
            "tools_enabled": self.tools_enabled,
            "ttl_seconds": self.ttl_seconds,
            "web_search_enabled": self.web_search_enabled,
        }

    def __repr__(self) -> str:
        return (
            "ClassificationCacheKey("
            f"safe_cache_key={self.safe_cache_key!r}, "
            f"policy_version={self.policy_version!r}, "
            f"ttl_seconds={self.ttl_seconds!r})"
        )


@dataclass(frozen=True, repr=False)
class ClassificationRenderContext:
    """Digest-only capture of all inputs allowed to affect a cached result."""

    key: ClassificationCacheKey
    request_scope: ClassificationRequestScope
    normalized_prompt_bytes: int
    capability_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.key, ClassificationCacheKey):
            raise TypeError("ClassificationRenderContext.key 必须是 ClassificationCacheKey")
        if not isinstance(self.request_scope, ClassificationRequestScope):
            raise TypeError("ClassificationRenderContext.request_scope 必须是 ClassificationRequestScope")
        self.request_scope.require_cacheable()
        _validate_positive_integer(
            self.normalized_prompt_bytes,
            label="normalized_prompt_bytes",
            maximum=_MAX_PROMPT_BYTES,
        )
        _validate_positive_integer(
            self.capability_count,
            label="capability_count",
            maximum=_MAX_CAPABILITIES + 1,
        )

    @classmethod
    def capture(
        cls,
        *,
        prompt: str,
        catalog_record: ToolCatalogRecord,
        model_identity: ClassificationModelIdentity,
        request_scope: ClassificationRequestScope,
        policy_version: str,
        additional_capabilities: tuple[str, ...] = (),
        ttl_seconds: float = 60.0,
    ) -> ClassificationRenderContext:
        if not isinstance(catalog_record, ToolCatalogRecord):
            raise TypeError("catalog_record 必须是 ToolCatalogRecord")
        if not isinstance(model_identity, ClassificationModelIdentity):
            raise TypeError("model_identity 必须是 ClassificationModelIdentity")
        if not isinstance(request_scope, ClassificationRequestScope):
            raise TypeError("request_scope 必须是 ClassificationRequestScope")
        request_scope.require_cacheable()
        prompt_hash, prompt_bytes = _normalized_prompt(prompt)
        catalog_key = catalog_record.key
        capability_digest, capability_count = _canonical_capabilities(
            catalog_key.permission,
            additional_capabilities,
        )
        key = ClassificationCacheKey(
            generation=catalog_key.generation,
            permission=catalog_key.permission,
            provider_cutover=catalog_key.provider_cutover,
            tools_enabled=catalog_key.tools_enabled,
            web_search_enabled=catalog_key.web_search_enabled,
            blacklist_digest=catalog_key.blacklist_digest,
            catalog_digest=catalog_record.catalog_digest,
            normalized_prompt_hash=prompt_hash,
            capability_digest=capability_digest,
            classifier_digest=model_identity.digest,
            policy_version=policy_version,
            ttl_seconds=ttl_seconds,
        )
        return cls(
            key=key,
            request_scope=request_scope,
            normalized_prompt_bytes=prompt_bytes,
            capability_count=capability_count,
        )

    @property
    def cache_key(self) -> ClassificationCacheKey:
        return self.key

    def safe_diagnostics(self) -> dict[str, bool | float | int | str]:
        return {
            **self.key.safe_diagnostics(),
            "capability_count": self.capability_count,
            "context_independent": self.request_scope.cacheable,
            "normalized_prompt_bytes": self.normalized_prompt_bytes,
        }

    def __repr__(self) -> str:
        return (
            "ClassificationRenderContext("
            f"safe_cache_key={self.key.safe_cache_key!r}, "
            f"normalized_prompt_bytes={self.normalized_prompt_bytes!r}, "
            f"capability_count={self.capability_count!r})"
        )


@dataclass(frozen=True, repr=False)
class ClassificationCacheRecord:
    """Canonical successful classification paired with its complete key."""

    key: ClassificationCacheKey
    result_json: str = field(repr=False)
    result_digest: str = field(init=False)
    result_bytes: int = field(init=False)
    difficulty: ClassificationDifficulty = field(init=False)
    source: ClassificationResultSource = field(init=False)
    vision_required: bool = field(init=False)
    required_plugin_count: int = field(init=False)
    _required_plugins: tuple[str, ...] = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.key, ClassificationCacheKey):
            raise TypeError("ClassificationCacheRecord.key 必须是 ClassificationCacheKey")
        difficulty, vision_required, required_plugins, source = _decode_canonical_result(self.result_json)
        encoded = self.result_json.encode("utf-8")
        object.__setattr__(
            self,
            "result_digest",
            hashlib.sha256(encoded).hexdigest(),
        )
        object.__setattr__(self, "result_bytes", len(encoded))
        object.__setattr__(self, "difficulty", difficulty)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "vision_required", vision_required)
        object.__setattr__(
            self,
            "required_plugin_count",
            len(required_plugins),
        )
        object.__setattr__(self, "_required_plugins", required_plugins)

    @classmethod
    def from_result(
        cls,
        key: ClassificationCacheKey,
        *,
        difficulty: ClassificationDifficulty | str,
        vision_required: bool,
        required_plugins: list[str] | tuple[str, ...],
        source: ClassificationResultSource,
    ) -> ClassificationCacheRecord:
        if not isinstance(key, ClassificationCacheKey):
            raise TypeError("key 必须是 ClassificationCacheKey")
        if not isinstance(source, ClassificationResultSource):
            raise TypeError("source 必须是 ClassificationResultSource")
        if not source.cacheable:
            raise ClassificationCacheIneligibleError("只有成功模型 classification result 可以缓存")
        normalized_difficulty = ClassificationDifficulty.parse(difficulty)
        normalized_vision = _validate_boolean(
            vision_required,
            label="classification vision_required",
        )
        normalized_plugins = _canonical_plugins(required_plugins)
        return cls(
            key=key,
            result_json=_canonical_result_json(
                normalized_difficulty,
                normalized_vision,
                normalized_plugins,
                source,
            ),
        )

    def materialize(self) -> tuple[str, bool, list[str]]:
        return (
            self.difficulty.value,
            self.vision_required,
            list(self._required_plugins),
        )

    def __repr__(self) -> str:
        return (
            "ClassificationCacheRecord("
            f"key={self.key!r}, "
            f"result_digest={self.result_digest!r}, "
            f"result_bytes={self.result_bytes!r}, "
            f"required_plugin_count={self.required_plugin_count!r})"
        )


@runtime_checkable
class ClassificationCacheProtocol(Protocol):
    """Backend-neutral cache contract; only unexpired exact records may return."""

    async def lookup(
        self,
        key: ClassificationCacheKey,
    ) -> ClassificationCacheRecord | None: ...

    async def publish(
        self,
        record: ClassificationCacheRecord,
    ) -> ClassificationCacheRecord:
        """Publish only a fresh successful model classification."""
        ...


@dataclass(frozen=True)
class MemoryClassificationCacheSettings:
    """Bounded process-local classification cache policy."""

    max_entries: int = 1_024
    max_record_bytes: int = 65_536
    max_total_bytes: int = 8_388_608

    def __post_init__(self) -> None:
        _validate_positive_integer(
            self.max_entries,
            label="max_entries",
            maximum=65_536,
        )
        _validate_positive_integer(
            self.max_record_bytes,
            label="max_record_bytes",
            maximum=_MAX_RESULT_BYTES,
        )
        _validate_positive_integer(
            self.max_total_bytes,
            label="max_total_bytes",
            maximum=268_435_456,
        )
        if self.max_total_bytes < self.max_record_bytes:
            raise ValueError("max_total_bytes 不得小于 max_record_bytes")

    def safe_diagnostics(self) -> dict[str, int]:
        return {
            "max_entries": self.max_entries,
            "max_record_bytes": self.max_record_bytes,
            "max_total_bytes": self.max_total_bytes,
        }


@dataclass(frozen=True)
class _MemoryClassificationEntry:
    record: ClassificationCacheRecord
    expires_at: float


def _read_clock(clock: Clock) -> float:
    try:
        value = clock()
    except Exception as error:
        raise ClassificationCacheUnavailableError(f"classification cache 时钟不可用 ({type(error).__name__})") from None
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise ClassificationCacheUnavailableError("classification cache 时钟返回无效值")
    return float(value)


class MemoryClassificationCache:
    """Short-TTL/LRU cache bound to one PID and one running event loop."""

    def __init__(
        self,
        *,
        settings: MemoryClassificationCacheSettings | None = None,
        clock: Clock | None = None,
        pid_provider: PidProvider | None = None,
        loop_provider: LoopProvider | None = None,
    ) -> None:
        if settings is not None and not isinstance(
            settings,
            MemoryClassificationCacheSettings,
        ):
            raise TypeError("settings 必须是 MemoryClassificationCacheSettings")
        for dependency, label in (
            (clock, "clock"),
            (pid_provider, "pid_provider"),
            (loop_provider, "loop_provider"),
        ):
            if dependency is not None and not callable(dependency):
                raise TypeError(f"{label} 必须可调用")
        self._settings = MemoryClassificationCacheSettings() if settings is None else settings
        self._clock = clock or time.monotonic
        self._pid_provider = pid_provider or os.getpid
        self._loop_provider = loop_provider or asyncio.get_running_loop
        self._records: OrderedDict[
            ClassificationCacheKey,
            _MemoryClassificationEntry,
        ] = OrderedDict()
        self._total_bytes = 0
        self._owner_pid: int | None = None
        self._owner_loop: asyncio.AbstractEventLoop | None = None
        self._last_now: float | None = None
        self._lock = asyncio.Lock()

    @property
    def settings(self) -> MemoryClassificationCacheSettings:
        return self._settings

    def __repr__(self) -> str:
        return f"MemoryClassificationCache(entries={len(self._records)!r}, total_bytes={self._total_bytes!r})"

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
            raise ClassificationCacheUnavailableError(f"classification cache 无法确认进程身份 ({type(error).__name__})") from None
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise ClassificationCacheUnavailableError("classification cache 进程身份无效")
        try:
            loop = self._loop_provider()
        except Exception as error:
            raise ClassificationCacheUnavailableError(
                f"classification cache 无法确认 event loop ({type(error).__name__})"
            ) from None
        if not isinstance(loop, asyncio.AbstractEventLoop):
            raise ClassificationCacheUnavailableError("classification cache event loop 身份无效")

        if self._owner_pid is None:
            self._owner_pid = pid
            self._owner_loop = loop
            return
        if pid != self._owner_pid:
            raise ClassificationCacheOwnershipError("MemoryClassificationCache 不得跨进程复用")
        if loop is not self._owner_loop:
            raise ClassificationCacheOwnershipError("MemoryClassificationCache 不得跨 event loop 复用")

    def _now_locked(self) -> float:
        now = _read_clock(self._clock)
        if self._last_now is not None and now < self._last_now:
            raise ClassificationCacheUnavailableError("classification cache 单调时钟发生回退")
        self._last_now = now
        return now

    def _prune_locked(self, now: float) -> None:
        expired = [key for key, entry in self._records.items() if now >= entry.expires_at]
        for key in expired:
            entry = self._records.pop(key)
            self._total_bytes -= entry.record.result_bytes

    def _evict_locked(self) -> None:
        while len(self._records) > self._settings.max_entries or self._total_bytes > self._settings.max_total_bytes:
            _key, entry = self._records.popitem(last=False)
            self._total_bytes -= entry.record.result_bytes

    async def lookup(
        self,
        key: ClassificationCacheKey,
    ) -> ClassificationCacheRecord | None:
        if not isinstance(key, ClassificationCacheKey):
            raise TypeError("key 必须是 ClassificationCacheKey")
        self._claim_owner()
        async with self._lock:
            now = self._now_locked()
            self._prune_locked(now)
            entry = self._records.get(key)
            if entry is None:
                return None
            self._records.move_to_end(key)
            return entry.record

    async def publish(
        self,
        record: ClassificationCacheRecord,
    ) -> ClassificationCacheRecord:
        if not isinstance(record, ClassificationCacheRecord):
            raise TypeError("record 必须是 ClassificationCacheRecord")
        if record.result_bytes > self._settings.max_record_bytes:
            raise ClassificationCacheUnavailableError("classification cache record 超过配置大小上限")
        self._claim_owner()
        async with self._lock:
            now = self._now_locked()
            self._prune_locked(now)
            current = self._records.get(record.key)
            if current is not None:
                self._records.move_to_end(record.key)
                if current.record != record:
                    raise ClassificationCacheConflictError("相同 classification cache identity 产生不同结果")
                return current.record

            self._records[record.key] = _MemoryClassificationEntry(
                record=record,
                expires_at=now + record.key.ttl_seconds,
            )
            self._total_bytes += record.result_bytes
            self._evict_locked()
            return record

    async def clear(self) -> None:
        self._claim_owner()
        async with self._lock:
            self._records.clear()
            self._total_bytes = 0


async def resolve_classification(
    cache: ClassificationCacheProtocol,
    key: ClassificationCacheKey,
    builder: ClassificationBuilder,
) -> ClassificationCacheRecord:
    """Resolve an exact result without treating cache failure as a bypass."""

    if not isinstance(key, ClassificationCacheKey):
        raise TypeError("key 必须是 ClassificationCacheKey")
    if not callable(builder):
        raise TypeError("builder 必须可调用")
    cached = await cache.lookup(key)
    if cached is not None:
        if not isinstance(cached, ClassificationCacheRecord) or cached.key != key:
            raise ClassificationCacheUnavailableError("classification cache 返回了错误 identity")
        return cached

    pending = builder()
    if not inspect.isawaitable(pending):
        raise TypeError("classification builder 必须返回 awaitable")
    record = await pending
    if not isinstance(record, ClassificationCacheRecord):
        raise TypeError("classification builder 必须返回 ClassificationCacheRecord")
    if record.key != key:
        raise ValueError("classification builder 返回了错误 identity")
    published = await cache.publish(record)
    if not isinstance(published, ClassificationCacheRecord) or published != record:
        raise ClassificationCacheUnavailableError("classification cache 未确认精确发布结果")
    return published

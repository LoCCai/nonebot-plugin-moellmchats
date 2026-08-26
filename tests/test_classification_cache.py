from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError, replace
import hashlib
import json
import math
import os
import re
from typing import Any

import pytest

import nonebot_plugin_moellmchats.classification_cache as classification_cache_module
from nonebot_plugin_moellmchats.classification_cache import (
    ClassificationCacheConflictError,
    ClassificationCacheIneligibleError,
    ClassificationCacheKey,
    ClassificationCacheOwnershipError,
    ClassificationCacheProtocol,
    ClassificationCacheRecord,
    ClassificationCacheUnavailableError,
    ClassificationDifficulty,
    ClassificationModelIdentity,
    ClassificationRenderContext,
    ClassificationRequestScope,
    ClassificationResultSource,
    MemoryClassificationCache,
    MemoryClassificationCacheSettings,
    normalized_classification_prompt_hash,
    resolve_classification,
)
from nonebot_plugin_moellmchats.tool_catalog_cache import (
    ToolCatalogCacheKey,
    ToolCatalogPermission,
    ToolCatalogRecord,
    ToolCatalogRenderContext,
)


class MutableClock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _catalog(
    *,
    generation: int = 42,
    is_superuser: bool = False,
    provider_cutover: bool = True,
    tools_enabled: bool = True,
    web_search_enabled: bool = False,
    blacklist_patterns: tuple[str, ...] = (),
    catalog: str = "- alpha: alpha tool",
) -> ToolCatalogRecord:
    context = ToolCatalogRenderContext.capture(
        generation=generation,
        is_superuser=is_superuser,
        provider_cutover=provider_cutover,
        tools_enabled=tools_enabled,
        web_search_enabled=web_search_enabled,
        blacklist_patterns=blacklist_patterns,
    )
    return ToolCatalogRecord(context.cache_key, catalog)


def _model(
    *,
    model: str = "classifier-v1",
    endpoint: str = "https://classifier.invalid/v1/chat/completions",
    json_mode: bool = True,
    api_family: str = "openai-chat-completions",
) -> ClassificationModelIdentity:
    return ClassificationModelIdentity.capture(
        model=model,
        endpoint=endpoint,
        json_mode=json_mode,
        api_family=api_family,
    )


def _context(
    *,
    prompt: str = "今天天气怎么样？",
    catalog_record: ToolCatalogRecord | None = None,
    model_identity: ClassificationModelIdentity | None = None,
    request_scope: ClassificationRequestScope | None = None,
    policy_version: str = "categorize-json-v1",
    additional_capabilities: tuple[str, ...] = (),
    ttl_seconds: float = 60.0,
) -> ClassificationRenderContext:
    return ClassificationRenderContext.capture(
        prompt=prompt,
        catalog_record=_catalog() if catalog_record is None else catalog_record,
        model_identity=_model() if model_identity is None else model_identity,
        request_scope=(ClassificationRequestScope.standard_prompt() if request_scope is None else request_scope),
        policy_version=policy_version,
        additional_capabilities=additional_capabilities,
        ttl_seconds=ttl_seconds,
    )


def _record(
    *,
    context: ClassificationRenderContext | None = None,
    difficulty: ClassificationDifficulty | str = "1",
    vision_required: bool = False,
    required_plugins: list[str] | tuple[str, ...] = ("alpha",),
) -> ClassificationCacheRecord:
    resolved_context = _context() if context is None else context
    return ClassificationCacheRecord.from_result(
        resolved_context.cache_key,
        difficulty=difficulty,
        vision_required=vision_required,
        required_plugins=required_plugins,
        source=ClassificationResultSource.MODEL_SUCCESS,
    )


def _settings(**changes: int) -> MemoryClassificationCacheSettings:
    values = {
        "max_entries": 4,
        "max_record_bytes": 4_096,
        "max_total_bytes": 16_384,
    }
    values.update(changes)
    return MemoryClassificationCacheSettings(**values)


def _key_values() -> dict[str, Any]:
    return {
        "generation": 42,
        "permission": ToolCatalogPermission.USER,
        "provider_cutover": True,
        "tools_enabled": True,
        "web_search_enabled": False,
        "blacklist_digest": "a" * 64,
        "catalog_digest": "b" * 64,
        "normalized_prompt_hash": "c" * 64,
        "capability_digest": "d" * 64,
        "classifier_digest": "e" * 64,
        "policy_version": "categorize-json-v1",
        "ttl_seconds": 60.0,
    }


def test_prompt_normalization_is_minimal_stable_and_digest_only() -> None:
    private_prompt = "  Ｈｅｌｌｏ\tworld \n"
    context = _context(prompt=private_prompt)
    equivalent = _context(prompt="Hello world")
    case_sensitive = _context(prompt="hello world")

    assert context.cache_key == equivalent.cache_key
    assert context.cache_key != case_sensitive.cache_key
    assert context.cache_key.normalized_prompt_hash == (normalized_classification_prompt_hash(private_prompt))
    assert re.fullmatch(
        r"classification:42:[0-9a-f]{64}",
        context.cache_key.safe_cache_key,
    )
    assert private_prompt.strip() not in repr(context)
    assert "Hello world" not in repr(context.cache_key)
    assert "Hello world" not in context.cache_key.safe_cache_key


@pytest.mark.parametrize(
    ("prompt", "error", "match"),
    [
        (object(), TypeError, "字符串"),
        ("", ValueError, "不能为空"),
        (" \t\n ", ValueError, "不能为空"),
        ("bad\x00prompt", ValueError, "NUL"),
        ("\ud800", ValueError, "UTF-8"),
        ("x" * 131_073, ValueError, "字符"),
        ("界" * 87_382, ValueError, "字节"),
    ],
)
def test_prompt_normalization_rejects_unsafe_inputs(
    prompt: object,
    error: type[Exception],
    match: str,
) -> None:
    with pytest.raises(error, match=match):
        normalized_classification_prompt_hash(prompt)  # type: ignore[arg-type]


def test_model_identity_hides_raw_endpoint_and_separates_request_mode() -> None:
    private_endpoint = "https://private.invalid/v1/classify"
    identity = _model(endpoint=private_endpoint)

    assert re.fullmatch(r"[0-9a-f]{64}", identity.digest)
    assert private_endpoint not in repr(identity)
    assert identity != _model(endpoint=private_endpoint + "/other")
    assert identity != _model(endpoint=private_endpoint, model="classifier-v2")
    assert identity != _model(endpoint=private_endpoint, json_mode=False)
    assert identity != _model(
        endpoint=private_endpoint,
        api_family="vendor-json-v1",
    )


@pytest.mark.parametrize(
    ("changes", "error", "match"),
    [
        ({"model": ""}, ValueError, "model"),
        ({"model": " x"}, ValueError, "model"),
        ({"model": "bad\x00model"}, ValueError, "model"),
        ({"model": "\ud800"}, ValueError, "UTF-8"),
        ({"endpoint": ""}, ValueError, "endpoint"),
        ({"endpoint": "x" * 4_097}, ValueError, "endpoint"),
        ({"json_mode": 1}, TypeError, "json_mode"),
        ({"api_family": "bad family"}, ValueError, "api_family"),
    ],
)
def test_model_identity_rejects_unsafe_inputs(
    changes: dict[str, Any],
    error: type[Exception],
    match: str,
) -> None:
    values: dict[str, Any] = {
        "model": "classifier",
        "endpoint": "https://classifier.invalid",
        "json_mode": True,
        "api_family": "openai-chat-completions",
    }
    values.update(changes)
    with pytest.raises(error, match=match):
        ClassificationModelIdentity.capture(**values)


def test_request_scope_requires_every_context_dimension_to_be_clear() -> None:
    standard = ClassificationRequestScope.standard_prompt()
    assert standard.cacheable is True
    standard.require_cacheable()

    for field_name in (
        "conversation_bound",
        "attachment_bound",
        "actor_identity_bound",
        "session_state_bound",
        "external_state_bound",
    ):
        contextual = replace(standard, **{field_name: True})
        assert contextual.cacheable is False
        with pytest.raises(
            ClassificationCacheIneligibleError,
            match="上下文",
        ):
            contextual.require_cacheable()
        with pytest.raises(ClassificationCacheIneligibleError):
            _context(request_scope=contextual)


@pytest.mark.parametrize(
    "field_name",
    [
        "conversation_bound",
        "attachment_bound",
        "actor_identity_bound",
        "session_state_bound",
        "external_state_bound",
    ],
)
def test_request_scope_requires_strict_booleans(field_name: str) -> None:
    values: dict[str, object] = {
        "conversation_bound": False,
        "attachment_bound": False,
        "actor_identity_bound": False,
        "session_state_bound": False,
        "external_state_bound": False,
    }
    values[field_name] = 0
    with pytest.raises(TypeError, match=field_name):
        ClassificationRequestScope(**values)  # type: ignore[arg-type]


def test_context_binds_catalog_model_capability_policy_and_short_ttl() -> None:
    private_capability = "tenant:trusted-tools"
    context = _context(
        additional_capabilities=(private_capability,),
        ttl_seconds=45,
    )

    assert context.capability_count == 2
    assert context.cache_key.ttl_seconds == 45.0
    assert private_capability not in repr(context)
    assert private_capability not in context.cache_key.safe_cache_key
    assert context.safe_diagnostics() == {
        "generation": 42,
        "permission": "user",
        "policy_version": "categorize-json-v1",
        "provider_cutover": True,
        "tools_enabled": True,
        "ttl_seconds": 45.0,
        "web_search_enabled": False,
        "capability_count": 2,
        "context_independent": True,
        "normalized_prompt_bytes": len("今天天气怎么样?".encode()),
    }


def test_context_key_separates_every_dynamic_classification_input() -> None:
    base = _context()
    variants = (
        _context(prompt="明天天气怎么样？"),
        _context(catalog_record=_catalog(generation=43)),
        _context(catalog_record=_catalog(is_superuser=True)),
        _context(catalog_record=_catalog(provider_cutover=False)),
        _context(catalog_record=_catalog(tools_enabled=False)),
        _context(catalog_record=_catalog(web_search_enabled=True)),
        _context(catalog_record=_catalog(blacklist_patterns=("alpha",))),
        _context(catalog_record=_catalog(catalog="- beta: beta tool")),
        _context(model_identity=_model(model="classifier-v2")),
        _context(policy_version="categorize-json-v2"),
        _context(additional_capabilities=("tenant:trusted-tools",)),
        _context(ttl_seconds=30),
    )

    assert len({base.cache_key, *(item.cache_key for item in variants)}) == 13


def test_permission_is_always_part_of_capability_identity() -> None:
    user = _context(catalog_record=_catalog(is_superuser=False))
    superuser = _context(catalog_record=_catalog(is_superuser=True))

    assert user.capability_count == 1
    assert superuser.capability_count == 1
    assert user.cache_key.permission is ToolCatalogPermission.USER
    assert superuser.cache_key.permission is ToolCatalogPermission.SUPERUSER
    assert user.cache_key.capability_digest != (superuser.cache_key.capability_digest)


@pytest.mark.parametrize(
    ("capabilities", "error", "match"),
    [
        (["tenant:one"], TypeError, "元组"),
        (("",), ValueError, "capability token"),
        ((" tenant:one",), ValueError, "capability token"),
        (("tenant one",), ValueError, "capability token"),
        (("bad\x00token",), ValueError, "capability token"),
        (("x" * 129,), ValueError, "capability token"),
        (tuple(f"cap:{index}" for index in range(257)), ValueError, "数量"),
    ],
)
def test_context_rejects_unsafe_capabilities(
    capabilities: object,
    error: type[Exception],
    match: str,
) -> None:
    with pytest.raises(error, match=match):
        _context(additional_capabilities=capabilities)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("changes", "error", "match"),
    [
        ({"generation": -1}, ValueError, "generation"),
        ({"generation": True}, ValueError, "generation"),
        ({"permission": "user"}, TypeError, "permission"),
        ({"provider_cutover": 1}, TypeError, "provider_cutover"),
        ({"tools_enabled": 1}, TypeError, "tools_enabled"),
        ({"web_search_enabled": 0}, TypeError, "web_search_enabled"),
        ({"blacklist_digest": "bad"}, ValueError, "SHA-256"),
        ({"catalog_digest": "bad"}, ValueError, "SHA-256"),
        ({"normalized_prompt_hash": "bad"}, ValueError, "SHA-256"),
        ({"capability_digest": "bad"}, ValueError, "SHA-256"),
        ({"classifier_digest": "bad"}, ValueError, "SHA-256"),
        ({"policy_version": ""}, ValueError, "policy_version"),
        ({"policy_version": "bad version"}, ValueError, "policy_version"),
        ({"ttl_seconds": 0}, ValueError, "ttl_seconds"),
        ({"ttl_seconds": 300.1}, ValueError, "ttl_seconds"),
        ({"ttl_seconds": math.nan}, ValueError, "ttl_seconds"),
        ({"ttl_seconds": True}, ValueError, "ttl_seconds"),
    ],
)
def test_cache_key_rejects_incomplete_or_unsafe_identity(
    changes: dict[str, Any],
    error: type[Exception],
    match: str,
) -> None:
    values = _key_values()
    values.update(changes)
    with pytest.raises(error, match=match):
        ClassificationCacheKey(**values)


def test_context_requires_typed_dependencies() -> None:
    values: dict[str, Any] = {
        "prompt": "hello",
        "catalog_record": _catalog(),
        "model_identity": _model(),
        "request_scope": ClassificationRequestScope.standard_prompt(),
        "policy_version": "categorize-json-v1",
    }
    for field_name, value, match in (
        ("catalog_record", object(), "ToolCatalogRecord"),
        ("model_identity", object(), "ClassificationModelIdentity"),
        ("request_scope", object(), "ClassificationRequestScope"),
    ):
        candidate = dict(values)
        candidate[field_name] = value
        with pytest.raises(TypeError, match=match):
            ClassificationRenderContext.capture(**candidate)

    with pytest.raises(TypeError, match="ClassificationCacheKey"):
        ClassificationRenderContext(
            key=object(),  # type: ignore[arg-type]
            request_scope=ClassificationRequestScope.standard_prompt(),
            normalized_prompt_bytes=1,
            capability_count=1,
        )


def test_record_is_canonical_frozen_digest_bound_and_detached() -> None:
    context = _context()
    record = _record(
        context=context,
        difficulty=ClassificationDifficulty.HARD,
        vision_required=True,
        required_plugins=["beta", "alpha", "beta"],
    )
    expected = '{"difficulty":"2","required_plugins":["alpha","beta"],"source":"model_success","vision_required":true}'

    assert record.result_json == expected
    assert record.result_digest == hashlib.sha256(expected.encode()).hexdigest()
    assert record.result_bytes == len(expected.encode())
    assert record.difficulty is ClassificationDifficulty.HARD
    assert record.source is ClassificationResultSource.MODEL_SUCCESS
    assert record.vision_required is True
    assert record.required_plugin_count == 2
    assert record.materialize() == ("2", True, ["alpha", "beta"])
    first = record.materialize()
    first[2].append("tampered")
    assert record.materialize() == ("2", True, ["alpha", "beta"])
    assert "alpha" not in repr(record)
    assert "beta" not in repr(record)
    with pytest.raises(FrozenInstanceError):
        record.result_json = "{}"  # type: ignore[misc]


def test_record_supports_successful_empty_plugin_result() -> None:
    record = _record(
        difficulty="0",
        vision_required=False,
        required_plugins=[],
    )

    assert record.required_plugin_count == 0
    assert record.materialize() == ("0", False, [])


@pytest.mark.parametrize(
    ("changes", "error", "match"),
    [
        ({"difficulty": "3"}, ValueError, "difficulty"),
        ({"difficulty": False}, ValueError, "difficulty"),
        ({"vision_required": 1}, TypeError, "vision_required"),
        ({"required_plugins": {"alpha"}}, TypeError, "数组或元组"),
        ({"required_plugins": [""]}, ValueError, "安全工具名"),
        ({"required_plugins": [" alpha"]}, ValueError, "安全工具名"),
        ({"required_plugins": ["bad\nname"]}, ValueError, "安全工具名"),
        ({"required_plugins": ["x" * 513]}, ValueError, "安全工具名"),
        (
            {"required_plugins": [f"tool-{index}" for index in range(513)]},
            ValueError,
            "数量",
        ),
    ],
)
def test_record_factory_rejects_non_success_or_unsafe_results(
    changes: dict[str, Any],
    error: type[Exception],
    match: str,
) -> None:
    values: dict[str, Any] = {
        "difficulty": "1",
        "vision_required": False,
        "required_plugins": ["alpha"],
    }
    values.update(changes)
    with pytest.raises(error, match=match):
        ClassificationCacheRecord.from_result(
            _context().cache_key,
            source=ClassificationResultSource.MODEL_SUCCESS,
            **values,
        )


@pytest.mark.parametrize(
    "source",
    [
        ClassificationResultSource.TIMEOUT_FALLBACK,
        ClassificationResultSource.PARSE_FALLBACK,
        ClassificationResultSource.CONTENT_BLOCKED,
    ],
)
def test_record_factory_rejects_transient_or_blocked_results(
    source: ClassificationResultSource,
) -> None:
    with pytest.raises(
        ClassificationCacheIneligibleError,
        match="成功模型",
    ):
        ClassificationCacheRecord.from_result(
            _context().cache_key,
            difficulty="1",
            vision_required=False,
            required_plugins=[],
            source=source,
        )


def test_record_factory_requires_typed_source() -> None:
    with pytest.raises(TypeError, match="ClassificationResultSource"):
        ClassificationCacheRecord.from_result(
            _context().cache_key,
            difficulty="1",
            vision_required=False,
            required_plugins=[],
            source="model_success",  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        ("", "非法"),
        ("[]", "字段集合"),
        (
            '{"difficulty":"1","difficulty":"2","required_plugins":[],"source":"model_success","vision_required":false}',
            "重复字段",
        ),
        (
            '{"difficulty":"1","required_plugins":[],"source":"model_success","vision_required":false,"extra":true}',
            "字段集合",
        ),
        (
            '{"difficulty":"9","required_plugins":[],"source":"model_success","vision_required":false}',
            "difficulty",
        ),
        (
            '{"difficulty":"1","required_plugins":[],"source":"model_success","vision_required":0}',
            "vision_required",
        ),
        (
            '{"difficulty":"1","required_plugins":[1],"source":"model_success","vision_required":false}',
            "安全工具名",
        ),
        (
            '{"difficulty":"1","required_plugins":["beta","alpha"],"source":"model_success","vision_required":false}',
            "canonical",
        ),
        (
            '{ "difficulty":"1","required_plugins":[],"source":"model_success","vision_required":false}',
            "canonical",
        ),
        (
            '{"difficulty":"1","required_plugins":[],"source":"timeout_fallback","vision_required":false}',
            "成功模型",
        ),
    ],
)
def test_record_constructor_rejects_ambiguous_or_noncanonical_json(
    payload: str,
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        ClassificationCacheRecord(
            key=_context().cache_key,
            result_json=payload,
        )


def test_record_requires_typed_key() -> None:
    with pytest.raises(TypeError, match="ClassificationCacheKey"):
        ClassificationCacheRecord(
            key=object(),  # type: ignore[arg-type]
            result_json=('{"difficulty":"1","required_plugins":[],"source":"model_success","vision_required":false}'),
        )


def test_memory_settings_are_bounded_and_safe() -> None:
    settings = _settings(
        max_entries=7,
        max_record_bytes=2_048,
        max_total_bytes=8_192,
    )
    assert settings.safe_diagnostics() == {
        "max_entries": 7,
        "max_record_bytes": 2_048,
        "max_total_bytes": 8_192,
    }


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("max_entries", 0),
        ("max_entries", True),
        ("max_entries", 65_537),
        ("max_record_bytes", 0),
        ("max_record_bytes", 1_048_577),
        ("max_total_bytes", 0),
        ("max_total_bytes", 268_435_457),
    ],
)
def test_memory_settings_reject_invalid_values(
    field_name: str,
    value: object,
) -> None:
    with pytest.raises(ValueError, match=field_name):
        _settings(**{field_name: value})  # type: ignore[arg-type]


def test_memory_settings_require_total_capacity_for_one_record() -> None:
    with pytest.raises(ValueError, match="max_total_bytes"):
        _settings(max_record_bytes=2_048, max_total_bytes=1_024)


def test_memory_cache_requires_typed_dependencies() -> None:
    with pytest.raises(TypeError, match="MemoryClassificationCacheSettings"):
        MemoryClassificationCache(settings=object())  # type: ignore[arg-type]
    for field_name in ("clock", "pid_provider", "loop_provider"):
        with pytest.raises(TypeError, match=field_name):
            MemoryClassificationCache(
                **{field_name: object()}  # type: ignore[arg-type]
            )


@pytest.mark.asyncio
async def test_memory_cache_miss_publish_hit_and_safe_diagnostics() -> None:
    cache = MemoryClassificationCache(settings=_settings())
    record = _record()

    assert isinstance(cache, ClassificationCacheProtocol)
    assert await cache.lookup(record.key) is None
    assert await cache.publish(record) is record
    assert await cache.lookup(record.key) is record
    assert await cache.publish(record) is record
    assert cache.safe_diagnostics() == {
        "backend": "memory",
        "configured": True,
        "max_entries": 4,
        "max_record_bytes": 4_096,
        "max_total_bytes": 16_384,
    }
    assert "alpha" not in repr(cache)


@pytest.mark.asyncio
async def test_memory_cache_ttl_is_short_fixed_and_not_refreshed_by_hits() -> None:
    clock = MutableClock()
    context = _context(ttl_seconds=60)
    record = _record(context=context)
    cache = MemoryClassificationCache(settings=_settings(), clock=clock)

    await cache.publish(record)
    clock.value = 59.999
    assert await cache.lookup(record.key) is record
    assert await cache.publish(record) is record
    clock.value = 60.0
    assert await cache.lookup(record.key) is None


@pytest.mark.asyncio
async def test_memory_cache_accepts_new_result_only_after_expiry() -> None:
    clock = MutableClock()
    context = _context(ttl_seconds=10)
    first = _record(context=context, difficulty="0")
    second = _record(context=context, difficulty="2")
    cache = MemoryClassificationCache(settings=_settings(), clock=clock)

    await cache.publish(first)
    with pytest.raises(ClassificationCacheConflictError, match="不同结果"):
        await cache.publish(second)
    clock.value = 10
    assert await cache.publish(second) is second
    assert await cache.lookup(second.key) is second


@pytest.mark.asyncio
async def test_memory_cache_lru_eviction_honors_recent_lookup() -> None:
    cache = MemoryClassificationCache(
        settings=_settings(max_entries=2),
    )
    first = _record(context=_context(prompt="first"))
    second = _record(context=_context(prompt="second"))
    third = _record(context=_context(prompt="third"))

    await cache.publish(first)
    await cache.publish(second)
    assert await cache.lookup(first.key) is first
    await cache.publish(third)

    assert await cache.lookup(first.key) is first
    assert await cache.lookup(second.key) is None
    assert await cache.lookup(third.key) is third


@pytest.mark.asyncio
async def test_memory_cache_total_bytes_evicts_oldest_record() -> None:
    first = _record(
        context=_context(prompt="first"),
        required_plugins=["alpha"],
    )
    second = _record(
        context=_context(prompt="second"),
        required_plugins=["beta"],
    )
    maximum = max(first.result_bytes, second.result_bytes)
    cache = MemoryClassificationCache(
        settings=_settings(
            max_record_bytes=maximum,
            max_total_bytes=first.result_bytes + second.result_bytes - 1,
        )
    )

    await cache.publish(first)
    await cache.publish(second)

    assert await cache.lookup(first.key) is None
    assert await cache.lookup(second.key) is second


@pytest.mark.asyncio
async def test_memory_cache_rejects_record_above_configured_limit() -> None:
    record = _record(required_plugins=["alpha"])
    cache = MemoryClassificationCache(
        settings=_settings(
            max_record_bytes=record.result_bytes - 1,
            max_total_bytes=record.result_bytes,
        )
    )

    with pytest.raises(
        ClassificationCacheUnavailableError,
        match="大小",
    ):
        await cache.publish(record)
    assert await cache.lookup(record.key) is None


@pytest.mark.asyncio
async def test_memory_cache_clear_discards_all_records() -> None:
    cache = MemoryClassificationCache(settings=_settings())
    first = _record(context=_context(prompt="first"))
    second = _record(context=_context(prompt="second"))
    await cache.publish(first)
    await cache.publish(second)

    await cache.clear()

    assert await cache.lookup(first.key) is None
    assert await cache.lookup(second.key) is None


@pytest.mark.asyncio
async def test_memory_cache_rejects_cross_pid_reuse() -> None:
    owner = [os.getpid()]
    cache = MemoryClassificationCache(
        settings=_settings(),
        pid_provider=lambda: owner[0],
    )
    record = _record()
    await cache.publish(record)

    owner[0] += 1
    with pytest.raises(
        ClassificationCacheOwnershipError,
        match="跨进程",
    ):
        await cache.lookup(record.key)


@pytest.mark.asyncio
async def test_memory_cache_rejects_cross_loop_reuse() -> None:
    first_loop = asyncio.new_event_loop()
    second_loop = asyncio.new_event_loop()
    selected = [first_loop]
    cache = MemoryClassificationCache(
        settings=_settings(),
        loop_provider=lambda: selected[0],
    )
    try:
        record = _record()
        await cache.publish(record)
        selected[0] = second_loop
        with pytest.raises(
            ClassificationCacheOwnershipError,
            match="event loop",
        ):
            await cache.lookup(record.key)
    finally:
        first_loop.close()
        second_loop.close()


@pytest.mark.asyncio
async def test_memory_cache_fails_closed_on_invalid_owner_identity() -> None:
    record = _record()
    for cache, match in (
        (
            MemoryClassificationCache(
                settings=_settings(),
                pid_provider=lambda: 0,
            ),
            "进程身份",
        ),
        (
            MemoryClassificationCache(
                settings=_settings(),
                loop_provider=lambda: object(),  # type: ignore[arg-type]
            ),
            "event loop",
        ),
        (
            MemoryClassificationCache(
                settings=_settings(),
                pid_provider=lambda: (_ for _ in ()).throw(RuntimeError("private")),
            ),
            "无法确认进程",
        ),
    ):
        with pytest.raises(ClassificationCacheUnavailableError, match=match):
            await cache.lookup(record.key)


@pytest.mark.asyncio
async def test_memory_cache_fails_closed_on_invalid_or_regressing_clock() -> None:
    record = _record()
    for value in (math.nan, math.inf, True, "now"):
        cache = MemoryClassificationCache(
            settings=_settings(),
            clock=lambda value=value: value,  # type: ignore[return-value]
        )
        with pytest.raises(
            ClassificationCacheUnavailableError,
            match="时钟",
        ):
            await cache.lookup(record.key)

    clock = MutableClock(5)
    cache = MemoryClassificationCache(settings=_settings(), clock=clock)
    await cache.publish(record)
    clock.value = 4
    with pytest.raises(
        ClassificationCacheUnavailableError,
        match="回退",
    ):
        await cache.lookup(record.key)


@pytest.mark.asyncio
async def test_memory_cache_rejects_invalid_api_types() -> None:
    cache = MemoryClassificationCache(settings=_settings())
    with pytest.raises(TypeError, match="ClassificationCacheKey"):
        await cache.lookup(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="ClassificationCacheRecord"):
        await cache.publish(object())  # type: ignore[arg-type]


class StubCache:
    def __init__(
        self,
        lookup_value: object = None,
        publish_value: object = None,
    ) -> None:
        self.lookup_value = lookup_value
        self.publish_value = publish_value
        self.lookups: list[ClassificationCacheKey] = []
        self.published: list[ClassificationCacheRecord] = []

    async def lookup(
        self,
        key: ClassificationCacheKey,
    ) -> ClassificationCacheRecord | None:
        self.lookups.append(key)
        return self.lookup_value  # type: ignore[return-value]

    async def publish(
        self,
        record: ClassificationCacheRecord,
    ) -> ClassificationCacheRecord:
        self.published.append(record)
        if self.publish_value is None:
            return record
        return self.publish_value  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_resolver_returns_exact_hit_without_calling_builder() -> None:
    record = _record()
    cache = StubCache(lookup_value=record)

    async def builder() -> ClassificationCacheRecord:
        raise AssertionError("cache hit must not build")

    assert await resolve_classification(cache, record.key, builder) is record
    assert cache.lookups == [record.key]
    assert cache.published == []


@pytest.mark.asyncio
async def test_resolver_builds_and_publishes_exact_miss() -> None:
    record = _record()
    cache = StubCache()
    calls = 0

    async def builder() -> ClassificationCacheRecord:
        nonlocal calls
        calls += 1
        return record

    assert await resolve_classification(cache, record.key, builder) is record
    assert calls == 1
    assert cache.lookups == [record.key]
    assert cache.published == [record]


@pytest.mark.asyncio
async def test_resolver_rejects_wrong_cached_type_or_identity() -> None:
    expected = _record(context=_context(prompt="expected"))
    wrong = _record(context=_context(prompt="wrong"))

    async def builder() -> ClassificationCacheRecord:
        return expected

    for value in (object(), wrong):
        with pytest.raises(
            ClassificationCacheUnavailableError,
            match="identity",
        ):
            await resolve_classification(
                StubCache(lookup_value=value),
                expected.key,
                builder,
            )


@pytest.mark.asyncio
async def test_resolver_requires_async_typed_exact_builder() -> None:
    record = _record()
    cache = StubCache()

    with pytest.raises(TypeError, match="builder"):
        await resolve_classification(
            cache,
            record.key,
            object(),  # type: ignore[arg-type]
        )

    def sync_builder() -> ClassificationCacheRecord:
        return record

    with pytest.raises(TypeError, match="awaitable"):
        await resolve_classification(cache, record.key, sync_builder)  # type: ignore[arg-type]

    async def wrong_type() -> object:
        return object()

    with pytest.raises(TypeError, match="ClassificationCacheRecord"):
        await resolve_classification(cache, record.key, wrong_type)  # type: ignore[arg-type]

    wrong_identity = _record(context=_context(prompt="wrong"))

    async def wrong_key() -> ClassificationCacheRecord:
        return wrong_identity

    with pytest.raises(ValueError, match="identity"):
        await resolve_classification(cache, record.key, wrong_key)


@pytest.mark.asyncio
async def test_resolver_requires_exact_publish_acknowledgement() -> None:
    record = _record()

    async def builder() -> ClassificationCacheRecord:
        return record

    for value in (
        object(),
        _record(context=_context(prompt="different")),
    ):
        with pytest.raises(
            ClassificationCacheUnavailableError,
            match="精确发布",
        ):
            await resolve_classification(
                StubCache(publish_value=value),
                record.key,
                builder,
            )


@pytest.mark.asyncio
async def test_resolver_propagates_builder_failure_without_publish() -> None:
    record = _record()
    cache = StubCache()

    async def builder() -> ClassificationCacheRecord:
        raise RuntimeError("classification failed")

    with pytest.raises(RuntimeError, match="classification failed"):
        await resolve_classification(cache, record.key, builder)
    assert cache.published == []


@pytest.mark.asyncio
async def test_resolver_propagates_cancellation_without_publish() -> None:
    record = _record()
    cache = StubCache()

    async def builder() -> ClassificationCacheRecord:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await resolve_classification(cache, record.key, builder)
    assert cache.published == []


def test_module_has_no_implicit_global_cache_instance() -> None:
    assert not any(isinstance(value, MemoryClassificationCache) for value in vars(classification_cache_module).values())


def test_catalog_record_identity_is_copied_without_raw_catalog() -> None:
    private_catalog = "- private_tool: internal description"
    catalog = _catalog(
        generation=7,
        is_superuser=True,
        provider_cutover=False,
        tools_enabled=True,
        web_search_enabled=True,
        blacklist_patterns=("private_tool",),
        catalog=private_catalog,
    )
    context = _context(catalog_record=catalog)
    key = context.cache_key

    assert key.generation == 7
    assert key.permission is ToolCatalogPermission.SUPERUSER
    assert key.provider_cutover is False
    assert key.tools_enabled is True
    assert key.web_search_enabled is True
    assert key.blacklist_digest == catalog.key.blacklist_digest
    assert key.catalog_digest == catalog.catalog_digest
    assert private_catalog not in repr(context)
    assert "private_tool" not in key.safe_cache_key


def test_direct_catalog_key_cannot_replace_verified_catalog_record() -> None:
    catalog_key = ToolCatalogCacheKey(
        generation=1,
        permission=ToolCatalogPermission.USER,
        provider_cutover=True,
        tools_enabled=True,
        web_search_enabled=False,
        blacklist_digest="a" * 64,
    )
    with pytest.raises(TypeError, match="ToolCatalogRecord"):
        _context(catalog_record=catalog_key)  # type: ignore[arg-type]


def test_result_json_is_standard_library_round_trip_safe() -> None:
    record = _record(
        difficulty="2",
        vision_required=True,
        required_plugins=["工具_alpha", "mcp__browser__open"],
    )
    decoded = json.loads(record.result_json)

    assert decoded == {
        "difficulty": "2",
        "required_plugins": ["mcp__browser__open", "工具_alpha"],
        "source": "model_success",
        "vision_required": True,
    }
    assert record.materialize() == (
        "2",
        True,
        ["mcp__browser__open", "工具_alpha"],
    )

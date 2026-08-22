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

from nonebot_plugin_moellmchats.builtin_tools import builtin_tool_specs
from nonebot_plugin_moellmchats.model_selector import model_selector
from nonebot_plugin_moellmchats.tool_catalog_cache import ToolCatalogPermission
from nonebot_plugin_moellmchats.tool_contracts import ToolPolicy, ToolSpec
from nonebot_plugin_moellmchats.tool_manager import (
    ProviderConsumerParityError,
    ToolManager,
    ToolSnapshot,
    tool_manager,
)
from nonebot_plugin_moellmchats.tool_providers import (
    DiscoveredTool,
    ProviderCatalogSnapshot,
    ProviderDiscoveryBatch,
    ProviderRegistration,
    builtin_tool_provider,
    file_tool_provider,
    generated_tool_provider,
    mcp_tool_provider,
    nonebot_plugin_provider,
    provider_registry,
    registered_tool_provider,
)
from nonebot_plugin_moellmchats.tool_schema_cache import (
    MemoryToolSchemaCache,
    MemoryToolSchemaCacheSettings,
    ToolSchemaCacheConflictError,
    ToolSchemaCacheKey,
    ToolSchemaCacheOwnershipError,
    ToolSchemaCacheProtocol,
    ToolSchemaCacheUnavailableError,
    ToolSchemaRecord,
    ToolSchemaRenderContext,
    resolve_tool_schema,
)


async def _handler(value: str = "ok") -> str:
    return value


def _context(
    *,
    generation: int = 42,
    selected_plugins: set[str] | None = None,
    is_superuser: bool = False,
    provider_cutover: bool = True,
    tools_enabled: bool = True,
    search_enabled: bool = False,
    blacklist_patterns: tuple[str, ...] = (),
) -> ToolSchemaRenderContext:
    return ToolSchemaRenderContext.capture(
        generation=generation,
        selected_plugins={"alpha"} if selected_plugins is None else selected_plugins,
        is_superuser=is_superuser,
        provider_cutover=provider_cutover,
        tools_enabled=tools_enabled,
        search_enabled=search_enabled,
        blacklist_patterns=blacklist_patterns,
    )


def _schema(name: str, *, description: str | None = None) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description or f"{name} description",
            "parameters": {
                "type": "object",
                "properties": {
                    "value": {
                        "type": "string",
                        "description": "value",
                    }
                },
                "required": [],
            },
        },
    }


def _record(
    *,
    generation: int = 42,
    selected_plugins: set[str] | None = None,
    schema_names: tuple[str, ...] = ("alpha",),
    **context_changes: Any,
) -> ToolSchemaRecord:
    selected = {"alpha"} if selected_plugins is None else selected_plugins
    context = _context(
        generation=generation,
        selected_plugins=selected,
        **context_changes,
    )
    return ToolSchemaRecord.from_schema(
        context.cache_key,
        selected,
        [_schema(name) for name in schema_names],
    )


def _settings(**changes: int) -> MemoryToolSchemaCacheSettings:
    values = {
        "max_entries": 4,
        "max_record_bytes": 4_096,
        "max_total_bytes": 16_384,
    }
    values.update(changes)
    return MemoryToolSchemaCacheSettings(**values)


def _provider_catalog(
    specs: tuple[ToolSpec, ...],
    *,
    generation: int,
) -> ProviderCatalogSnapshot:
    batches: list[ProviderDiscoveryBatch] = []
    for provider, provider_specs in (
        (registered_tool_provider, specs),
        (file_tool_provider, ()),
        (generated_tool_provider, ()),
        (mcp_tool_provider, ()),
        (builtin_tool_provider, builtin_tool_specs()),
        (nonebot_plugin_provider, ()),
    ):
        registration = ProviderRegistration.from_provider(provider)
        records = tuple(
            DiscoveredTool(
                provider_id=registration.provider_id,
                source=registration.source,
                trust=registration.trust,
                generation=generation,
                spec=spec,
            )
            for spec in provider_specs
        )
        batches.append(
            ProviderDiscoveryBatch(
                registration=registration,
                generation=generation,
                tools=records,
            )
        )
    return provider_registry.build_snapshot(generation, tuple(batches))


def _tool_snapshot(*, generation: int = 42) -> ToolSnapshot:
    dependency = ToolSpec(
        name="dependency_tool",
        description="dependency tool",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
        policy=ToolPolicy.configured(),
    )
    root = ToolSpec(
        name="root_tool",
        description="root tool",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
        dependencies=(dependency.name,),
        policy=ToolPolicy.configured(),
    )
    admin = ToolSpec(
        name="admin_tool",
        description="admin tool",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
        permission="superuser",
        policy=ToolPolicy.configured(),
    )
    specs = (root, dependency, admin)
    return ToolSnapshot(
        generation=generation,
        plugin_info={},
        custom_tools={
            spec.name: {
                **spec.as_legacy_schema(),
                "source": "registered",
            }
            for spec in specs
        },
        tool_dependencies={root.name: {dependency.name}},
        mcp_tool_names=set(),
        provider_catalog=_provider_catalog(specs, generation=generation),
    )


def test_context_canonicalizes_toolset_and_hides_raw_inputs_from_safe_key() -> None:
    private_tool = "private_tool"
    private_pattern = " mcp__private__* "
    context = ToolSchemaRenderContext(
        generation=42,
        permission=ToolCatalogPermission.USER,
        provider_cutover=True,
        tools_enabled=True,
        search_enabled=False,
        selected_plugins=(private_tool, "alpha", private_tool),
        blacklist_patterns=(private_pattern, "alpha", "alpha", "  "),
    )

    assert context.selected_plugins == ("alpha", private_tool)
    assert context.blacklist_patterns == ("alpha", "mcp__private__*")
    assert re.fullmatch(r"[0-9a-f]{64}", context.selected_plugins_digest)
    assert re.fullmatch(r"[0-9a-f]{64}", context.blacklist_digest)
    assert private_tool not in repr(context)
    assert "private" not in context.cache_key.safe_cache_key
    assert re.fullmatch(r"schema:42:[0-9a-f]{64}", context.cache_key.safe_cache_key)


def test_cache_key_separates_every_dynamic_schema_input() -> None:
    base = _context()
    variants = (
        replace(base, generation=43),
        replace(base, permission=ToolCatalogPermission.SUPERUSER),
        replace(base, provider_cutover=False),
        replace(base, tools_enabled=False),
        replace(base, search_enabled=True),
        replace(base, selected_plugins=("beta",)),
        replace(base, blacklist_patterns=("alpha",)),
    )

    assert len({base.cache_key, *(item.cache_key for item in variants)}) == 8


def test_context_blacklist_matches_runtime_semantics() -> None:
    context = _context(
        blacklist_patterns=(
            "exact_tool",
            "mcp__filesystem__*",
            "mcp__browser",
        )
    )

    assert context.is_blacklisted("exact_tool") is True
    assert context.is_blacklisted("mcp__filesystem__read_file") is True
    assert context.is_blacklisted("mcp__browser__open") is True
    assert context.is_blacklisted("mcp__other__open") is False
    with pytest.raises(ValueError, match="安全工具名"):
        context.is_blacklisted("")


@pytest.mark.parametrize(
    ("changes", "error", "match"),
    [
        ({"generation": -1}, ValueError, "generation"),
        ({"generation": True}, ValueError, "generation"),
        ({"permission": "root"}, TypeError, "permission"),
        ({"provider_cutover": 1}, TypeError, "provider_cutover"),
        ({"tools_enabled": 1}, TypeError, "tools_enabled"),
        ({"search_enabled": 0}, TypeError, "search_enabled"),
        ({"selected_plugins": ["alpha"]}, TypeError, "元组"),
        ({"selected_plugins": ("",)}, ValueError, "安全工具名"),
        ({"selected_plugins": (" alpha",)}, ValueError, "安全工具名"),
        ({"selected_plugins": ("bad\x00name",)}, ValueError, "安全工具名"),
        ({"blacklist_patterns": ["alpha"]}, TypeError, "元组"),
    ],
)
def test_context_rejects_incomplete_or_unsafe_identity(
    changes: dict[str, Any],
    error: type[Exception],
    match: str,
) -> None:
    values: dict[str, Any] = {
        "generation": 42,
        "permission": ToolCatalogPermission.USER,
        "provider_cutover": True,
        "tools_enabled": True,
        "search_enabled": False,
        "selected_plugins": ("alpha",),
        "blacklist_patterns": (),
    }
    values.update(changes)
    with pytest.raises(error, match=match):
        ToolSchemaRenderContext(**values)


def test_context_capture_requires_typed_set_and_boolean_actor() -> None:
    with pytest.raises(TypeError, match="字符串集合"):
        ToolSchemaRenderContext.capture(
            generation=1,
            selected_plugins=["alpha"],  # type: ignore[arg-type]
            is_superuser=False,
            provider_cutover=True,
            tools_enabled=True,
            search_enabled=False,
            blacklist_patterns=(),
        )
    with pytest.raises(TypeError, match="is_superuser"):
        ToolSchemaRenderContext.capture(
            generation=1,
            selected_plugins={"alpha"},
            is_superuser=1,  # type: ignore[arg-type]
            provider_cutover=True,
            tools_enabled=True,
            search_enabled=False,
            blacklist_patterns=(),
        )


def test_context_rejects_excessive_selected_tool_count() -> None:
    with pytest.raises(ValueError, match="数量"):
        ToolSchemaRenderContext(
            generation=1,
            permission=ToolCatalogPermission.USER,
            provider_cutover=True,
            tools_enabled=True,
            search_enabled=False,
            selected_plugins=tuple(f"tool-{index}" for index in range(4_097)),
            blacklist_patterns=(),
        )


@pytest.mark.parametrize(
    ("changes", "error", "match"),
    [
        ({"generation": -1}, ValueError, "generation"),
        ({"permission": "admin"}, TypeError, "permission"),
        ({"provider_cutover": 1}, TypeError, "provider_cutover"),
        ({"tools_enabled": 1}, TypeError, "tools_enabled"),
        ({"search_enabled": 0}, TypeError, "search_enabled"),
        ({"blacklist_digest": "bad"}, ValueError, "SHA-256"),
        ({"selected_plugins_digest": "bad"}, ValueError, "SHA-256"),
    ],
)
def test_cache_key_rejects_invalid_fields(
    changes: dict[str, Any],
    error: type[Exception],
    match: str,
) -> None:
    values: dict[str, Any] = {
        "generation": 42,
        "permission": ToolCatalogPermission.USER,
        "provider_cutover": True,
        "tools_enabled": True,
        "search_enabled": False,
        "blacklist_digest": "a" * 64,
        "selected_plugins_digest": "b" * 64,
    }
    values.update(changes)
    with pytest.raises(error, match=match):
        ToolSchemaCacheKey(**values)


def test_schema_record_is_canonical_frozen_and_materializes_detached_values() -> None:
    context = _context(selected_plugins={"alpha", "beta"})
    schema = [_schema("beta"), _schema("alpha")]
    record = ToolSchemaRecord.from_schema(
        context.cache_key,
        {"alpha", "beta"},
        schema,
    )

    assert record.expanded_plugins == ("alpha", "beta")
    assert record.tool_names == ("beta", "alpha")
    assert record.tool_count == 2
    assert record.schema_digest == hashlib.sha256(record.schema_json.encode("utf-8")).hexdigest()
    assert record.schema_bytes == len(record.schema_json.encode("utf-8"))
    assert record.schema_json == json.dumps(
        schema,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    assert "alpha" not in repr(record)
    assert "description" not in repr(record)
    with pytest.raises(FrozenInstanceError):
        record.schema_json = "[]"  # type: ignore[misc]

    names, first = record.materialize()
    names.add("tampered")
    first[0]["function"]["description"] = "tampered"
    second_names, second = record.materialize()
    assert "tampered" not in second_names
    assert second[0]["function"]["description"] != "tampered"


def test_schema_record_supports_safe_empty_schema() -> None:
    context = _context(
        selected_plugins={"alpha"},
        tools_enabled=False,
    )
    record = ToolSchemaRecord.from_schema(context.cache_key, {"alpha"}, [])

    assert record.schema_json == "[]"
    assert record.tool_count == 0
    assert record.materialize() == ({"alpha"}, [])


@pytest.mark.parametrize(
    ("schema", "expanded", "match"),
    [
        ({}, {"alpha"}, "顶层"),
        ([{"type": "other", "function": {}}], {"alpha"}, "function 对象"),
        ([{"type": "function", "function": {}}], {"alpha"}, "安全工具名"),
        ([_schema("alpha"), _schema("alpha")], {"alpha"}, "重复工具名"),
        ([_schema("beta")], {"alpha"}, "expanded_plugins"),
    ],
)
def test_schema_record_rejects_invalid_payloads(
    schema: Any,
    expanded: set[str],
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        ToolSchemaRecord.from_schema(_context().cache_key, expanded, schema)


def test_schema_record_rejects_non_json_nul_and_nonfinite_values() -> None:
    invalid_values = (
        object(),
        "bad\x00value",
        math.inf,
        math.nan,
    )
    for value in invalid_values:
        schema = _schema("alpha")
        schema["function"]["parameters"]["properties"]["value"]["default"] = value
        with pytest.raises(ValueError, match=r"不可序列化|NUL|非有限"):
            ToolSchemaRecord.from_schema(
                _context().cache_key,
                {"alpha"},
                [schema],
            )


def test_schema_record_rejects_noncanonical_or_duplicate_json() -> None:
    key = _context().cache_key
    with pytest.raises(ValueError, match="canonical"):
        ToolSchemaRecord(key, ("alpha",), "[ ]")
    duplicate = (
        '[{"type":"function","function":{"name":"alpha",'
        '"name":"alpha","description":"alpha",'
        '"parameters":{"type":"object","properties":{}}}}]'
    )
    with pytest.raises(ValueError, match="非法"):
        ToolSchemaRecord(key, ("alpha",), duplicate)


def test_schema_record_rejects_excessive_json_depth() -> None:
    nested: dict[str, Any] = {"type": "string"}
    for _index in range(70):
        nested = {"type": "object", "properties": {"child": nested}}
    schema = _schema("alpha")
    schema["function"]["parameters"] = nested
    with pytest.raises(ValueError, match="嵌套"):
        ToolSchemaRecord.from_schema(
            _context().cache_key,
            {"alpha"},
            [schema],
        )


def test_schema_record_requires_typed_inputs() -> None:
    with pytest.raises(TypeError, match="ToolSchemaCacheKey"):
        ToolSchemaRecord(object(), ("alpha",), "[]")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="元组"):
        ToolSchemaRecord(_context().cache_key, ["alpha"], "[]")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="字符串集合"):
        ToolSchemaRecord.from_schema(
            _context().cache_key,
            ["alpha"],  # type: ignore[arg-type]
            [_schema("alpha")],
        )


def test_memory_settings_are_bounded_and_safe() -> None:
    settings = _settings(
        max_entries=7,
        max_record_bytes=8_192,
        max_total_bytes=32_768,
    )
    assert settings.safe_diagnostics() == {
        "max_entries": 7,
        "max_record_bytes": 8_192,
        "max_total_bytes": 32_768,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_entries", 0),
        ("max_entries", True),
        ("max_entries", 65_537),
        ("max_record_bytes", 0),
        ("max_record_bytes", 17_825_793),
        ("max_total_bytes", 0),
        ("max_total_bytes", 536_870_913),
    ],
)
def test_memory_settings_reject_invalid_values(field: str, value: object) -> None:
    with pytest.raises(ValueError, match=field):
        _settings(**{field: value})  # type: ignore[arg-type]


def test_memory_settings_require_total_capacity_for_one_record() -> None:
    with pytest.raises(ValueError, match="max_total_bytes"):
        _settings(max_record_bytes=8_192, max_total_bytes=4_096)


def test_memory_cache_requires_typed_dependencies() -> None:
    with pytest.raises(TypeError, match="MemoryToolSchemaCacheSettings"):
        MemoryToolSchemaCache(settings=object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="pid_provider"):
        MemoryToolSchemaCache(pid_provider=object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="loop_provider"):
        MemoryToolSchemaCache(loop_provider=object())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_memory_cache_miss_publish_hit_and_safe_diagnostics() -> None:
    cache = MemoryToolSchemaCache(settings=_settings())
    record = _record()

    assert isinstance(cache, ToolSchemaCacheProtocol)
    assert await cache.lookup(record.key) is None
    assert await cache.publish(record) is record
    assert await cache.lookup(record.key) is record
    assert cache.safe_diagnostics() == {
        "backend": "memory",
        "configured": True,
        **_settings().safe_diagnostics(),
    }
    assert record.schema_json not in repr(cache)


@pytest.mark.asyncio
async def test_memory_cache_accepts_identical_publish_and_rejects_collision() -> None:
    cache = MemoryToolSchemaCache(settings=_settings())
    original = _record()
    identical = _record()
    conflicting = ToolSchemaRecord.from_schema(
        original.key,
        {"alpha"},
        [_schema("alpha", description="different")],
    )

    assert await cache.publish(original) is original
    assert await cache.publish(identical) is original
    with pytest.raises(ToolSchemaCacheConflictError, match="不同 schema"):
        await cache.publish(conflicting)
    assert await cache.lookup(original.key) is original


@pytest.mark.asyncio
async def test_memory_cache_uses_lru_without_deleting_old_generation() -> None:
    cache = MemoryToolSchemaCache(settings=_settings(max_entries=2))
    generation_1 = _record(generation=1)
    generation_2 = _record(generation=2)
    generation_3 = _record(generation=3)

    await cache.publish(generation_1)
    await cache.publish(generation_2)
    assert await cache.lookup(generation_1.key) is generation_1
    await cache.publish(generation_3)

    assert await cache.lookup(generation_2.key) is None
    assert await cache.lookup(generation_1.key) is generation_1
    assert await cache.lookup(generation_3.key) is generation_3


@pytest.mark.asyncio
async def test_memory_cache_uses_total_byte_capacity() -> None:
    first = _record(generation=1)
    second = _record(generation=2)
    cache = MemoryToolSchemaCache(
        settings=_settings(
            max_record_bytes=max(first.record_bytes, second.record_bytes),
            max_total_bytes=first.record_bytes + second.record_bytes - 1,
        )
    )

    await cache.publish(first)
    await cache.publish(second)

    assert await cache.lookup(first.key) is None
    assert await cache.lookup(second.key) is second


@pytest.mark.asyncio
async def test_memory_cache_rejects_oversized_record_without_storing() -> None:
    record = _record()
    cache = MemoryToolSchemaCache(
        settings=_settings(
            max_record_bytes=record.record_bytes - 1,
            max_total_bytes=record.record_bytes,
        )
    )

    with pytest.raises(ToolSchemaCacheUnavailableError, match="大小"):
        await cache.publish(record)
    assert await cache.lookup(record.key) is None


@pytest.mark.asyncio
async def test_memory_cache_clear_retains_owner_boundary() -> None:
    owner = {"pid": os.getpid()}
    cache = MemoryToolSchemaCache(
        settings=_settings(),
        pid_provider=lambda: owner["pid"],
    )
    record = _record()
    await cache.publish(record)
    await cache.clear()

    assert await cache.lookup(record.key) is None
    owner["pid"] += 1
    with pytest.raises(ToolSchemaCacheOwnershipError, match="跨进程"):
        await cache.lookup(record.key)


@pytest.mark.asyncio
async def test_memory_cache_rejects_cross_process_and_loop_reuse() -> None:
    owner = {"pid": os.getpid()}
    loop = {"value": asyncio.get_running_loop()}
    cache = MemoryToolSchemaCache(
        settings=_settings(),
        pid_provider=lambda: owner["pid"],
        loop_provider=lambda: loop["value"],
    )
    record = _record()
    assert await cache.lookup(record.key) is None

    owner["pid"] += 1
    with pytest.raises(ToolSchemaCacheOwnershipError, match="跨进程"):
        await cache.lookup(record.key)
    owner["pid"] -= 1

    foreign_loop = asyncio.new_event_loop()
    loop["value"] = foreign_loop
    try:
        with pytest.raises(ToolSchemaCacheOwnershipError, match="event loop"):
            await cache.lookup(record.key)
    finally:
        foreign_loop.close()


@pytest.mark.asyncio
async def test_memory_cache_sanitizes_owner_provider_errors() -> None:
    def broken_pid() -> int:
        raise RuntimeError("redis://secret@example.invalid")

    cache = MemoryToolSchemaCache(
        settings=_settings(),
        pid_provider=broken_pid,
    )
    with pytest.raises(ToolSchemaCacheUnavailableError) as error_info:
        await cache.lookup(_record().key)
    assert "RuntimeError" in str(error_info.value)
    assert "secret" not in str(error_info.value)
    assert error_info.value.__cause__ is None


@pytest.mark.asyncio
async def test_memory_cache_rejects_invalid_provider_results_and_inputs() -> None:
    record = _record()
    for provider, match in (
        (lambda: 0, "进程身份"),
        (lambda: True, "进程身份"),
    ):
        cache = MemoryToolSchemaCache(
            settings=_settings(),
            pid_provider=provider,
        )
        with pytest.raises(ToolSchemaCacheUnavailableError, match=match):
            await cache.lookup(record.key)

    cache = MemoryToolSchemaCache(
        settings=_settings(),
        loop_provider=lambda: object(),  # type: ignore[arg-type]
    )
    with pytest.raises(ToolSchemaCacheUnavailableError, match="event loop"):
        await cache.lookup(record.key)

    cache = MemoryToolSchemaCache(settings=_settings())
    with pytest.raises(TypeError, match="ToolSchemaCacheKey"):
        await cache.lookup(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="ToolSchemaRecord"):
        await cache.publish(object())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_concurrent_identical_publish_has_one_canonical_record() -> None:
    cache = MemoryToolSchemaCache(settings=_settings())
    record = _record()

    results = await asyncio.gather(*(cache.publish(record) for _ in range(8)))
    assert all(item is record for item in results)


@pytest.mark.asyncio
async def test_resolver_builds_on_miss_and_skips_builder_on_hit() -> None:
    cache = MemoryToolSchemaCache(settings=_settings())
    record = _record()
    calls = 0

    def builder() -> ToolSchemaRecord:
        nonlocal calls
        calls += 1
        return record

    assert await resolve_tool_schema(cache, record.key, builder) is record
    assert await resolve_tool_schema(cache, record.key, builder) is record
    assert calls == 1


@pytest.mark.asyncio
async def test_resolver_never_caches_failed_parity_build() -> None:
    cache = MemoryToolSchemaCache(settings=_settings())
    key = _record().key

    def failed_builder() -> ToolSchemaRecord:
        raise ProviderConsumerParityError("provider parity failed")

    with pytest.raises(ProviderConsumerParityError, match="parity"):
        await resolve_tool_schema(cache, key, failed_builder)
    assert await cache.lookup(key) is None


@pytest.mark.asyncio
async def test_resolver_rejects_wrong_builder_and_backend_results() -> None:
    cache = MemoryToolSchemaCache(settings=_settings())
    expected = _record(generation=1)
    wrong = _record(generation=2)

    with pytest.raises(ValueError, match="错误 identity"):
        await resolve_tool_schema(cache, expected.key, lambda: wrong)
    assert await cache.lookup(expected.key) is None
    with pytest.raises(TypeError, match="ToolSchemaRecord"):
        await resolve_tool_schema(
            cache,
            expected.key,
            lambda: "schema",  # type: ignore[arg-type,return-value]
        )
    with pytest.raises(TypeError, match="builder"):
        await resolve_tool_schema(
            cache,
            expected.key,
            object(),  # type: ignore[arg-type]
        )

    class WrongLookup:
        async def lookup(self, key: ToolSchemaCacheKey) -> ToolSchemaRecord:
            del key
            return wrong

        async def publish(self, record: ToolSchemaRecord) -> ToolSchemaRecord:
            return record

    with pytest.raises(ToolSchemaCacheUnavailableError, match="identity"):
        await resolve_tool_schema(WrongLookup(), expected.key, lambda: expected)

    class WrongPublish:
        async def lookup(self, key: ToolSchemaCacheKey) -> None:
            del key
            return None

        async def publish(self, record: ToolSchemaRecord) -> ToolSchemaRecord:
            del record
            return wrong

    with pytest.raises(ToolSchemaCacheUnavailableError, match="发布"):
        await resolve_tool_schema(WrongPublish(), expected.key, lambda: expected)


def test_snapshot_captures_policy_once_and_build_does_not_read_globals(
    monkeypatch,
) -> None:
    snapshot = _tool_snapshot(generation=77)
    monkeypatch.setattr(model_selector, "get_tool_blacklist", lambda: ["admin_tool"])
    context = snapshot.capture_llm_payload_schema_context(
        {"root_tool", "admin_tool"},
        tools_enabled=True,
        search_enabled=False,
        is_superuser=True,
        provider_cutover=True,
    )
    monkeypatch.setattr(
        model_selector,
        "get_tool_blacklist",
        lambda: pytest.fail("显式 context 不应重新读取黑名单"),
    )
    monkeypatch.setattr(
        tool_manager,
        "is_tool_blacklisted",
        lambda _name: pytest.fail("显式 context 不应读取全局黑名单"),
    )

    record = snapshot.build_llm_payload_schema_record(context)
    expanded, schema = record.materialize()
    assert expanded == {"root_tool", "dependency_tool"}
    assert [item["function"]["name"] for item in schema] == [
        "dependency_tool",
        "root_tool",
    ]


def test_snapshot_schema_record_matches_legacy_and_provider_views(monkeypatch) -> None:
    snapshot = _tool_snapshot()
    monkeypatch.setattr(model_selector, "get_tool_blacklist", lambda: [])
    selected = {"root_tool", "admin_tool", "web_search", "stale_tool"}

    legacy_names, legacy_schema = snapshot.get_llm_payload_tools(
        selected,
        tools_enabled=True,
        search_enabled=True,
        is_superuser=False,
        provider_cutover=False,
    )
    legacy_record = snapshot.get_llm_payload_schema_record(
        selected,
        tools_enabled=True,
        search_enabled=True,
        is_superuser=False,
        provider_cutover=False,
    )
    provider_record = snapshot.get_llm_payload_schema_record(
        selected,
        tools_enabled=True,
        search_enabled=True,
        is_superuser=False,
        provider_cutover=True,
    )
    record_names, record_schema = legacy_record.materialize()
    provider_names, provider_schema = provider_record.materialize()

    assert record_names == provider_names == legacy_names
    assert sorted(item["function"]["name"] for item in record_schema) == sorted(
        item["function"]["name"] for item in legacy_schema
    )
    assert provider_schema == record_schema
    assert "admin_tool" not in provider_record.tool_names
    assert provider_record.tool_names[-1] == "web_search"


def test_snapshot_admin_and_disabled_schema_contexts(monkeypatch) -> None:
    snapshot = _tool_snapshot()
    monkeypatch.setattr(model_selector, "get_tool_blacklist", lambda: [])

    admin = snapshot.get_llm_payload_schema_record(
        {"admin_tool"},
        tools_enabled=True,
        search_enabled=False,
        is_superuser=True,
        provider_cutover=True,
    )
    disabled = snapshot.get_llm_payload_schema_record(
        {"root_tool"},
        tools_enabled=False,
        search_enabled=True,
        provider_cutover=True,
    )

    assert admin.tool_names == ("admin_tool",)
    assert disabled.tool_names == ()
    assert disabled.materialize()[0] == {"root_tool", "dependency_tool"}


def test_snapshot_rejects_cross_generation_or_wrong_context() -> None:
    snapshot = _tool_snapshot(generation=42)
    context = _context(generation=43, selected_plugins={"root_tool"})
    with pytest.raises(ValueError, match="generation"):
        snapshot.build_llm_payload_schema_record(context)
    with pytest.raises(TypeError, match="ToolSchemaRenderContext"):
        snapshot.build_llm_payload_schema_record(object())  # type: ignore[arg-type]


def test_explicit_builder_rejects_context_flag_drift() -> None:
    context = _context()
    with pytest.raises(ValueError, match="payload 标志"):
        ToolManager.build_llm_payload_schema(
            ["alpha"],
            tools_enabled=False,
            search_enabled=False,
            plugin_info={},
            custom_tools={},
            render_context=context,
        )
    with pytest.raises(ValueError, match="permission"):
        ToolManager.build_tool_schema(
            ["alpha"],
            plugin_info={},
            custom_tools={},
            is_superuser=True,
            render_context=context,
        )


@pytest.mark.asyncio
async def test_snapshot_parity_failure_cannot_enter_schema_cache(
    monkeypatch,
) -> None:
    snapshot = _tool_snapshot()
    context = _context(
        selected_plugins={"root_tool"},
        provider_cutover=True,
    )
    cache = MemoryToolSchemaCache(settings=_settings())
    monkeypatch.setattr(
        ToolManager,
        "build_provider_llm_payload_schema",
        lambda **_kwargs: [],
    )

    with pytest.raises(ProviderConsumerParityError, match="schema"):
        await resolve_tool_schema(
            cache,
            context.cache_key,
            lambda: snapshot.build_llm_payload_schema_record(context),
        )
    assert await cache.lookup(context.cache_key) is None


def test_schema_cache_module_does_not_create_global_cache() -> None:
    import nonebot_plugin_moellmchats.tool_schema_cache as module

    assert not any(isinstance(value, MemoryToolSchemaCache) for value in vars(module).values())

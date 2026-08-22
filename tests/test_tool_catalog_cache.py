from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError, replace
import hashlib
import os
import re
from typing import Any

import pytest

from nonebot_plugin_moellmchats.builtin_tools import builtin_tool_specs
from nonebot_plugin_moellmchats.tool_catalog_cache import (
    MemoryToolCatalogCache,
    MemoryToolCatalogCacheSettings,
    ToolCatalogCacheConflictError,
    ToolCatalogCacheKey,
    ToolCatalogCacheOwnershipError,
    ToolCatalogCacheProtocol,
    ToolCatalogCacheUnavailableError,
    ToolCatalogPermission,
    ToolCatalogRecord,
    ToolCatalogRenderContext,
    resolve_tool_catalog,
)
from nonebot_plugin_moellmchats.tool_contracts import ToolPolicy, ToolSpec
from nonebot_plugin_moellmchats.tool_manager import (
    ProviderConsumerParityError,
    ToolManager,
    ToolSnapshot,
    model_selector,
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


async def _handler(value: str = "ok") -> str:
    return value


def _context(
    *,
    generation: int = 42,
    is_superuser: bool = False,
    provider_cutover: bool = True,
    tools_enabled: bool = True,
    web_search_enabled: bool = False,
    blacklist_patterns: tuple[str, ...] = (),
) -> ToolCatalogRenderContext:
    return ToolCatalogRenderContext.capture(
        generation=generation,
        is_superuser=is_superuser,
        provider_cutover=provider_cutover,
        tools_enabled=tools_enabled,
        web_search_enabled=web_search_enabled,
        blacklist_patterns=blacklist_patterns,
    )


def _record(
    *,
    generation: int = 42,
    catalog: str = "- echo | 自定义函数 | echo",
    **context_changes: Any,
) -> ToolCatalogRecord:
    context = _context(generation=generation, **context_changes)
    return ToolCatalogRecord(context.cache_key, catalog)


def _settings(**changes: int) -> MemoryToolCatalogCacheSettings:
    values = {
        "max_entries": 4,
        "max_catalog_bytes": 1_024,
        "max_total_bytes": 4_096,
    }
    values.update(changes)
    return MemoryToolCatalogCacheSettings(**values)


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
    user_spec = ToolSpec(
        name="user_tool",
        description="user tool",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
        policy=ToolPolicy.configured(),
    )
    admin_spec = ToolSpec(
        name="admin_tool",
        description="admin tool",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
        permission="superuser",
        policy=ToolPolicy.configured(),
    )
    return ToolSnapshot(
        generation=generation,
        plugin_info={},
        custom_tools={
            user_spec.name: {
                **user_spec.as_legacy_schema(),
                "source": "registered",
            },
            admin_spec.name: {
                **admin_spec.as_legacy_schema(),
                "source": "registered",
            },
        },
        tool_dependencies={},
        mcp_tool_names=set(),
        provider_catalog=_provider_catalog(
            (user_spec, admin_spec),
            generation=generation,
        ),
    )


def test_permission_is_closed_to_two_typed_levels() -> None:
    assert ToolCatalogPermission.from_superuser(False) is ToolCatalogPermission.USER
    assert ToolCatalogPermission.from_superuser(True) is ToolCatalogPermission.SUPERUSER
    with pytest.raises(TypeError, match="is_superuser"):
        ToolCatalogPermission.from_superuser(1)  # type: ignore[arg-type]


def test_context_canonicalizes_blacklist_and_keeps_raw_patterns_out_of_keys() -> None:
    secret_pattern = " mcp__private__* "
    context = _context(
        blacklist_patterns=(secret_pattern, "user_tool", "user_tool", "  "),
    )

    assert context.blacklist_patterns == ("mcp__private__*", "user_tool")
    assert re.fullmatch(r"[0-9a-f]{64}", context.blacklist_digest)
    assert secret_pattern not in repr(context)
    assert "private" not in context.cache_key.safe_cache_key
    assert context.cache_key.safe_cache_key.startswith("catalog:user:42:")


def test_cache_key_separates_every_dynamic_render_input() -> None:
    base = _context()
    variants = (
        replace(base, generation=43),
        replace(base, permission=ToolCatalogPermission.SUPERUSER),
        replace(base, provider_cutover=False),
        replace(base, tools_enabled=False),
        replace(base, web_search_enabled=True),
        replace(base, blacklist_patterns=("user_tool",)),
    )

    assert len({base.cache_key, *(item.cache_key for item in variants)}) == 7


def test_blacklist_semantics_match_exact_wildcard_and_service_filters() -> None:
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
    with pytest.raises(ValueError, match="工具名"):
        context.is_blacklisted("")


@pytest.mark.parametrize(
    ("changes", "error", "match"),
    [
        ({"generation": -1}, ValueError, "generation"),
        ({"generation": True}, ValueError, "generation"),
        ({"permission": "root"}, TypeError, "permission"),
        ({"provider_cutover": 1}, TypeError, "provider_cutover"),
        ({"tools_enabled": 1}, TypeError, "tools_enabled"),
        ({"web_search_enabled": 0}, TypeError, "web_search_enabled"),
        ({"blacklist_patterns": ["tool"]}, TypeError, "元组"),
        ({"blacklist_patterns": (object(),)}, TypeError, "字符串"),
        ({"blacklist_patterns": ("bad\x00name",)}, ValueError, "非法"),
        ({"blacklist_patterns": ("x" * 513,)}, ValueError, "过长"),
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
        "web_search_enabled": False,
        "blacklist_patterns": (),
    }
    values.update(changes)
    with pytest.raises(error, match=match):
        ToolCatalogRenderContext(**values)


def test_context_rejects_excessive_blacklist_count() -> None:
    with pytest.raises(ValueError, match="数量"):
        _context(blacklist_patterns=tuple(f"tool-{i}" for i in range(4_097)))


@pytest.mark.parametrize(
    ("changes", "error", "match"),
    [
        ({"generation": -1}, ValueError, "generation"),
        ({"permission": "admin"}, TypeError, "permission"),
        ({"provider_cutover": 1}, TypeError, "provider_cutover"),
        ({"tools_enabled": 1}, TypeError, "tools_enabled"),
        ({"web_search_enabled": 0}, TypeError, "web_search_enabled"),
        ({"blacklist_digest": "not-a-digest"}, ValueError, "SHA-256"),
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
        "web_search_enabled": False,
        "blacklist_digest": "a" * 64,
    }
    values.update(changes)
    with pytest.raises(error, match=match):
        ToolCatalogCacheKey(**values)


def test_catalog_record_is_frozen_bounded_and_digest_bound() -> None:
    record = _record(catalog="- first\n- second")

    assert record.catalog_bytes == len(record.catalog.encode())
    assert record.catalog_digest == hashlib.sha256(record.catalog.encode()).hexdigest()
    assert record.entry_count == 2
    with pytest.raises(FrozenInstanceError):
        record.catalog = "tampered"  # type: ignore[misc]
    with pytest.raises(TypeError, match="ToolCatalogCacheKey"):
        ToolCatalogRecord(object(), "catalog")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="非空"):
        ToolCatalogRecord(_context().cache_key, "")
    with pytest.raises(ValueError, match="安全字符串"):
        ToolCatalogRecord(_context().cache_key, "bad\x00catalog")


def test_memory_settings_are_bounded_and_safe() -> None:
    settings = _settings(max_entries=7, max_catalog_bytes=2_048, max_total_bytes=8_192)
    assert settings.safe_diagnostics() == {
        "max_entries": 7,
        "max_catalog_bytes": 2_048,
        "max_total_bytes": 8_192,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_entries", 0),
        ("max_entries", True),
        ("max_entries", 65_537),
        ("max_catalog_bytes", 0),
        ("max_catalog_bytes", 16_777_217),
        ("max_total_bytes", 0),
        ("max_total_bytes", 268_435_457),
    ],
)
def test_memory_settings_reject_invalid_values(field: str, value: object) -> None:
    with pytest.raises(ValueError, match=field):
        _settings(**{field: value})  # type: ignore[arg-type]


def test_memory_settings_require_total_capacity_for_one_record() -> None:
    with pytest.raises(ValueError, match="max_total_bytes"):
        _settings(max_catalog_bytes=2_048, max_total_bytes=1_024)


def test_memory_cache_requires_typed_dependencies() -> None:
    with pytest.raises(TypeError, match="MemoryToolCatalogCacheSettings"):
        MemoryToolCatalogCache(settings=object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="pid_provider"):
        MemoryToolCatalogCache(pid_provider=object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="loop_provider"):
        MemoryToolCatalogCache(loop_provider=object())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_memory_cache_miss_publish_hit_and_safe_diagnostics() -> None:
    cache = MemoryToolCatalogCache(settings=_settings())
    record = _record()

    assert isinstance(cache, ToolCatalogCacheProtocol)
    assert await cache.lookup(record.key) is None
    assert await cache.publish(record) is record
    assert await cache.lookup(record.key) is record
    assert cache.safe_diagnostics() == {
        "backend": "memory",
        "configured": True,
        **_settings().safe_diagnostics(),
    }
    assert record.catalog not in repr(cache)


@pytest.mark.asyncio
async def test_memory_cache_accepts_identical_publish_and_rejects_key_collision() -> None:
    cache = MemoryToolCatalogCache(settings=_settings())
    original = _record(catalog="catalog-a")
    identical = _record(catalog="catalog-a")
    conflicting = _record(catalog="catalog-b")

    assert await cache.publish(original) is original
    assert await cache.publish(identical) is original
    with pytest.raises(ToolCatalogCacheConflictError, match="不同目录"):
        await cache.publish(conflicting)
    assert await cache.lookup(original.key) is original


@pytest.mark.asyncio
async def test_memory_cache_uses_lru_entry_capacity_without_generation_deletion() -> None:
    cache = MemoryToolCatalogCache(settings=_settings(max_entries=2))
    generation_1 = _record(generation=1, catalog="generation-1")
    generation_2 = _record(generation=2, catalog="generation-2")
    generation_3 = _record(generation=3, catalog="generation-3")

    await cache.publish(generation_1)
    await cache.publish(generation_2)
    assert await cache.lookup(generation_1.key) is generation_1
    await cache.publish(generation_3)

    assert await cache.lookup(generation_2.key) is None
    assert await cache.lookup(generation_1.key) is generation_1
    assert await cache.lookup(generation_3.key) is generation_3


@pytest.mark.asyncio
async def test_memory_cache_uses_total_byte_capacity() -> None:
    cache = MemoryToolCatalogCache(
        settings=_settings(
            max_entries=4,
            max_catalog_bytes=12,
            max_total_bytes=20,
        )
    )
    first = _record(generation=1, catalog="a" * 12)
    second = _record(generation=2, catalog="b" * 12)

    await cache.publish(first)
    await cache.publish(second)

    assert await cache.lookup(first.key) is None
    assert await cache.lookup(second.key) is second


@pytest.mark.asyncio
async def test_memory_cache_rejects_oversized_record_without_storing_it() -> None:
    cache = MemoryToolCatalogCache(settings=_settings(max_catalog_bytes=5, max_total_bytes=10))
    record = _record(catalog="123456")

    with pytest.raises(ToolCatalogCacheUnavailableError, match="大小"):
        await cache.publish(record)
    assert await cache.lookup(record.key) is None


@pytest.mark.asyncio
async def test_memory_cache_clear_removes_records_but_retains_owner() -> None:
    owner = {"pid": os.getpid()}
    cache = MemoryToolCatalogCache(
        settings=_settings(),
        pid_provider=lambda: owner["pid"],
    )
    record = _record()
    await cache.publish(record)
    await cache.clear()

    assert await cache.lookup(record.key) is None
    owner["pid"] += 1
    with pytest.raises(ToolCatalogCacheOwnershipError, match="跨进程"):
        await cache.lookup(record.key)


@pytest.mark.asyncio
async def test_memory_cache_rejects_cross_process_and_loop_reuse() -> None:
    owner = {"pid": os.getpid()}
    loop = {"value": asyncio.get_running_loop()}
    cache = MemoryToolCatalogCache(
        settings=_settings(),
        pid_provider=lambda: owner["pid"],
        loop_provider=lambda: loop["value"],
    )
    record = _record()
    assert await cache.lookup(record.key) is None

    owner["pid"] += 1
    with pytest.raises(ToolCatalogCacheOwnershipError, match="跨进程"):
        await cache.lookup(record.key)
    owner["pid"] -= 1

    foreign_loop = asyncio.new_event_loop()
    loop["value"] = foreign_loop
    try:
        with pytest.raises(ToolCatalogCacheOwnershipError, match="event loop"):
            await cache.lookup(record.key)
    finally:
        foreign_loop.close()


@pytest.mark.asyncio
async def test_memory_cache_sanitizes_owner_provider_errors() -> None:
    def broken_pid() -> int:
        raise RuntimeError("redis://secret@example.invalid")

    cache = MemoryToolCatalogCache(
        settings=_settings(),
        pid_provider=broken_pid,
    )
    with pytest.raises(ToolCatalogCacheUnavailableError) as error_info:
        await cache.lookup(_record().key)
    assert "RuntimeError" in str(error_info.value)
    assert "secret" not in str(error_info.value)
    assert error_info.value.__cause__ is None


@pytest.mark.asyncio
async def test_memory_cache_rejects_invalid_provider_results() -> None:
    record = _record()
    for provider, match in (
        (lambda: 0, "进程身份"),
        (lambda: True, "进程身份"),
    ):
        cache = MemoryToolCatalogCache(
            settings=_settings(),
            pid_provider=provider,
        )
        with pytest.raises(ToolCatalogCacheUnavailableError, match=match):
            await cache.lookup(record.key)

    cache = MemoryToolCatalogCache(
        settings=_settings(),
        loop_provider=lambda: object(),  # type: ignore[arg-type]
    )
    with pytest.raises(ToolCatalogCacheUnavailableError, match="event loop"):
        await cache.lookup(record.key)


@pytest.mark.asyncio
async def test_memory_cache_rejects_invalid_operation_inputs() -> None:
    cache = MemoryToolCatalogCache(settings=_settings())
    with pytest.raises(TypeError, match="ToolCatalogCacheKey"):
        await cache.lookup(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="ToolCatalogRecord"):
        await cache.publish(object())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_concurrent_identical_publish_has_one_canonical_record() -> None:
    cache = MemoryToolCatalogCache(settings=_settings())
    record = _record()

    results = await asyncio.gather(*(cache.publish(record) for _ in range(8)))
    assert all(item is record for item in results)


@pytest.mark.asyncio
async def test_resolver_builds_on_miss_and_skips_builder_on_hit() -> None:
    cache = MemoryToolCatalogCache(settings=_settings())
    record = _record()
    calls = 0

    def builder() -> ToolCatalogRecord:
        nonlocal calls
        calls += 1
        return record

    assert await resolve_tool_catalog(cache, record.key, builder) is record
    assert await resolve_tool_catalog(cache, record.key, builder) is record
    assert calls == 1


@pytest.mark.asyncio
async def test_resolver_never_caches_failed_parity_build() -> None:
    cache = MemoryToolCatalogCache(settings=_settings())
    key = _record().key

    def failed_builder() -> ToolCatalogRecord:
        raise ProviderConsumerParityError("provider parity failed")

    with pytest.raises(ProviderConsumerParityError, match="parity"):
        await resolve_tool_catalog(cache, key, failed_builder)
    assert await cache.lookup(key) is None


@pytest.mark.asyncio
async def test_resolver_rejects_wrong_builder_result_without_publication() -> None:
    cache = MemoryToolCatalogCache(settings=_settings())
    key = _record(generation=1).key
    wrong = _record(generation=2)

    with pytest.raises(ValueError, match="错误 identity"):
        await resolve_tool_catalog(cache, key, lambda: wrong)
    assert await cache.lookup(key) is None
    with pytest.raises(TypeError, match="ToolCatalogRecord"):
        await resolve_tool_catalog(cache, key, lambda: "catalog")  # type: ignore[arg-type,return-value]
    with pytest.raises(TypeError, match="builder"):
        await resolve_tool_catalog(cache, key, object())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_resolver_rejects_untrusted_backend_results() -> None:
    expected = _record(generation=1)
    wrong = _record(generation=2)

    class WrongLookup:
        async def lookup(self, key: ToolCatalogCacheKey) -> ToolCatalogRecord:
            del key
            return wrong

        async def publish(self, record: ToolCatalogRecord) -> ToolCatalogRecord:
            return record

    with pytest.raises(ToolCatalogCacheUnavailableError, match="identity"):
        await resolve_tool_catalog(WrongLookup(), expected.key, lambda: expected)

    class WrongPublish:
        async def lookup(self, key: ToolCatalogCacheKey) -> None:
            del key
            return None

        async def publish(self, record: ToolCatalogRecord) -> ToolCatalogRecord:
            del record
            return wrong

    with pytest.raises(ToolCatalogCacheUnavailableError, match="发布"):
        await resolve_tool_catalog(WrongPublish(), expected.key, lambda: expected)


def test_explicit_context_renders_without_reading_mutable_global_policy(
    monkeypatch,
) -> None:
    snapshot = _tool_snapshot()
    context = _context(
        is_superuser=False,
        provider_cutover=True,
        blacklist_patterns=("user_tool",),
    )
    monkeypatch.setattr(
        model_selector,
        "get_use_tools",
        lambda: pytest.fail("显式 context 不应重新读取 Tools 开关"),
    )
    monkeypatch.setattr(
        model_selector,
        "get_web_search",
        lambda: pytest.fail("显式 context 不应重新读取 Search 开关"),
    )
    monkeypatch.setattr(
        model_selector,
        "get_tool_blacklist",
        lambda: pytest.fail("显式 context 不应重新读取黑名单"),
    )

    record = snapshot.build_brief_catalog_record(context)
    assert record.key == context.cache_key
    assert record.catalog == "当前工具调用与联网功能均已关闭，无需返回任何插件。"
    assert "user_tool" not in record.catalog
    assert "admin_tool" not in record.catalog


def test_explicit_context_filters_permission_and_feature_flags() -> None:
    snapshot = _tool_snapshot()
    user = snapshot.build_brief_catalog_record(_context())
    admin = snapshot.build_brief_catalog_record(_context(is_superuser=True))
    search_only = snapshot.build_brief_catalog_record(_context(tools_enabled=False, web_search_enabled=True))

    assert "user_tool" in user.catalog
    assert "admin_tool" not in user.catalog
    assert "admin_tool" in admin.catalog
    assert search_only.catalog.startswith("- web_search |")


def test_snapshot_captures_complete_dynamic_policy_once(monkeypatch) -> None:
    snapshot = _tool_snapshot(generation=77)
    monkeypatch.setattr(model_selector, "get_use_tools", lambda: False)
    monkeypatch.setattr(model_selector, "get_web_search", lambda: True)
    monkeypatch.setattr(
        model_selector,
        "get_tool_blacklist",
        lambda: [" user_tool ", "mcp__private__*"],
    )

    context = snapshot.capture_brief_catalog_context(
        is_superuser=True,
        provider_cutover=False,
    )

    assert context.generation == 77
    assert context.permission is ToolCatalogPermission.SUPERUSER
    assert context.provider_cutover is False
    assert context.tools_enabled is False
    assert context.web_search_enabled is True
    assert context.blacklist_patterns == ("mcp__private__*", "user_tool")


def test_snapshot_rejects_cross_generation_context() -> None:
    snapshot = _tool_snapshot(generation=42)
    with pytest.raises(ValueError, match="generation"):
        snapshot.build_brief_catalog_record(_context(generation=43))
    with pytest.raises(TypeError, match="ToolCatalogRenderContext"):
        snapshot.build_brief_catalog_record(object())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_snapshot_parity_failure_cannot_enter_cache(monkeypatch) -> None:
    snapshot = _tool_snapshot()
    context = _context(provider_cutover=True)
    cache = MemoryToolCatalogCache(settings=_settings())
    monkeypatch.setattr(
        ToolManager,
        "build_provider_brief_catalog",
        lambda **_kwargs: "drifted provider catalog",
    )

    with pytest.raises(ProviderConsumerParityError, match="rollback view"):
        await resolve_tool_catalog(
            cache,
            context.cache_key,
            lambda: snapshot.build_brief_catalog_record(context),
        )
    assert await cache.lookup(context.cache_key) is None

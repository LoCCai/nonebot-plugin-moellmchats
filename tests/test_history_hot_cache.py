from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
from math import inf, nan
from typing import Any

import pytest

from nonebot_plugin_moellmchats.chat_history import MessageRecord
from nonebot_plugin_moellmchats.history_hot_cache import (
    HistoryCacheLoadToken,
    HistoryCacheLookup,
    HistoryHotCacheOwnershipError,
    HistoryHotCacheProtocol,
    HistoryHotCacheUnavailableError,
    HistoryWindow,
    MemoryHistoryHotCache,
    MemoryHistoryHotCacheSettings,
)

_GENERATION_A = "a" * 32
_GENERATION_B = "b" * 32
_GENERATION_C = "c" * 32
_GENERATION_D = "d" * 32
_GENERATION_E = "e" * 32


class ManualClock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _generation_factory(*values: str) -> Callable[[], str]:
    generations = iter(values)
    return lambda: next(generations)


def _settings(**changes: Any) -> MemoryHistoryHotCacheSettings:
    values: dict[str, Any] = {
        "ttl_seconds": 60.0,
        "load_timeout_seconds": 10.0,
        "max_conversations": 4,
        "max_messages": 4,
        "max_payload_bytes": 65_536,
    }
    values.update(changes)
    return MemoryHistoryHotCacheSettings(**values)


def _message(
    message_id: int | None,
    *,
    conversation_id: str = "conversation-alpha",
    content: str | None = None,
    structured_content: Any = None,
) -> MessageRecord:
    return MessageRecord(
        message_id=message_id,
        conversation_id=conversation_id,
        platform_message_id=f"platform-{message_id}" if message_id is not None else None,
        role="user" if message_id is None or message_id % 2 else "assistant",
        sender_id="user-1",
        content=content if content is not None else f"message-{message_id}",
        structured_content=structured_content,
        created_at=datetime(2026, 8, 22, 12, 0, message_id or 0, tzinfo=timezone.utc),
    )


def _window(
    *message_ids: int,
    conversation_id: str = "conversation-alpha",
    has_older: bool = False,
) -> HistoryWindow:
    return HistoryWindow(
        conversation_id=conversation_id,
        messages=tuple(_message(message_id, conversation_id=conversation_id) for message_id in message_ids),
        has_older=has_older,
    )


def test_memory_history_hot_cache_settings_are_bounded_and_safe() -> None:
    settings = _settings(
        ttl_seconds=90,
        load_timeout_seconds=12.5,
        max_conversations=17,
        max_messages=19,
        max_payload_bytes=32_768,
    )

    assert settings.safe_diagnostics() == {
        "ttl_seconds": 90.0,
        "load_timeout_seconds": 12.5,
        "max_conversations": 17,
        "max_messages": 19,
        "max_payload_bytes": 32_768,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ttl_seconds", 0),
        ("ttl_seconds", 86_401),
        ("ttl_seconds", inf),
        ("load_timeout_seconds", 0),
        ("load_timeout_seconds", 301),
        ("load_timeout_seconds", nan),
        ("max_conversations", 0),
        ("max_conversations", 100_001),
        ("max_conversations", True),
        ("max_messages", 0),
        ("max_messages", 201),
        ("max_messages", 1.5),
        ("max_payload_bytes", 1_023),
        ("max_payload_bytes", 16_777_217),
    ],
)
def test_memory_history_hot_cache_settings_reject_invalid_values(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValueError, match=field):
        _settings(**{field: value})


def test_load_timeout_cannot_outlive_ready_ttl() -> None:
    with pytest.raises(ValueError, match="load_timeout_seconds"):
        _settings(ttl_seconds=5, load_timeout_seconds=6)


def test_history_window_is_persisted_ordered_and_bounded_by_conversation() -> None:
    window = HistoryWindow(
        conversation_id="conversation-alpha",
        messages=(
            _message(1, structured_content={"nested": [1, {"ok": True}]}),
            _message(2),
            _message(3),
        ),
        has_older=False,
    )

    assert window.oldest_message_id == 1
    assert window.newest_message_id == 3
    assert window.recent(3) is window
    recent = window.recent(2)
    assert [message.message_id for message in recent.messages] == [2, 3]
    assert recent.has_older is True


@pytest.mark.parametrize(
    ("messages", "has_older", "match"),
    [
        ([], False, "元组"),
        ((), True, "空 HistoryWindow"),
        ((_message(None),), False, "已持久化"),
        ((_message(2), _message(1)), False, "严格递增"),
        ((_message(1), _message(1)), False, "严格递增"),
        ((_message(1, conversation_id="other"),), False, "同一会话"),
        ((object(),), False, "MessageRecord"),
    ],
)
def test_history_window_rejects_invalid_records(
    messages: object,
    has_older: bool,
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        HistoryWindow(
            conversation_id="conversation-alpha",
            messages=messages,  # type: ignore[arg-type]
            has_older=has_older,
        )


@pytest.mark.parametrize("limit", [0, 201, True, 1.5])
def test_history_window_recent_rejects_invalid_limits(limit: object) -> None:
    with pytest.raises(ValueError, match="limit"):
        _window(1).recent(limit)  # type: ignore[arg-type]


def test_lookup_and_load_token_are_exclusive_and_token_repr_is_redacted() -> None:
    token = HistoryCacheLoadToken(
        conversation_fingerprint="f" * 64,
        generation=_GENERATION_A,
        expires_at=123.0,
    )

    assert repr(token) == "HistoryCacheLoadToken(<redacted>)"
    assert HistoryCacheLookup(load_token=token).hit is False
    assert HistoryCacheLookup(window=_window(1)).hit is True
    with pytest.raises(ValueError, match="只能包含"):
        HistoryCacheLookup()
    with pytest.raises(ValueError, match="只能包含"):
        HistoryCacheLookup(window=_window(1), load_token=token)


def test_load_token_rejects_noncanonical_identity() -> None:
    with pytest.raises(ValueError, match="SHA-256"):
        HistoryCacheLoadToken("raw-conversation", _GENERATION_A, 1.0)
    with pytest.raises(ValueError, match="generation"):
        HistoryCacheLoadToken("f" * 64, "not-a-generation", 1.0)
    with pytest.raises(ValueError, match="expires_at"):
        HistoryCacheLoadToken("f" * 64, _GENERATION_A, inf)


def test_memory_cache_requires_explicit_typed_dependencies() -> None:
    with pytest.raises(TypeError, match="MemoryHistoryHotCacheSettings"):
        MemoryHistoryHotCache(settings=object())  # type: ignore[arg-type]
    for field in ("clock", "generation_factory", "pid_provider", "loop_provider"):
        with pytest.raises(TypeError, match=field):
            MemoryHistoryHotCache(**{field: object()})  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_memory_cache_miss_publish_hit_and_recent_suffix() -> None:
    clock = ManualClock()
    cache = MemoryHistoryHotCache(
        settings=_settings(),
        clock=clock,
        generation_factory=lambda: _GENERATION_A,
    )

    lookup = await cache.lookup("conversation-alpha", limit=2)
    assert isinstance(cache, HistoryHotCacheProtocol)
    assert lookup.hit is False
    assert lookup.load_token is not None
    assert await cache.publish(lookup.load_token, _window(1, 2, 3, 4)) is True

    hit = await cache.lookup("conversation-alpha", limit=2)
    assert hit.load_token is None
    assert hit.window is not None
    assert [message.message_id for message in hit.window.messages] == [3, 4]
    assert hit.window.has_older is True

    complete = await cache.lookup("conversation-alpha", limit=4)
    assert complete.window == _window(1, 2, 3, 4)
    assert cache.safe_diagnostics() == {
        "backend": "memory",
        "configured": True,
        **_settings().safe_diagnostics(),
    }
    assert "conversation-alpha" not in repr(cache)


@pytest.mark.asyncio
async def test_memory_cache_rejects_invalid_limits_and_publish_inputs() -> None:
    cache = MemoryHistoryHotCache(
        settings=_settings(max_messages=2),
        generation_factory=lambda: _GENERATION_A,
    )
    lookup = await cache.lookup("conversation-alpha", limit=2)
    assert lookup.load_token is not None

    for limit in (0, 3, True):
        with pytest.raises(ValueError, match="limit"):
            await cache.lookup("conversation-alpha", limit=limit)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="load_token"):
        await cache.publish(object(), _window(1))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="window"):
        await cache.publish(lookup.load_token, object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="max_messages"):
        await cache.publish(lookup.load_token, _window(1, 2, 3))
    with pytest.raises(ValueError, match="不匹配"):
        await cache.publish(
            lookup.load_token,
            _window(1, conversation_id="conversation-beta"),
        )


@pytest.mark.asyncio
async def test_memory_cache_rejects_oversized_window_without_leaking_content() -> None:
    secret = "sensitive-body-" * 200
    cache = MemoryHistoryHotCache(
        settings=_settings(max_payload_bytes=1_024),
        generation_factory=lambda: _GENERATION_A,
    )
    lookup = await cache.lookup("conversation-alpha", limit=1)
    assert lookup.load_token is not None

    with pytest.raises(HistoryHotCacheUnavailableError) as error_info:
        await cache.publish(
            lookup.load_token,
            HistoryWindow(
                conversation_id="conversation-alpha",
                messages=(_message(1, content=secret),),
                has_older=False,
            ),
        )
    assert secret not in str(error_info.value)


@pytest.mark.asyncio
async def test_memory_cache_invalidation_rejects_late_and_duplicate_publish() -> None:
    cache = MemoryHistoryHotCache(
        settings=_settings(),
        generation_factory=_generation_factory(_GENERATION_A, _GENERATION_B),
    )
    first = await cache.lookup("conversation-alpha", limit=4)
    assert first.load_token is not None

    await cache.invalidate("conversation-alpha")
    assert await cache.publish(first.load_token, _window(1)) is False
    second = await cache.lookup("conversation-alpha", limit=4)
    assert second.load_token is not None
    assert second.load_token.generation == _GENERATION_B
    assert await cache.publish(second.load_token, _window(1, 2)) is True
    assert await cache.publish(second.load_token, _window(1, 2)) is False


@pytest.mark.asyncio
async def test_memory_cache_reservation_and_ready_ttls_are_fixed() -> None:
    clock = ManualClock()
    cache = MemoryHistoryHotCache(
        settings=_settings(ttl_seconds=20, load_timeout_seconds=5),
        clock=clock,
        generation_factory=_generation_factory(
            _GENERATION_A,
            _GENERATION_B,
            _GENERATION_C,
        ),
    )

    first = await cache.lookup("conversation-alpha", limit=4)
    assert first.load_token is not None
    assert first.load_token.expires_at == 105.0
    clock.advance(5)
    assert await cache.publish(first.load_token, _window(1)) is False

    second = await cache.lookup("conversation-alpha", limit=4)
    assert second.load_token is not None
    assert second.load_token.generation == _GENERATION_B
    assert await cache.publish(second.load_token, _window(1)) is True
    clock.advance(19.9)
    assert (await cache.lookup("conversation-alpha", limit=4)).hit is True
    clock.advance(0.1)
    expired = await cache.lookup("conversation-alpha", limit=4)
    assert expired.hit is False
    assert expired.load_token is not None
    assert expired.load_token.generation == _GENERATION_C


async def _populate(
    cache: MemoryHistoryHotCache,
    conversation_id: str,
    message_id: int,
) -> None:
    lookup = await cache.lookup(conversation_id, limit=1)
    assert lookup.load_token is not None
    assert await cache.publish(
        lookup.load_token,
        _window(message_id, conversation_id=conversation_id),
    )


@pytest.mark.asyncio
async def test_memory_cache_uses_lru_capacity_and_evicted_tokens_cannot_publish() -> None:
    cache = MemoryHistoryHotCache(
        settings=_settings(max_conversations=2, max_messages=2),
        generation_factory=_generation_factory(
            _GENERATION_A,
            _GENERATION_B,
            _GENERATION_C,
            _GENERATION_D,
            _GENERATION_E,
        ),
    )
    await _populate(cache, "conversation-a", 1)
    await _populate(cache, "conversation-b", 2)
    assert (await cache.lookup("conversation-a", limit=1)).hit is True

    c_lookup = await cache.lookup("conversation-c", limit=1)
    assert c_lookup.load_token is not None
    b_lookup = await cache.lookup("conversation-b", limit=1)
    assert b_lookup.hit is False
    assert b_lookup.load_token is not None
    d_lookup = await cache.lookup("conversation-d", limit=1)
    assert d_lookup.load_token is not None
    assert await cache.publish(c_lookup.load_token, _window(3, conversation_id="conversation-c")) is False


@pytest.mark.asyncio
async def test_memory_cache_clear_invalidates_outstanding_tokens() -> None:
    cache = MemoryHistoryHotCache(
        settings=_settings(),
        generation_factory=lambda: _GENERATION_A,
    )
    lookup = await cache.lookup("conversation-alpha", limit=1)
    assert lookup.load_token is not None
    await cache.clear()

    assert await cache.publish(lookup.load_token, _window(1)) is False
    assert (await cache.lookup("conversation-alpha", limit=1)).hit is False


@pytest.mark.asyncio
async def test_memory_cache_only_allows_one_concurrent_publisher() -> None:
    cache = MemoryHistoryHotCache(
        settings=_settings(),
        generation_factory=lambda: _GENERATION_A,
    )
    lookup = await cache.lookup("conversation-alpha", limit=1)
    assert lookup.load_token is not None

    results = await asyncio.gather(
        cache.publish(lookup.load_token, _window(1)),
        cache.publish(lookup.load_token, _window(1)),
    )
    assert sorted(results) == [False, True]


@pytest.mark.asyncio
async def test_memory_cache_rejects_cross_process_reuse() -> None:
    owner = {"pid": 100}
    cache = MemoryHistoryHotCache(
        settings=_settings(),
        generation_factory=lambda: _GENERATION_A,
        pid_provider=lambda: owner["pid"],
    )
    assert (await cache.lookup("conversation-alpha", limit=1)).hit is False
    owner["pid"] = 101

    with pytest.raises(HistoryHotCacheOwnershipError, match="跨进程"):
        await cache.invalidate("conversation-alpha")


@pytest.mark.asyncio
async def test_memory_cache_rejects_invalid_clock_and_generation_without_leaking_values() -> None:
    bad_clock = MemoryHistoryHotCache(
        settings=_settings(),
        clock=lambda: nan,
        generation_factory=lambda: _GENERATION_A,
    )
    with pytest.raises(HistoryHotCacheUnavailableError, match="时钟返回无效值"):
        await bad_clock.lookup("conversation-secret", limit=1)

    bad_generation = MemoryHistoryHotCache(
        settings=_settings(),
        generation_factory=lambda: "conversation-secret",
    )
    with pytest.raises(HistoryHotCacheUnavailableError) as error_info:
        await bad_generation.lookup("conversation-secret", limit=1)
    assert "conversation-secret" not in str(error_info.value)


@pytest.mark.asyncio
async def test_memory_cache_propagates_generation_factory_cancellation() -> None:
    def cancelled() -> str:
        raise asyncio.CancelledError

    cache = MemoryHistoryHotCache(
        settings=_settings(),
        generation_factory=cancelled,
    )
    with pytest.raises(asyncio.CancelledError):
        await cache.lookup("conversation-alpha", limit=1)

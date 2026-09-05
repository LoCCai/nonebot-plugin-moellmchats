from __future__ import annotations

from nonebot_plugin_moellmchats.state_store import (
    BoundedDequeStore,
    BoundedValueStore,
)


def test_deque_store_contains_is_not_default_creating() -> None:
    store = BoundedDequeStore(lambda: 8)

    assert "missing" not in store
    assert len(store) == 0

    store["42"].append("hello")
    assert "42" in store
    assert len(store) == 1

    store.clear()
    assert "42" not in store


def test_value_store_contains_is_not_default_creating() -> None:
    store = BoundedValueStore(lambda: 0)

    assert 42 not in store
    assert len(store) == 0

    assert store[42] == 0
    assert 42 in store
    store.clear()
    assert 42 not in store


def test_reset_by_string_key_clears_real_history() -> None:
    store = BoundedDequeStore(lambda: 8)
    user_id = "12345"
    store[user_id].append({"role": "user", "content": "hi"})

    assert user_id in store
    store[user_id].clear()
    assert list(store[user_id]) == []

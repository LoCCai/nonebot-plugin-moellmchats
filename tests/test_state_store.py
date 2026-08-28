from __future__ import annotations

from nonebot_plugin_moellmchats.state_store import (
    BoundedDequeStore,
    BoundedValueStore,
)


def test_deque_store_contains_is_not_default_creating() -> None:
    store = BoundedDequeStore(lambda: 8)

    assert "missing" not in store
    # 成员判断不得借 __getitem__ 制造垃圾条目
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

    store[42] = 5
    assert store[42] == 5

    store.clear()
    assert 42 not in store


def test_reset_by_string_key_clears_real_history() -> None:
    # 回归：重置命令此前用 int 键查询 str 键写入的历史，永远清不掉
    store = BoundedDequeStore(lambda: 8)
    user_id = "12345"
    store[user_id].append({"role": "user", "content": "hi"})

    assert user_id in store
    store[user_id].clear()
    assert store[user_id] is not None
    assert len(store[user_id]) == 0

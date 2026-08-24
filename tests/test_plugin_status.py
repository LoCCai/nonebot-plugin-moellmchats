from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import nonebot_plugin_moellmchats as plugin


class SizeStore:
    def __init__(
        self,
        value: int = 0,
        *,
        failure: BaseException | None = None,
    ) -> None:
        self.value = value
        self.failure = failure
        self.calls = 0

    async def size(self) -> int:
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        return self.value


class SnapshotStore:
    def __init__(self, snapshot: object | None) -> None:
        self.snapshot = snapshot

    def current(self) -> object | None:
        return self.snapshot


class ResourceHost:
    def __init__(self, snapshot: object, store: SizeStore) -> None:
        self.snapshot = snapshot
        self.store = store
        self.leases = 0

    @asynccontextmanager
    async def lease(self, snapshot: object) -> AsyncIterator[SimpleNamespace]:
        assert snapshot is self.snapshot
        self.leases += 1
        yield SimpleNamespace(resources=SimpleNamespace(pending_action_store=self.store))


@pytest.mark.asyncio
async def test_status_reads_pending_count_from_current_generation_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = SimpleNamespace(generation=7)
    generation_store = SizeStore(9)
    memory_store = SizeStore(3)
    host = ResourceHost(snapshot, generation_store)
    monkeypatch.setattr(plugin, "runtime_snapshots", SnapshotStore(snapshot))
    monkeypatch.setattr(plugin, "runtime_resource_host", host)
    monkeypatch.setattr(plugin, "pending_action_store", memory_store)

    assert await plugin._current_pending_action_count() == 9
    assert host.leases == 1
    assert generation_store.calls == 1
    assert memory_store.calls == 0


@pytest.mark.asyncio
async def test_status_never_falls_back_to_memory_when_generation_store_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = SimpleNamespace(generation=8)
    generation_store = SizeStore(failure=RuntimeError("redis://user:private-secret@internal.invalid/0"))
    memory_store = SizeStore(5)
    monkeypatch.setattr(plugin, "runtime_snapshots", SnapshotStore(snapshot))
    monkeypatch.setattr(
        plugin,
        "runtime_resource_host",
        ResourceHost(snapshot, generation_store),
    )
    monkeypatch.setattr(plugin, "pending_action_store", memory_store)

    assert await plugin._current_pending_action_count() == "不可用"
    assert generation_store.calls == 1
    assert memory_store.calls == 0


@pytest.mark.asyncio
async def test_status_uses_memory_store_before_any_snapshot_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory_store = SizeStore(4)
    monkeypatch.setattr(plugin, "runtime_snapshots", SnapshotStore(None))
    monkeypatch.setattr(plugin, "pending_action_store", memory_store)

    assert await plugin._current_pending_action_count() == 4
    assert memory_store.calls == 1


@pytest.mark.asyncio
async def test_status_pending_count_propagates_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = SimpleNamespace(generation=9)
    generation_store = SizeStore(failure=asyncio.CancelledError())
    monkeypatch.setattr(plugin, "runtime_snapshots", SnapshotStore(snapshot))
    monkeypatch.setattr(
        plugin,
        "runtime_resource_host",
        ResourceHost(snapshot, generation_store),
    )

    with pytest.raises(asyncio.CancelledError):
        await plugin._current_pending_action_count()

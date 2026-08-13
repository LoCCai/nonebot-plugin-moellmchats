from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any


def immutable_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return a detached, read-only top-level mapping for a runtime generation."""
    return MappingProxyType(dict(value))


@dataclass(frozen=True)
class RuntimeSnapshot:
    generation: int
    config: Mapping[str, Any]
    model_state: Any
    temperaments: Mapping[str, str]
    temperament_assignments: Mapping[str, str]
    replies: Mapping[str, tuple[str, ...]]
    tool_snapshot: Any
    emotions: tuple[str, ...]
    reloaded_at: float


class RuntimeSnapshotStore:
    """Atomically publishes generations and pins one generation to a request."""

    def __init__(self) -> None:
        self._current: RuntimeSnapshot | None = None
        self._bound: ContextVar[RuntimeSnapshot | None] = ContextVar(
            "moellm_runtime_snapshot", default=None
        )

    def current(self) -> RuntimeSnapshot | None:
        return self._current

    def active(self) -> RuntimeSnapshot | None:
        return self._bound.get() or self._current

    def bound(self) -> RuntimeSnapshot | None:
        return self._bound.get()

    def publish(self, snapshot: RuntimeSnapshot) -> None:
        self._current = snapshot

    def patch_current(self, **changes: Any) -> None:
        if self._current is not None:
            self._current = replace(self._current, **changes)

    @contextmanager
    def bind(self, snapshot: RuntimeSnapshot | None) -> Iterator[None]:
        token = self._bound.set(snapshot)
        try:
            yield
        finally:
            self._bound.reset(token)


runtime_snapshots = RuntimeSnapshotStore()

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any


def immutable_value(value: Any) -> Any:
    """Detach and recursively freeze values published in a runtime generation."""
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): immutable_value(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(immutable_value(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(immutable_value(item) for item in value)
    return deepcopy(value)


def immutable_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return a detached, recursively read-only runtime mapping."""
    return immutable_value(value)


def mutable_value(value: Any) -> Any:
    """Return a detached mutable copy suitable for JSON payloads and legacy APIs."""
    if isinstance(value, Mapping):
        return {key: mutable_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [mutable_value(item) for item in value]
    if isinstance(value, frozenset):
        return {mutable_value(item) for item in value}
    return deepcopy(value)


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

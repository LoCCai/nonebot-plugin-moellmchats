from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field, replace
import re
from threading import RLock
from types import MappingProxyType
from typing import Any

from .tool_artifacts import ToolArtifact, ToolContractSnapshot

_STATE_DIGEST_RE = re.compile(r"^[a-f0-9]{64}$")
_BUNDLE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


def validate_generated_stamp(
    revision: int,
    state_digest: str,
    active: Mapping[str, str],
) -> Mapping[str, str]:
    """Validate and detach the lifecycle stamp carried by a runtime object."""

    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise ValueError("generated_state_revision 必须是非负整数")
    if not isinstance(state_digest, str) or (
        state_digest and not _STATE_DIGEST_RE.fullmatch(state_digest)
    ):
        raise ValueError("generated_state_digest 必须为空值或 64 位 SHA-256")
    if not isinstance(active, Mapping) or not all(
        isinstance(bundle_id, str)
        and _BUNDLE_ID_RE.fullmatch(bundle_id)
        and isinstance(digest, str)
        and _STATE_DIGEST_RE.fullmatch(digest)
        for bundle_id, digest in active.items()
    ):
        raise ValueError("generated_active 必须是安全工具包到 64 位哈希的映射")
    return immutable_mapping(active)


def immutable_value(value: Any) -> Any:
    """Detach and recursively freeze values published in a runtime generation."""
    if isinstance(value, (ToolArtifact, ToolContractSnapshot)):
        # Both types deep-freeze their JSON members, bind all security-relevant
        # fields into a digest, and return themselves from __deepcopy__.
        return value
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
    if isinstance(value, (ToolArtifact, ToolContractSnapshot)):
        return value
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
    generated_state_revision: int = 0
    generated_state_digest: str = ""
    generated_active: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.model_state is not None:
            from .model_selector import ModelRuntimeState

            if not isinstance(self.model_state, ModelRuntimeState):
                raise ValueError(
                    "RuntimeSnapshot.model_state 必须是 ModelRuntimeState"
                )
        if self.tool_snapshot is not None:
            from .tool_manager import ToolSnapshot

            if not isinstance(self.tool_snapshot, ToolSnapshot):
                raise ValueError("RuntimeSnapshot.tool_snapshot 必须是 ToolSnapshot")
        for field_name in (
            "config",
            "temperaments",
            "temperament_assignments",
            "replies",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, Mapping):
                raise ValueError(f"RuntimeSnapshot.{field_name} 必须是映射")
            object.__setattr__(self, field_name, immutable_mapping(value))
        emotions = tuple(self.emotions)
        if not all(isinstance(emotion, str) for emotion in emotions):
            raise ValueError("RuntimeSnapshot.emotions 必须是字符串元组")
        object.__setattr__(self, "emotions", emotions)
        object.__setattr__(
            self,
            "generated_active",
            validate_generated_stamp(
                self.generated_state_revision,
                self.generated_state_digest,
                self.generated_active,
            ),
        )


_EXPECTED_CURRENT_UNSET = object()


class RuntimeSnapshotStore:
    """Atomically publishes generations and pins one generation to a request."""

    def __init__(self) -> None:
        self._current: RuntimeSnapshot | None = None
        self._lock = RLock()
        self._bound: ContextVar[RuntimeSnapshot | None] = ContextVar(
            "moellm_runtime_snapshot", default=None
        )

    def current(self) -> RuntimeSnapshot | None:
        with self._lock:
            return self._current

    def active(self) -> RuntimeSnapshot | None:
        bound = self._bound.get()
        return bound if bound is not None else self.current()

    def bound(self) -> RuntimeSnapshot | None:
        return self._bound.get()

    def publish(
        self,
        snapshot: RuntimeSnapshot,
        *,
        expected_current: RuntimeSnapshot | object | None = _EXPECTED_CURRENT_UNSET,
    ) -> None:
        with self._lock:
            current = self._current
            if (
                expected_current is not _EXPECTED_CURRENT_UNSET
                and current is not expected_current
            ):
                raise RuntimeError("runtime snapshot CAS 冲突")
            if current is not None and snapshot.generation <= current.generation:
                raise ValueError("runtime snapshot generation 必须严格递增")
            self._current = snapshot

    def patch_current(
        self,
        *,
        expected_current: RuntimeSnapshot | object | None = _EXPECTED_CURRENT_UNSET,
        **changes: Any,
    ) -> None:
        with self._lock:
            current = self._current
            if (
                expected_current is not _EXPECTED_CURRENT_UNSET
                and current is not expected_current
            ):
                raise RuntimeError("runtime snapshot CAS 冲突")
            if current is not None:
                self._current = replace(current, **changes)

    @contextmanager
    def bind(self, snapshot: RuntimeSnapshot | None) -> Iterator[None]:
        token = self._bound.set(snapshot)
        try:
            yield
        finally:
            self._bound.reset(token)


runtime_snapshots = RuntimeSnapshotStore()

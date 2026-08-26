from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Hashable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Protocol

from .runtime_metrics import runtime_metrics


class AdmissionRejected(RuntimeError):
    pass


class AdmissionGateProtocol(Protocol):
    """Backend-neutral context-manager boundary used by admission callers."""

    def slot(
        self,
        key: Hashable | None = None,
    ) -> AbstractAsyncContextManager[None]: ...


@dataclass
class _Counters:
    active: int = 0
    pending: int = 0


class AdmissionController:
    """A bounded async gate; waiting tasks are counted before they can suspend."""

    def __init__(
        self,
        *,
        name: str,
        max_active: int,
        max_pending: int,
        max_per_key: int | None = None,
    ) -> None:
        self.name = name
        self.max_active = max_active
        self.max_pending = max_pending
        self.max_per_key = max_per_key
        self._condition = asyncio.Condition()
        self._counters = _Counters()
        self._per_key: dict[Hashable, int] = {}
        self._active_keys: set[Hashable] = set()

    @property
    def active(self) -> int:
        return self._counters.active

    @property
    def pending(self) -> int:
        return self._counters.pending

    async def _reserve(self, key: Hashable | None) -> None:
        async with self._condition:
            if self._counters.pending >= self.max_pending:
                self._rejected()
                raise AdmissionRejected(f"{self.name} queue is full")
            if key is not None and self.max_per_key is not None:
                key_count = self._per_key.get(key, 0)
                if key_count >= self.max_per_key:
                    self._rejected()
                    raise AdmissionRejected(f"{self.name} per-user limit reached")
                self._per_key[key] = key_count + 1
            self._counters.pending += 1
            self._publish()

    async def _release(self, key: Hashable | None) -> None:
        async with self._condition:
            self._counters.active -= 1
            if key is not None:
                self._active_keys.discard(key)
            if key is not None and self.max_per_key is not None:
                remaining = self._per_key.get(key, 1) - 1
                if remaining > 0:
                    self._per_key[key] = remaining
                else:
                    self._per_key.pop(key, None)
            self._publish()
            self._condition.notify_all()

    async def _unreserve(self, key: Hashable | None) -> None:
        async with self._condition:
            self._counters.pending -= 1
            if key is not None and self.max_per_key is not None:
                remaining = self._per_key.get(key, 1) - 1
                if remaining > 0:
                    self._per_key[key] = remaining
                else:
                    self._per_key.pop(key, None)
            self._publish()
            self._condition.notify_all()

    def _can_activate(self, key: Hashable | None) -> bool:
        return self._counters.active < self.max_active and (
            key is None or key not in self._active_keys
        )

    @asynccontextmanager
    async def slot(self, key: Hashable | None = None) -> AsyncIterator[None]:
        await self._reserve(key)
        activated = False
        try:
            async with self._condition:
                await self._condition.wait_for(lambda: self._can_activate(key))
                self._counters.pending -= 1
                self._counters.active += 1
                if key is not None:
                    self._active_keys.add(key)
                activated = True
                self._publish()
            yield
        finally:
            if activated:
                await self._release(key)
            else:
                await self._unreserve(key)

    def _rejected(self) -> None:
        if self.name == "llm":
            runtime_metrics.llm_rejected += 1
        else:
            runtime_metrics.dispatch_rejected += 1

    def _publish(self) -> None:
        if self.name == "llm":
            runtime_metrics.llm_active = self.active
            runtime_metrics.llm_pending = self.pending
        else:
            runtime_metrics.dispatch_active = self.active
            runtime_metrics.dispatch_pending = self.pending


_llm_controller: AdmissionController | None = None
_dispatch_controller: AdmissionController | None = None


def get_llm_controller() -> AdmissionController:
    global _llm_controller
    from .config import config_parser

    limits = (
        config_parser.get_config("llm_max_active", 4),
        config_parser.get_config("llm_max_pending", 32),
        config_parser.get_config("llm_max_per_user", 2),
    )
    if _llm_controller is None or (
        (_llm_controller.active, _llm_controller.pending) == (0, 0)
        and (
            _llm_controller.max_active,
            _llm_controller.max_pending,
            _llm_controller.max_per_key,
        )
        != limits
    ):
        _llm_controller = AdmissionController(
            name="llm",
            max_active=limits[0],
            max_pending=limits[1],
            max_per_key=limits[2],
        )
    return _llm_controller


def get_dispatch_controller() -> AdmissionController:
    global _dispatch_controller
    from .config import config_parser

    max_pending = config_parser.get_config("legacy_dispatch_max_pending", 16)
    if _dispatch_controller is None or (
        (_dispatch_controller.active, _dispatch_controller.pending) == (0, 0)
        and _dispatch_controller.max_pending != max_pending
    ):
        _dispatch_controller = AdmissionController(
            name="dispatch",
            max_active=1,
            max_pending=max_pending,
        )
    return _dispatch_controller

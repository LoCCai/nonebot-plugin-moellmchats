from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import Enum
import inspect
import os
from types import MappingProxyType
from typing import Any, TypeAlias

from .agent_runtime import DeadlineContext
from .compat import timeout as timeout_scope
from .tool_contracts import ToolEffect
from .tool_providers import (
    ProviderCatalogSnapshot,
    ToolExecutionBoundary,
    ToolResultProvenance,
    ToolSource,
    ToolTrustDecision,
    ToolTrustLevel,
    ToolTrustOperation,
)

TrustedRunnerInvocation: TypeAlias = Callable[
    [Mapping[str, Any]],
    Awaitable[Any],
]

_RUNTIME_ARGUMENTS = frozenset({"_bot", "_event", "_tool_context", "_tool_manager"})


class TrustedRunnerPoolError(RuntimeError):
    """A trusted runner pool rejected or failed one bounded operation."""


class TrustedRunnerEligibilityError(TrustedRunnerPoolError):
    """A tool is not eligible for the explicitly reviewed runner pool."""


class TrustedRunnerPoolLifecycleError(TrustedRunnerPoolError):
    """The pool is not in a lifecycle state that accepts the operation."""


class TrustedRunnerPoolOwnershipError(TrustedRunnerPoolError):
    """A pool was reused across its bound process or event-loop boundary."""


class TrustedRunnerPoolBusy(TrustedRunnerPoolError):
    """The bounded outstanding-work limit was reached."""


class TrustedRunnerPoolClosed(TrustedRunnerPoolError):
    """The pool closed before one accepted invocation completed."""


class TrustedRunnerExecutionError(TrustedRunnerPoolError):
    """A trusted invocation failed without exposing its exception text."""


class TrustedRunnerExecutionTimeout(TrustedRunnerPoolError):
    """The request's shared deadline expired before completion."""


class TrustedRunnerPoolState(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    CLOSING = "closing"
    CLOSED = "closed"
    FAILED = "failed"


@dataclass(frozen=True)
class TrustedRunnerPoolPolicy:
    worker_count: int = 4
    max_outstanding: int = 64

    def __post_init__(self) -> None:
        if not isinstance(self.worker_count, int) or isinstance(self.worker_count, bool) or not 1 <= self.worker_count <= 64:
            raise ValueError("trusted runner worker_count 必须在 1 到 64 之间")
        if (
            not isinstance(self.max_outstanding, int)
            or isinstance(self.max_outstanding, bool)
            or not self.worker_count <= self.max_outstanding <= 4096
        ):
            raise ValueError("trusted runner max_outstanding 必须不小于 worker_count 且不超过 4096")


@dataclass(frozen=True)
class TrustedRunnerExecutionReport:
    tool_name: str
    generation: int
    worker_id: int
    decision: ToolTrustDecision
    result: Any

    def __post_init__(self) -> None:
        if not isinstance(self.decision, ToolTrustDecision):
            raise ValueError("trusted runner report 必须绑定 trust decision")
        if self.tool_name != self.decision.tool_name:
            raise ValueError("trusted runner report 工具身份不一致")
        if self.generation != self.decision.generation:
            raise ValueError("trusted runner report generation 不一致")
        if not isinstance(self.worker_id, int) or isinstance(self.worker_id, bool) or self.worker_id <= 0:
            raise ValueError("trusted runner report worker_id 必须是正整数")
        if (
            not self.decision.allowed
            or self.decision.operation is not ToolTrustOperation.EXECUTION
            or self.decision.trust is not ToolTrustLevel.TRUSTED
            or self.decision.boundary is not ToolExecutionBoundary.IN_PROCESS
            or self.decision.result_provenance is not ToolResultProvenance.UNVERIFIED
            or self.decision.effect is not ToolEffect.READ_ONLY
            or self.decision.confirmation_required
        ):
            raise ValueError("trusted runner report decision 不满足池化边界")


@dataclass(frozen=True)
class TrustedRunnerPoolSnapshot:
    generation: int
    state: TrustedRunnerPoolState
    eligible_tools: tuple[str, ...]
    worker_count: int
    max_outstanding: int
    pending: int
    active: int
    completed: int
    failed: int
    timed_out: int
    cancelled: int
    rejected: int

    def __post_init__(self) -> None:
        if not isinstance(self.state, TrustedRunnerPoolState):
            raise ValueError("trusted runner snapshot state 非法")
        if not isinstance(self.generation, int) or isinstance(self.generation, bool) or self.generation < 0:
            raise ValueError("trusted runner snapshot generation 非法")
        if (
            not isinstance(self.eligible_tools, tuple)
            or not self.eligible_tools
            or tuple(sorted(self.eligible_tools)) != self.eligible_tools
            or len(set(self.eligible_tools)) != len(self.eligible_tools)
        ):
            raise ValueError("trusted runner snapshot eligible_tools 非法")
        for name in (
            "worker_count",
            "max_outstanding",
            "pending",
            "active",
            "completed",
            "failed",
            "timed_out",
            "cancelled",
            "rejected",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"trusted runner snapshot {name} 非法")
        if self.active > self.worker_count:
            raise ValueError("trusted runner snapshot active 超过 worker_count")
        if self.pending + self.active > self.max_outstanding:
            raise ValueError("trusted runner snapshot outstanding 超过上限")


class _InvocationFailed(RuntimeError):
    pass


@dataclass
class _WorkItem:
    item_id: int
    tool_name: str
    invocation: TrustedRunnerInvocation
    dependencies: Mapping[str, Any]
    deadline: DeadlineContext
    decision: ToolTrustDecision
    result: asyncio.Future[TrustedRunnerExecutionReport]
    cancel_event: asyncio.Event
    finished: asyncio.Event
    started: bool = False
    worker_id: int | None = None
    cancel_reason: str | None = None


def _is_async_callable(value: object) -> bool:
    return inspect.iscoroutinefunction(value) or (
        callable(value) and inspect.iscoroutinefunction(getattr(value, "__call__", None))
    )


def _callable_signature(value: object, *, label: str) -> inspect.Signature:
    if not callable(value):
        raise TrustedRunnerEligibilityError(f"trusted runner {label} 必须可调用")
    try:
        return inspect.signature(value)
    except (TypeError, ValueError):
        raise TrustedRunnerEligibilityError(f"trusted runner {label} 参数签名不可检查") from None


class TrustedRunnerPool:
    """A generation-pinned, bounded pool for reviewed trusted async handlers.

    The pool is deliberately an in-process coordination primitive. It does not
    elevate trust, grant capabilities, inject Bot/Event objects, or replace the
    one-call-one-process Generated Tool sandbox. Callers must explicitly opt in
    each eligible tool and explicitly own start/close lifecycle boundaries.
    """

    def __init__(
        self,
        *,
        catalog: ProviderCatalogSnapshot,
        eligible_tools: tuple[str, ...],
        policy: TrustedRunnerPoolPolicy | None = None,
    ) -> None:
        if not isinstance(catalog, ProviderCatalogSnapshot):
            raise TypeError("trusted runner catalog 必须是 ProviderCatalogSnapshot")
        if (
            not isinstance(eligible_tools, tuple)
            or not eligible_tools
            or not all(isinstance(name, str) and name for name in eligible_tools)
        ):
            raise TrustedRunnerEligibilityError("trusted runner eligible_tools 必须是非空工具名元组")
        if len(set(eligible_tools)) != len(eligible_tools):
            raise TrustedRunnerEligibilityError("trusted runner eligible_tools 不得重复")
        if policy is None:
            policy = TrustedRunnerPoolPolicy()
        if not isinstance(policy, TrustedRunnerPoolPolicy):
            raise TypeError("trusted runner policy 必须是 TrustedRunnerPoolPolicy")

        ordered_tools = tuple(sorted(eligible_tools))
        for tool_name in ordered_tools:
            self._validate_eligible_tool(catalog, tool_name)

        self._catalog = catalog
        self._eligible_tools = ordered_tools
        self._eligible_set = frozenset(ordered_tools)
        self._policy = policy
        self._state = TrustedRunnerPoolState.CREATED
        self._owner_pid: int | None = None
        self._owner_loop: asyncio.AbstractEventLoop | None = None
        self._wake_event: asyncio.Event | None = None
        self._workers: tuple[asyncio.Task[None], ...] = ()
        self._pending_items: deque[_WorkItem] = deque()
        self._items: dict[int, _WorkItem] = {}
        self._next_item_id = 1
        self._pending = 0
        self._active = 0
        self._completed = 0
        self._failed = 0
        self._timed_out = 0
        self._cancelled = 0
        self._rejected = 0

    @staticmethod
    def _validate_eligible_tool(
        catalog: ProviderCatalogSnapshot,
        tool_name: str,
    ) -> None:
        trust = catalog.trust_policy_for(tool_name)
        if (
            trust.trust is not ToolTrustLevel.TRUSTED
            or trust.source not in {ToolSource.REGISTERED, ToolSource.BUILTIN}
            or trust.boundary is not ToolExecutionBoundary.IN_PROCESS
            or trust.result_provenance is not ToolResultProvenance.UNVERIFIED
            or trust.spec.effect is not ToolEffect.READ_ONLY
            or trust.confirmation_required
            or trust.spec.policy is not None
        ):
            raise TrustedRunnerEligibilityError(f"工具 {tool_name} 不满足 trusted in-process read_only 池化边界")
        if not _is_async_callable(trust.spec.handler):
            raise TrustedRunnerEligibilityError(f"工具 {tool_name} 必须是可取消的 async handler")
        signature = _callable_signature(
            trust.spec.handler,
            label=f"工具 {tool_name} handler",
        )
        runtime_arguments = tuple(sorted(_RUNTIME_ARGUMENTS.intersection(signature.parameters)))
        if runtime_arguments:
            raise TrustedRunnerEligibilityError(f"工具 {tool_name} 需要主进程 runtime 参数，禁止池化")

    @property
    def generation(self) -> int:
        return self._catalog.generation

    @property
    def state(self) -> TrustedRunnerPoolState:
        return self._state

    @property
    def eligible_tools(self) -> tuple[str, ...]:
        return self._eligible_tools

    def snapshot(self) -> TrustedRunnerPoolSnapshot:
        return TrustedRunnerPoolSnapshot(
            generation=self.generation,
            state=self._state,
            eligible_tools=self._eligible_tools,
            worker_count=self._policy.worker_count,
            max_outstanding=self._policy.max_outstanding,
            pending=self._pending,
            active=self._active,
            completed=self._completed,
            failed=self._failed,
            timed_out=self._timed_out,
            cancelled=self._cancelled,
            rejected=self._rejected,
        )

    def _require_owner(self) -> None:
        if self._owner_pid is None or self._owner_loop is None:
            raise TrustedRunnerPoolLifecycleError("trusted runner pool 尚未启动")
        if os.getpid() != self._owner_pid:
            raise TrustedRunnerPoolOwnershipError("trusted runner pool 禁止跨进程复用")
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            raise TrustedRunnerPoolOwnershipError("trusted runner pool 必须在绑定 event loop 中使用") from None
        if loop is not self._owner_loop:
            raise TrustedRunnerPoolOwnershipError("trusted runner pool 禁止跨 event loop 复用")

    async def start(self) -> TrustedRunnerPool:
        if self._state is TrustedRunnerPoolState.RUNNING:
            self._require_owner()
            return self
        if self._state is not TrustedRunnerPoolState.CREATED:
            raise TrustedRunnerPoolLifecycleError("trusted runner pool 关闭或失败后不得重启")
        self._owner_pid = os.getpid()
        self._owner_loop = asyncio.get_running_loop()
        self._wake_event = asyncio.Event()
        self._state = TrustedRunnerPoolState.RUNNING
        self._workers = tuple(
            asyncio.create_task(
                self._worker_loop(worker_id),
                name=f"moellm-trusted-runner-{self.generation}-{worker_id}",
            )
            for worker_id in range(1, self._policy.worker_count + 1)
        )
        return self

    async def __aenter__(self) -> TrustedRunnerPool:  # noqa: PYI034
        return await self.start()

    async def __aexit__(
        self,
        _exc_type: object,
        _exc: object,
        _traceback: object,
    ) -> None:
        await self.close()

    @staticmethod
    def _validate_invocation(
        tool_name: str,
        invocation: TrustedRunnerInvocation,
    ) -> None:
        if not _is_async_callable(invocation):
            raise TrustedRunnerEligibilityError(f"工具 {tool_name} runner invocation 必须是 async callable")
        signature = _callable_signature(
            invocation,
            label=f"工具 {tool_name} invocation",
        )
        try:
            signature.bind(MappingProxyType({}))
        except TypeError:
            raise TrustedRunnerEligibilityError(f"工具 {tool_name} runner invocation 必须只需依赖结果映射") from None

    async def execute(
        self,
        *,
        tool_name: str,
        invocation: TrustedRunnerInvocation,
        dependencies: Mapping[str, Any],
        deadline: DeadlineContext,
        is_superuser: bool,
        confirmed: bool = False,
    ) -> TrustedRunnerExecutionReport:
        self._require_owner()
        if self._state is not TrustedRunnerPoolState.RUNNING:
            raise TrustedRunnerPoolLifecycleError("trusted runner pool 当前不接受执行")
        if tool_name not in self._eligible_set:
            self._rejected += 1
            raise TrustedRunnerEligibilityError(f"工具 {tool_name} 未显式加入 trusted runner pool")
        self._validate_invocation(tool_name, invocation)
        if not isinstance(dependencies, Mapping):
            raise TrustedRunnerEligibilityError(f"工具 {tool_name} dependencies 必须是映射")
        if not isinstance(deadline, DeadlineContext):
            raise TrustedRunnerEligibilityError("trusted runner deadline 必须是 DeadlineContext")

        decision = self._catalog.require_trust(
            tool_name,
            ToolTrustOperation.EXECUTION,
            is_superuser=is_superuser,
            confirmed=confirmed,
        )
        if tool_name not in self._eligible_set:
            raise TrustedRunnerEligibilityError(f"工具 {tool_name} trust identity 已漂移")
        remaining = deadline.remaining()
        if remaining <= 0:
            self._timed_out += 1
            raise TrustedRunnerExecutionTimeout(f"工具 {tool_name} 入池前共享 deadline 已耗尽")
        if len(self._items) >= self._policy.max_outstanding:
            self._rejected += 1
            raise TrustedRunnerPoolBusy("trusted runner pool outstanding 已满")

        loop = asyncio.get_running_loop()
        item_id = self._next_item_id
        self._next_item_id += 1
        item = _WorkItem(
            item_id=item_id,
            tool_name=tool_name,
            invocation=invocation,
            dependencies=MappingProxyType(dict(dependencies)),
            deadline=deadline,
            decision=decision,
            result=loop.create_future(),
            cancel_event=asyncio.Event(),
            finished=asyncio.Event(),
        )
        self._items[item_id] = item
        self._pending_items.append(item)
        self._pending += 1
        assert self._wake_event is not None
        self._wake_event.set()

        try:
            async with timeout_scope(remaining):
                return await asyncio.shield(item.result)
        except TimeoutError:
            await self._cancel_and_drain(item, reason="timeout")
            raise TrustedRunnerExecutionTimeout(f"工具 {tool_name} 执行耗尽共享 deadline") from None
        except asyncio.CancelledError:
            await self._cancel_and_drain(item, reason="caller")
            raise

    async def _cancel_and_drain(
        self,
        item: _WorkItem,
        *,
        reason: str,
    ) -> None:
        if item.finished.is_set():
            return
        if item.cancel_reason is None:
            item.cancel_reason = reason
        if not item.result.done():
            item.result.cancel()
        item.cancel_event.set()
        if not item.started:
            try:
                self._pending_items.remove(item)
            except ValueError:
                pass
            else:
                self._pending -= 1
                self._finish_item(item, outcome=reason)
                return
        await asyncio.shield(item.finished.wait())

    async def _next_item(self) -> _WorkItem | None:
        assert self._wake_event is not None
        while True:
            if self._pending_items:
                item = self._pending_items.popleft()
                self._pending -= 1
                item.started = True
                return item
            if self._state is not TrustedRunnerPoolState.RUNNING:
                return None
            self._wake_event.clear()
            if self._pending_items:
                self._wake_event.set()
                continue
            await self._wake_event.wait()

    @staticmethod
    async def _invoke(item: _WorkItem) -> Any:
        try:
            return await item.invocation(item.dependencies)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise _InvocationFailed from None

    async def _worker_loop(self, worker_id: int) -> None:
        current_item: _WorkItem | None = None
        try:
            while True:
                current_item = await self._next_item()
                if current_item is None:
                    return
                current_item.worker_id = worker_id
                self._active += 1
                try:
                    await self._process_item(current_item)
                finally:
                    self._active -= 1
                current_item = None
        except asyncio.CancelledError:
            raise
        except BaseException:
            self._state = TrustedRunnerPoolState.FAILED
            if current_item is not None and not current_item.finished.is_set():
                self._set_item_exception(
                    current_item,
                    TrustedRunnerPoolClosed("trusted runner pool worker 意外终止"),
                )
                self._finish_item(current_item, outcome="failed")
            self._fail_all_items()
            raise

    async def _process_item(self, item: _WorkItem) -> None:
        if item.cancel_reason is not None:
            self._finish_item(item, outcome=item.cancel_reason)
            return
        remaining = item.deadline.remaining()
        if remaining <= 0:
            self._set_item_exception(
                item,
                TrustedRunnerExecutionTimeout(f"工具 {item.tool_name} 排队期间共享 deadline 已耗尽"),
            )
            self._finish_item(item, outcome="timeout")
            return

        invocation_task = asyncio.create_task(
            self._invoke(item),
            name=f"moellm-trusted-invocation-{self.generation}-{item.item_id}",
        )
        cancellation_task = asyncio.create_task(item.cancel_event.wait())
        try:
            async with timeout_scope(remaining):
                done, _ = await asyncio.wait(
                    {invocation_task, cancellation_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if item.cancel_reason is not None or cancellation_task in done:
                    if not invocation_task.done():
                        invocation_task.cancel()
                    await asyncio.gather(invocation_task, return_exceptions=True)
                    self._finish_item(
                        item,
                        outcome=item.cancel_reason or "caller",
                    )
                    return
                if invocation_task.cancelled():
                    self._set_item_exception(
                        item,
                        TrustedRunnerExecutionError(f"工具 {item.tool_name} 任务发生非调用方取消"),
                    )
                    self._finish_item(item, outcome="failed")
                    return
                error = invocation_task.exception()
                if error is not None:
                    self._set_item_exception(
                        item,
                        TrustedRunnerExecutionError(f"工具 {item.tool_name} trusted invocation 执行失败"),
                    )
                    self._finish_item(item, outcome="failed")
                    return
                assert item.worker_id is not None
                if not item.result.done():
                    item.result.set_result(
                        TrustedRunnerExecutionReport(
                            tool_name=item.tool_name,
                            generation=self.generation,
                            worker_id=item.worker_id,
                            decision=item.decision,
                            result=invocation_task.result(),
                        )
                    )
                self._finish_item(item, outcome="completed")
        except TimeoutError:
            if not invocation_task.done():
                invocation_task.cancel()
            await asyncio.gather(invocation_task, return_exceptions=True)
            self._set_item_exception(
                item,
                TrustedRunnerExecutionTimeout(f"工具 {item.tool_name} 执行耗尽共享 deadline"),
            )
            self._finish_item(item, outcome="timeout")
        finally:
            if not cancellation_task.done():
                cancellation_task.cancel()
            await asyncio.gather(cancellation_task, return_exceptions=True)

    @staticmethod
    def _set_item_exception(
        item: _WorkItem,
        error: TrustedRunnerPoolError,
    ) -> None:
        if not item.result.done():
            item.result.set_exception(error)

    def _finish_item(self, item: _WorkItem, *, outcome: str) -> None:
        if item.finished.is_set():
            return
        self._items.pop(item.item_id, None)
        if outcome == "completed":
            self._completed += 1
        elif outcome == "failed":
            self._failed += 1
        elif outcome == "timeout":
            self._timed_out += 1
        else:
            self._cancelled += 1
            if outcome == "close":
                self._set_item_exception(
                    item,
                    TrustedRunnerPoolClosed(f"工具 {item.tool_name} 因 trusted runner pool 关闭而取消"),
                )
        item.finished.set()

    def _fail_all_items(self) -> None:
        error = TrustedRunnerPoolClosed("trusted runner pool worker 意外终止")
        while self._pending_items:
            item = self._pending_items.popleft()
            self._pending -= 1
            self._set_item_exception(item, error)
            self._finish_item(item, outcome="failed")
        for item in tuple(self._items.values()):
            if item.finished.is_set():
                continue
            item.cancel_reason = "close"
            item.cancel_event.set()
            self._set_item_exception(item, error)
        if self._wake_event is not None:
            self._wake_event.set()

    async def close(self) -> None:
        if self._state is TrustedRunnerPoolState.CREATED:
            self._state = TrustedRunnerPoolState.CLOSED
            return
        if self._state is TrustedRunnerPoolState.CLOSED:
            return
        self._require_owner()
        if self._state is TrustedRunnerPoolState.CLOSING:
            await asyncio.gather(*self._workers, return_exceptions=True)
            return

        self._state = TrustedRunnerPoolState.CLOSING
        while self._pending_items:
            item = self._pending_items.popleft()
            self._pending -= 1
            item.cancel_reason = "close"
            self._finish_item(item, outcome="close")
        active_items = tuple(item for item in self._items.values() if not item.finished.is_set())
        for item in active_items:
            if item.cancel_reason is None:
                item.cancel_reason = "close"
            item.cancel_event.set()
        assert self._wake_event is not None
        self._wake_event.set()
        if active_items:
            await asyncio.gather(*(item.finished.wait() for item in active_items))
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers = ()
        if self._state is not TrustedRunnerPoolState.FAILED:
            self._state = TrustedRunnerPoolState.CLOSED

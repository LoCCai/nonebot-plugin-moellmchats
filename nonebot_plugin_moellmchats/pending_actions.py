from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
import json
import math
import re
import secrets
import time
from typing import Any
import uuid

from .config import config_parser
from .tool_contracts import ToolEffect, ToolResult, validate_tool_arguments
from .tool_execution import execute_custom_tool

_NONCE_RE = re.compile(r"^[A-F0-9]{6}$")
_CallerKey = tuple[str, str, str, str | None]


class PendingActionError(RuntimeError):
    """A pending action could not be safely created, resolved, or executed."""


def canonicalize_arguments(arguments: dict[str, Any]) -> str:
    try:
        return json.dumps(
            arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise PendingActionError("工具参数无法固化，危险操作已拒绝") from error


def hash_arguments(arguments_json: str) -> str:
    return hashlib.sha256(arguments_json.encode("utf-8")).hexdigest()


def _bot_id(bot: Any) -> str:
    return str(getattr(bot, "self_id", ""))


def _adapter_id(bot: Any) -> str:
    adapter = getattr(bot, "adapter", None)
    adapter_name = getattr(adapter, "get_name", None)
    if callable(adapter_name):
        try:
            return str(adapter_name())
        except Exception:
            pass
    return f"{type(bot).__module__}.{type(bot).__qualname__}"


def _group_id(event: Any) -> str | None:
    value = getattr(event, "group_id", None)
    return None if value is None else str(value)


def _caller_key(bot: Any, event: Any) -> _CallerKey:
    return (
        _bot_id(bot),
        _adapter_id(bot),
        str(getattr(event, "user_id", "")),
        _group_id(event),
    )


@dataclass(frozen=True)
class PendingAction:
    action_id: str
    bot_id: str
    adapter_id: str
    user_id: str
    group_id: str | None
    tool_name: str
    arguments_json: str
    arguments_hash: str
    generation: int
    bundle_digest: str | None
    created_at: float
    expires_at: float
    nonce: str

    def arguments(self) -> dict[str, Any]:
        try:
            value = json.loads(self.arguments_json)
        except (TypeError, ValueError) as error:
            raise PendingActionError("待确认参数已损坏，操作已拒绝") from error
        if not isinstance(value, dict):
            raise PendingActionError("待确认参数不是 JSON 对象，操作已拒绝")
        if hash_arguments(self.arguments_json) != self.arguments_hash:
            raise PendingActionError("待确认参数校验失败，操作已拒绝")
        return value


@dataclass(frozen=True)
class _FailureWindow:
    started_at: float
    failures: int


class PendingActionStore:
    """Bounded, one-shot in-memory confirmation store.

    The interface intentionally keeps Bot, Event, handlers, and other live objects out
    of stored state so a future Redis backend can implement the same contract.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        nonce_factory: Callable[[], str] | None = None,
    ) -> None:
        self._actions: OrderedDict[str, PendingAction] = OrderedDict()
        self._failure_windows: OrderedDict[_CallerKey, _FailureWindow] = (
            OrderedDict()
        )
        self._lock = asyncio.Lock()
        self._clock = clock
        self._nonce_factory = nonce_factory or (
            lambda: secrets.token_hex(3).upper()
        )

    def _prune_locked(self, now: float) -> None:
        for nonce in [
            nonce
            for nonce, action in self._actions.items()
            if action.expires_at <= now
        ]:
            self._actions.pop(nonce, None)

    def _new_nonce_locked(self) -> str:
        for _ in range(32):
            nonce = str(self._nonce_factory()).strip().upper()
            if _NONCE_RE.fullmatch(nonce) and nonce not in self._actions:
                return nonce
        raise PendingActionError("无法生成安全确认码，危险操作已拒绝")

    @staticmethod
    def _failure_limits() -> tuple[float, int, int]:
        window = config_parser.get_config(
            "pending_action_failure_window_seconds", 60
        )
        max_failures = config_parser.get_config("pending_action_max_failures", 8)
        max_keys = config_parser.get_config(
            "pending_action_max_failure_keys", 4_096
        )
        values = {
            "pending_action_failure_window_seconds": window,
            "pending_action_max_failures": max_failures,
            "pending_action_max_failure_keys": max_keys,
        }
        for field, value in values.items():
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise PendingActionError(f"{field} 配置无效")
        return float(window), max_failures, max_keys

    def _prune_failures_locked(
        self,
        now: float,
        *,
        window_seconds: float,
        max_keys: int,
    ) -> None:
        for caller in [
            caller
            for caller, failure in self._failure_windows.items()
            if failure.started_at + window_seconds <= now
        ]:
            self._failure_windows.pop(caller, None)
        while len(self._failure_windows) > max_keys:
            self._failure_windows.popitem(last=False)

    def _check_failure_budget_locked(
        self,
        caller: _CallerKey,
        now: float,
        *,
        window_seconds: float,
        max_failures: int,
        max_keys: int,
    ) -> None:
        self._prune_failures_locked(
            now,
            window_seconds=window_seconds,
            max_keys=max_keys,
        )
        failure = self._failure_windows.get(caller)
        if failure is not None and failure.failures >= max_failures:
            raise PendingActionError("确认码失败尝试过多，请稍后重试")

    def _record_failure_locked(
        self,
        caller: _CallerKey,
        now: float,
        *,
        window_seconds: float,
        max_keys: int,
    ) -> None:
        failure = self._failure_windows.get(caller)
        if failure is None or failure.started_at + window_seconds <= now:
            while len(self._failure_windows) >= max_keys:
                self._failure_windows.popitem(last=False)
            self._failure_windows[caller] = _FailureWindow(now, 1)
            return
        self._failure_windows[caller] = _FailureWindow(
            failure.started_at,
            failure.failures + 1,
        )
        self._failure_windows.move_to_end(caller)

    def _clear_failures_locked(self, caller: _CallerKey) -> None:
        self._failure_windows.pop(caller, None)

    async def create(
        self,
        *,
        bot: Any,
        event: Any,
        tool_name: str,
        arguments: dict[str, Any],
        generation: int,
        bundle_digest: str | None = None,
    ) -> PendingAction:
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
            raise PendingActionError("运行 generation 无效，危险操作已拒绝")
        arguments_json = canonicalize_arguments(arguments)
        now = self._clock()
        ttl = config_parser.get_config("pending_action_ttl_seconds", 120)
        limit = config_parser.get_config("pending_action_max_entries", 256)
        argument_limit = config_parser.get_config(
            "pending_action_max_argument_bytes", 16_384
        )
        if not isinstance(ttl, (int, float)) or isinstance(ttl, bool) or ttl <= 0:
            raise PendingActionError("待确认操作 TTL 配置无效")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise PendingActionError("待确认操作容量配置无效")
        if (
            not isinstance(argument_limit, int)
            or isinstance(argument_limit, bool)
            or argument_limit <= 0
        ):
            raise PendingActionError("待确认参数大小配置无效")
        if len(arguments_json.encode("utf-8")) > argument_limit:
            raise PendingActionError("待确认工具参数超过大小限制")
        async with self._lock:
            self._prune_locked(now)
            arguments_hash = hash_arguments(arguments_json)
            caller = (
                _bot_id(bot),
                _adapter_id(bot),
                str(getattr(event, "user_id", "")),
                _group_id(event),
                tool_name,
            )
            for old_nonce, old_action in list(self._actions.items()):
                old_caller = (
                    old_action.bot_id,
                    old_action.adapter_id,
                    old_action.user_id,
                    old_action.group_id,
                    old_action.tool_name,
                )
                if old_caller != caller:
                    continue
                if (
                    old_action.arguments_hash == arguments_hash
                    and old_action.generation == generation
                    and old_action.bundle_digest == bundle_digest
                ):
                    return old_action
                # Only the latest set of arguments for one tool/session may be
                # confirmed; an earlier nonce is invalidated to avoid ambiguity.
                self._actions.pop(old_nonce, None)
            if len(self._actions) >= limit:
                raise PendingActionError("待确认操作队列已满，请稍后重试")
            nonce = self._new_nonce_locked()
            action = PendingAction(
                action_id=uuid.uuid4().hex,
                bot_id=_bot_id(bot),
                adapter_id=_adapter_id(bot),
                user_id=str(getattr(event, "user_id", "")),
                group_id=_group_id(event),
                tool_name=tool_name,
                arguments_json=arguments_json,
                arguments_hash=arguments_hash,
                generation=generation,
                bundle_digest=bundle_digest,
                created_at=now,
                expires_at=now + float(ttl),
                nonce=nonce,
            )
            self._actions[nonce] = action
            return action

    @staticmethod
    def _matches_caller(action: PendingAction, bot: Any, event: Any) -> bool:
        return (
            action.bot_id == _bot_id(bot)
            and action.adapter_id == _adapter_id(bot)
            and action.user_id == str(getattr(event, "user_id", ""))
            and action.group_id == _group_id(event)
        )

    async def consume(
        self,
        nonce: str,
        *,
        bot: Any,
        event: Any,
        generation: int,
    ) -> PendingAction:
        normalized = nonce.strip().upper()
        now = self._clock()
        caller = _caller_key(bot, event)
        window_seconds, max_failures, max_keys = self._failure_limits()
        async with self._lock:
            self._check_failure_budget_locked(
                caller,
                now,
                window_seconds=window_seconds,
                max_failures=max_failures,
                max_keys=max_keys,
            )
            try:
                if not _NONCE_RE.fullmatch(normalized):
                    raise PendingActionError("确认码格式错误")
                action = self._actions.get(normalized)
                if action is None:
                    self._prune_locked(now)
                    raise PendingActionError("确认码不存在、已过期或已使用")
                if action.expires_at <= now:
                    self._actions.pop(normalized, None)
                    raise PendingActionError("确认码已过期")
                if not self._matches_caller(action, bot, event):
                    raise PendingActionError("确认码与当前用户或会话不匹配")
                if action.generation != generation:
                    self._actions.pop(normalized, None)
                    raise PendingActionError("工具已重载，原确认码已失效")
                # Consume before execution. A failed or cancelled side effect must
                # never be replayed automatically because its external completion
                # state is unknown.
                self._actions.pop(normalized, None)
                action.arguments()
            except PendingActionError:
                self._record_failure_locked(
                    caller,
                    now,
                    window_seconds=window_seconds,
                    max_keys=max_keys,
                )
                raise
            self._clear_failures_locked(caller)
            return action

    async def cancel(self, nonce: str, *, bot: Any, event: Any) -> None:
        normalized = nonce.strip().upper()
        now = self._clock()
        caller = _caller_key(bot, event)
        window_seconds, max_failures, max_keys = self._failure_limits()
        async with self._lock:
            self._check_failure_budget_locked(
                caller,
                now,
                window_seconds=window_seconds,
                max_failures=max_failures,
                max_keys=max_keys,
            )
            try:
                if not _NONCE_RE.fullmatch(normalized):
                    raise PendingActionError("确认码格式错误")
                self._prune_locked(now)
                action = self._actions.get(normalized)
                if action is None or not self._matches_caller(action, bot, event):
                    raise PendingActionError(
                        "确认码不存在、已过期或与当前会话不匹配"
                    )
                self._actions.pop(normalized, None)
            except PendingActionError:
                self._record_failure_locked(
                    caller,
                    now,
                    window_seconds=window_seconds,
                    max_keys=max_keys,
                )
                raise
            self._clear_failures_locked(caller)

    async def clear(self) -> None:
        async with self._lock:
            self._actions.clear()
            self._failure_windows.clear()

    def remaining_ttl_seconds(self, action: PendingAction) -> int:
        """Return the action's actual remaining TTL, rounded up for display."""

        if not isinstance(action, PendingAction):
            raise TypeError("action 必须是 PendingAction")
        return max(0, math.ceil(action.expires_at - self._clock()))

    async def size(self) -> int:
        async with self._lock:
            self._prune_locked(self._clock())
            return len(self._actions)


async def execute_pending_action(
    nonce: str,
    *,
    bot: Any,
    event: Any,
    runtime_snapshot: Any,
    store: PendingActionStore | None = None,
) -> tuple[PendingAction, ToolResult]:
    if runtime_snapshot is None:
        raise PendingActionError("LLM 运行快照尚未就绪，危险操作已拒绝")
    action_store = store or pending_action_store
    action = await action_store.consume(
        nonce,
        bot=bot,
        event=event,
        generation=runtime_snapshot.generation,
    )
    tool_snapshot = runtime_snapshot.tool_snapshot
    tool_entry = tool_snapshot.custom_tools.get(action.tool_name)
    if not isinstance(tool_entry, Mapping):
        raise PendingActionError("待确认工具已不可用，操作未执行")
    spec = tool_entry.get("tool_spec")
    if spec is None or spec.effect != ToolEffect.MUTATING:
        raise PendingActionError("待确认工具属性已变化，操作未执行")
    if action.bundle_digest != tool_entry.get("bundle_digest"):
        raise PendingActionError("待确认工具版本已变化，操作未执行")
    arguments = action.arguments()
    if error := validate_tool_arguments(arguments, spec.parameters):
        raise PendingActionError(f"待确认工具参数校验失败：{error}")
    try:
        result = await execute_custom_tool(
            action.tool_name,
            tool_entry,
            arguments,
            bot=bot,
            event=event,
            confirmed=True,
        )
    except PendingActionError:
        raise
    except Exception as error:
        raise PendingActionError(f"危险操作执行失败：{error!s}") from error
    return action, result


pending_action_store = PendingActionStore()

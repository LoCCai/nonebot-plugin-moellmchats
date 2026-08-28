from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import math
import re
import secrets
import time
from types import MappingProxyType
from typing import Any
import uuid

from nonebot.log import logger

from .config import config_parser
from .onebot_facade import (
    bot_self_id,
    coerce_action_identifier,
    event_group_id,
    event_user_id,
)
from .protocol_context import (
    ProtocolCapabilitySnapshot,
    ProtocolRequestBinding,
    current_protocol_binding,
    probe_protocol_capabilities,
    protocol_tool_available,
)
from .protocol_registry import (
    ProtocolWrapperPolicy,
    protocol_registry,
)
from .tool_contracts import ToolResult, render_tool_result, validate_tool_arguments

_NONCE_RE = re.compile(r"^[A-F0-9]{6}$")
_SENSITIVE_KEY_RE = re.compile(
    r"(?:authorization|base64|clientkey|cookie|credential|csrf|password|path|rkey|secret|token)",
    re.IGNORECASE,
)
_BASE64_RE = re.compile(r"^[A-Za-z0-9+/=_-]{256,}$")
_WINDOWS_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
_MAX_AUDITS = 2_048
_MAX_RATE_KEYS = 4_096
_MAX_SANITIZED_NODES = 2_000
_MAX_SANITIZED_DEPTH = 10
_MAX_SANITIZED_ITEMS = 100
_MAX_RESULT_CHARS = 24_000
_MAX_ARGUMENT_JSON_BYTES = 64 * 1024


class ProtocolExecutionError(RuntimeError):
    """A protocol action was rejected or failed with known non-success."""


class ProtocolInvocationStatus(str, Enum):
    COMPLETED = "completed"
    WAITING_CONFIRMATION = "waiting_confirmation"
    RESULT_UNKNOWN = "result_unknown"


@dataclass(frozen=True)
class ProtocolInvocation:
    status: ProtocolInvocationStatus
    result: ToolResult
    tool_name: str
    action: str
    confirmation_nonce: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, ProtocolInvocationStatus):
            raise TypeError("协议调用状态非法")
        if not isinstance(self.result, ToolResult):
            raise TypeError("协议调用结果必须是 ToolResult")
        if not isinstance(self.tool_name, str) or not isinstance(self.action, str):
            raise TypeError("协议调用 identity 非法")
        if self.status is ProtocolInvocationStatus.WAITING_CONFIRMATION:
            if not isinstance(self.confirmation_nonce, str) or not _NONCE_RE.fullmatch(self.confirmation_nonce):
                raise ValueError("待确认协议调用缺少确认码")
        elif self.confirmation_nonce is not None:
            raise ValueError("非待确认协议调用不得携带确认码")


@dataclass(frozen=True)
class ProtocolAuditRecord:
    audit_id: str
    created_at: float
    tool_name: str
    action: str
    protocol: str
    implementation: str
    bot_id: str
    actor_user_id: str
    scene: str
    generation: int
    policy_digest: str
    status: str
    result_digest: str


@dataclass(frozen=True)
class _PreparedAction:
    tool_name: str
    action: str
    api_protocol: str
    parameters: Mapping[str, Any]
    effect: str
    capability: str
    confirmation: str
    rate_limit: Mapping[str, Any]
    redact_fields: tuple[str, ...]
    policy_digest: str
    permission: str


@dataclass(frozen=True)
class ProtocolPendingAction:
    action_id: str
    nonce: str
    bot_id: str
    adapter_id: str
    protocol: str
    implementation: str
    implementation_version: str
    supported_actions_digest: str
    actor_user_id: str
    scene: str
    scene_id: str
    generation: int
    tool_name: str
    action: str
    parameters_json: str
    parameters_digest: str
    policy_digest: str
    created_at: float
    expires_at: float

    def parameters(self) -> dict[str, Any]:
        try:
            value = json.loads(self.parameters_json)
        except json.JSONDecodeError as error:
            raise ProtocolExecutionError("协议确认参数已损坏") from error
        if not isinstance(value, dict) or _digest_text(self.parameters_json) != self.parameters_digest:
            raise ProtocolExecutionError("协议确认参数摘要不一致")
        return value


def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_parameters(value: Mapping[str, Any]) -> str:
    try:
        return json.dumps(
            dict(value),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise ProtocolExecutionError("协议参数无法固化") from error


def _enforce_parameter_budget(value: Mapping[str, Any]) -> None:
    payload = _canonical_parameters(value)
    if len(payload.encode("utf-8")) > _MAX_ARGUMENT_JSON_BYTES:
        raise ProtocolExecutionError("协议参数 JSON 超过 64 KiB 上限")


def _scene_id(snapshot: ProtocolCapabilitySnapshot) -> str:
    if snapshot.scene == "group":
        return snapshot.group_id or ""
    if snapshot.scene == "channel":
        return f"{snapshot.guild_id or ''}/{snapshot.channel_id or ''}"
    return snapshot.actor_user_id


class ProtocolPendingActionStore:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        nonce_factory: Callable[[], str] | None = None,
    ) -> None:
        self._clock = clock
        self._nonce_factory = nonce_factory or (lambda: secrets.token_hex(3).upper())
        self._items: OrderedDict[str, ProtocolPendingAction] = OrderedDict()
        self._lock = asyncio.Lock()

    def _prune(self, now: float) -> None:
        for nonce in [key for key, item in self._items.items() if item.expires_at <= now]:
            self._items.pop(nonce, None)

    def _nonce(self) -> str:
        for _ in range(32):
            value = str(self._nonce_factory()).strip().upper()
            if _NONCE_RE.fullmatch(value) and value not in self._items:
                return value
        raise ProtocolExecutionError("无法生成协议确认码")

    async def create(
        self,
        binding: ProtocolRequestBinding,
        prepared: _PreparedAction,
    ) -> ProtocolPendingAction:
        snapshot = binding.snapshot
        now = self._clock()
        ttl = config_parser.get_config("pending_action_ttl_seconds", 120)
        limit = config_parser.get_config("pending_action_max_entries", 256)
        if (
            not isinstance(ttl, (int, float))
            or isinstance(ttl, bool)
            or ttl <= 0
            or not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit <= 0
        ):
            raise ProtocolExecutionError("协议确认存储配置非法")
        parameters_json = _canonical_parameters(prepared.parameters)
        async with self._lock:
            self._prune(now)
            identity = (
                snapshot.bot_id,
                snapshot.adapter_id,
                snapshot.actor_user_id,
                _scene_id(snapshot),
                prepared.tool_name,
            )
            for old_nonce, old in list(self._items.items()):
                old_identity = (
                    old.bot_id,
                    old.adapter_id,
                    old.actor_user_id,
                    old.scene_id,
                    old.tool_name,
                )
                if old_identity != identity:
                    continue
                if (
                    old.parameters_json == parameters_json
                    and old.generation == snapshot.runtime_generation
                    and old.policy_digest == prepared.policy_digest
                    and old.supported_actions_digest == snapshot.supported_actions_digest
                ):
                    return old
                self._items.pop(old_nonce, None)
            if len(self._items) >= limit:
                raise ProtocolExecutionError("协议确认队列已满")
            nonce = self._nonce()
            pending = ProtocolPendingAction(
                action_id=uuid.uuid4().hex,
                nonce=nonce,
                bot_id=snapshot.bot_id,
                adapter_id=snapshot.adapter_id,
                protocol=snapshot.protocol or "",
                implementation=snapshot.implementation,
                implementation_version=snapshot.implementation_version,
                supported_actions_digest=snapshot.supported_actions_digest,
                actor_user_id=snapshot.actor_user_id,
                scene=snapshot.scene,
                scene_id=_scene_id(snapshot),
                generation=snapshot.runtime_generation,
                tool_name=prepared.tool_name,
                action=prepared.action,
                parameters_json=parameters_json,
                parameters_digest=_digest_text(parameters_json),
                policy_digest=prepared.policy_digest,
                created_at=now,
                expires_at=now + float(ttl),
            )
            self._items[nonce] = pending
            return pending

    async def contains(self, nonce: str) -> bool:
        normalized = str(nonce).strip().upper()
        async with self._lock:
            self._prune(self._clock())
            return normalized in self._items

    async def consume(
        self,
        nonce: str,
        *,
        bot: Any,
        event: Any,
        generation: int,
    ) -> ProtocolPendingAction:
        normalized = str(nonce).strip().upper()
        now = self._clock()
        async with self._lock:
            self._prune(now)
            item = self._items.get(normalized)
            if item is None:
                raise ProtocolExecutionError("协议确认码不存在、已过期或已使用")
            if (
                item.bot_id != bot_self_id(bot, event)
                or item.adapter_id != _adapter_id(bot)
                or item.actor_user_id != event_user_id(event)
                or item.scene_id != _event_scene_id(event)
            ):
                raise ProtocolExecutionError("协议确认码与当前 Bot、用户或会话不匹配")
            if item.generation != generation:
                self._items.pop(normalized, None)
                raise ProtocolExecutionError("协议工具 generation 已变化，确认码失效")
            self._items.pop(normalized, None)
            item.parameters()
            return item

    async def cancel(self, nonce: str, *, bot: Any, event: Any) -> None:
        normalized = str(nonce).strip().upper()
        async with self._lock:
            self._prune(self._clock())
            item = self._items.get(normalized)
            if item is None:
                raise ProtocolExecutionError("协议确认码不存在或已过期")
            if (
                item.bot_id != bot_self_id(bot, event)
                or item.adapter_id != _adapter_id(bot)
                or item.actor_user_id != event_user_id(event)
                or item.scene_id != _event_scene_id(event)
            ):
                raise ProtocolExecutionError("协议确认码与当前会话不匹配")
            self._items.pop(normalized, None)

    async def clear(self) -> None:
        async with self._lock:
            self._items.clear()

    async def size(self) -> int:
        async with self._lock:
            self._prune(self._clock())
            return len(self._items)


def _adapter_id(bot: Any) -> str:
    from .onebot_facade import adapter_identity

    return adapter_identity(bot)


def _event_scene_id(event: Any) -> str:
    group_id = event_group_id(event)
    if group_id is not None:
        return group_id
    guild = getattr(event, "guild_id", None)
    channel = getattr(event, "channel_id", None)
    if guild is not None or channel is not None:
        return f"{guild or ''}/{channel or ''}"
    return event_user_id(event)


@dataclass(frozen=True)
class _RateClaim:
    key: str
    amount: int
    expires_at: float


class ProtocolRateLimiter:
    """Bounded, atomic in-process action limiter; claims are conservative."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._events: OrderedDict[str, deque[tuple[float, int]]] = OrderedDict()
        self._uncertain_until: OrderedDict[str, float] = OrderedDict()
        self._lock = asyncio.Lock()

    @staticmethod
    def _key(
        snapshot: ProtocolCapabilitySnapshot,
        prepared: _PreparedAction,
        parameters: Mapping[str, Any],
    ) -> str:
        target = str(
            parameters.get("target_id")
            or parameters.get("user_id")
            or parameters.get("group_id")
            or parameters.get("channel_id")
            or parameters.get("guild_id")
            or parameters.get("message_id")
            or _scene_id(snapshot)
        )
        date = datetime.now(timezone.utc).date().isoformat()
        value = {
            "action": prepared.action,
            "actor": snapshot.actor_user_id,
            "bot": snapshot.bot_id,
            "date": date,
            "scene": _scene_id(snapshot),
            "target": target,
            "tool": prepared.tool_name,
        }
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        return hashlib.sha256(encoded).hexdigest()

    async def claim(
        self,
        snapshot: ProtocolCapabilitySnapshot,
        prepared: _PreparedAction,
    ) -> _RateClaim:
        limit = prepared.rate_limit.get("limit")
        window = prepared.rate_limit.get("window_seconds")
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit <= 0
            or not isinstance(window, int)
            or isinstance(window, bool)
            or window <= 0
        ):
            raise ProtocolExecutionError("协议动作限额策略非法")
        amount = 1
        if prepared.tool_name == "qq__like_me":
            value = prepared.parameters.get("times", 1)
            if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 10:
                raise ProtocolExecutionError("点赞次数必须是 1 到 10 的整数")
            amount = value
        key = self._key(snapshot, prepared, prepared.parameters)
        now = self._clock()
        async with self._lock:
            for uncertain_key in [item_key for item_key, expires_at in self._uncertain_until.items() if expires_at <= now]:
                self._uncertain_until.pop(uncertain_key, None)
            if self._uncertain_until.get(key, 0) > now:
                raise ProtocolExecutionError("同一目标的上次副作用结果仍不确定，禁止自动重试")
            queue = self._events.get(key)
            if queue is None:
                while len(self._events) >= _MAX_RATE_KEYS:
                    self._events.popitem(last=False)
                queue = deque()
                self._events[key] = queue
            while queue and queue[0][0] + window <= now:
                queue.popleft()
            used = sum(item_amount for _, item_amount in queue)
            if used + amount > limit:
                raise ProtocolExecutionError(f"协议动作已达到限额（{limit}/{window}秒），未执行")
            queue.append((now, amount))
            self._events.move_to_end(key)
        return _RateClaim(key=key, amount=amount, expires_at=now + window)

    async def mark_result_unknown(self, claim: _RateClaim) -> None:
        async with self._lock:
            while len(self._uncertain_until) >= _MAX_RATE_KEYS:
                self._uncertain_until.popitem(last=False)
            self._uncertain_until[claim.key] = claim.expires_at
            self._uncertain_until.move_to_end(claim.key)

    async def clear(self) -> None:
        async with self._lock:
            self._events.clear()
            self._uncertain_until.clear()


def _capability_allowed(tool_name: str, capability: str) -> bool:
    from .builtin_tools import builtin_protocol_spec

    spec = builtin_protocol_spec(tool_name)
    if spec is None or spec.policy is None:
        return False
    profile = spec.policy.effective_v2
    if capability == "bot_read":
        return profile.bot_read
    if capability == "bot_send":
        return profile.bot_read and profile.bot_send
    if capability == "bot_manage":
        return profile.bot_read and profile.bot_send and profile.bot_manage
    return False


def _event_value(snapshot: ProtocolCapabilitySnapshot, source: str) -> str | None:
    return {
        "event.user_id": snapshot.actor_user_id,
        "event.group_id": snapshot.group_id,
        "event.message_id": snapshot.message_id,
        "event.reply_message_id": snapshot.reply_message_id,
        "bot.self_id": snapshot.bot_id,
    }.get(source)


def _profiled_parameters(
    action_id: str,
    action: str,
    argument_profile: str,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    if argument_profile == "strict":
        return dict(arguments)
    if argument_profile != "text_only_message":
        raise ProtocolExecutionError("协议参数策略非法")

    message = arguments.get("message")
    if not isinstance(message, str) or not message:
        raise ProtocolExecutionError("纯文本消息不能为空")
    parameters: dict[str, Any] = {"message": message}
    if action == "send_group_msg":
        parameters["group_id"] = arguments["group_id"]
    elif action == "send_private_msg":
        parameters["user_id"] = arguments["user_id"]
    elif action == "send_msg":
        message_type = arguments.get("message_type")
        target_field = "group_id" if message_type == "group" else "user_id"
        other_field = "user_id" if message_type == "group" else "group_id"
        if message_type not in {"private", "group"}:
            raise ProtocolExecutionError("send_msg 只允许 private 或 group 目标")
        if target_field not in arguments or other_field in arguments:
            raise ProtocolExecutionError(f"send_msg 的 {message_type} 目标字段不完整或相互冲突")
        parameters["message_type"] = message_type
        parameters[target_field] = arguments[target_field]
    elif action_id == "onebot_v12:send_message":
        detail_type = arguments.get("detail_type")
        if not isinstance(detail_type, str):
            raise ProtocolExecutionError("send_message 只允许 private、group 或 channel 目标")
        targets = {
            "private": ("user_id",),
            "group": ("group_id",),
            "channel": ("guild_id", "channel_id"),
        }
        required_targets = targets.get(detail_type)
        if required_targets is None:
            raise ProtocolExecutionError("send_message 只允许 private、group 或 channel 目标")
        supplied_targets = {name for name in ("user_id", "group_id", "guild_id", "channel_id") if name in arguments}
        if supplied_targets != set(required_targets):
            raise ProtocolExecutionError(f"send_message 的 {detail_type} 目标字段不完整或相互冲突")
        parameters["detail_type"] = detail_type
        for name in required_targets:
            parameters[name] = arguments[name]
    else:
        raise ProtocolExecutionError(f"动作 {action_id} 不能使用纯文本消息参数策略")
    if action_id.startswith(("onebot_v11:", "napcat_v11:")):
        parameters["auto_escape"] = True
    return parameters


def _action_protocol_available(snapshot: ProtocolCapabilitySnapshot, protocol: str, action: str) -> bool:
    if action not in snapshot.supported_actions:
        return False
    if protocol == "onebot_v12":
        return snapshot.protocol == "onebot_v12"
    if protocol == "onebot_v11":
        return snapshot.protocol == "onebot_v11"
    return protocol == "napcat_v11" and snapshot.protocol == "onebot_v11" and snapshot.implementation == "napcat"


def _prepare_action(
    binding: ProtocolRequestBinding,
    tool_name: str,
    arguments: Mapping[str, Any],
) -> _PreparedAction:
    snapshot = binding.snapshot
    if not protocol_tool_available(tool_name, snapshot=snapshot):
        raise ProtocolExecutionError("协议工具不在当前 Bot、权限或场景能力快照中")
    from .builtin_tools import builtin_protocol_spec

    spec = builtin_protocol_spec(tool_name)
    if spec is None:
        raise ProtocolExecutionError("协议工具不是包内可信 Builtin")
    _enforce_parameter_budget(arguments)
    error = validate_tool_arguments(dict(arguments), spec.parameters)
    if error:
        raise ProtocolExecutionError(f"协议参数错误：{error}")

    wrapper = protocol_registry.wrapper_by_name.get(tool_name)
    if wrapper is not None:
        return _prepare_wrapper(snapshot, wrapper, arguments)
    action = protocol_registry.action_for_tool(tool_name)
    if action is None:
        raise ProtocolExecutionError("协议工具 action identity 缺失")
    policy = protocol_registry.policy_for(action)
    if policy.exposure not in {"user", "superuser"}:
        raise ProtocolExecutionError(policy.denial_reason or "协议动作永久拒绝")
    if policy.permission == "superuser" and not snapshot.is_superuser:
        raise ProtocolExecutionError("协议动作仅允许 NoneBot SUPERUSER")
    parameters = _profiled_parameters(
        action.action_id,
        action.action,
        policy.argument_profile,
        arguments,
    )
    for field, source in policy.injected_params.items():
        value = _event_value(snapshot, source)
        if value is None:
            raise ProtocolExecutionError(f"当前场景缺少受信目标 {source}")
        parameters[field] = coerce_action_identifier(value, snapshot.protocol or action.protocol)
    _enforce_parameter_budget(parameters)
    return _PreparedAction(
        tool_name=tool_name,
        action=action.action,
        api_protocol=action.protocol,
        parameters=MappingProxyType(parameters),
        effect=policy.effect,
        capability=policy.capability,
        confirmation=policy.confirmation,
        rate_limit=policy.rate_limit,
        redact_fields=policy.redact_fields,
        policy_digest=policy.policy_digest,
        permission=policy.permission,
    )


def _prepare_wrapper(
    snapshot: ProtocolCapabilitySnapshot,
    policy: ProtocolWrapperPolicy,
    arguments: Mapping[str, Any],
) -> _PreparedAction:
    if policy.tool_name == "qq__like_me":
        action = "send_like"
        parameters: dict[str, Any] = {
            "user_id": coerce_action_identifier(snapshot.actor_user_id, snapshot.protocol or "onebot_v11"),
            "times": arguments.get("times", 1),
        }
        api_protocol = "onebot_v11"
    elif policy.tool_name == "qq__poke_current":
        if snapshot.scene == "group" and "group_poke" in snapshot.supported_actions:
            action = "group_poke"
        elif snapshot.scene == "private" and "friend_poke" in snapshot.supported_actions:
            action = "friend_poke"
        elif "send_poke" in snapshot.supported_actions:
            action = "send_poke"
        else:
            raise ProtocolExecutionError("当前 NapCat 不支持受限戳一戳动作")
        parameters = {
            "user_id": snapshot.actor_user_id,
            "target_id": snapshot.actor_user_id,
        }
        if snapshot.group_id is not None:
            parameters["group_id"] = snapshot.group_id
        api_protocol = "napcat_v11"
    elif policy.tool_name == "qq__react_current_message":
        if snapshot.message_id is None:
            raise ProtocolExecutionError("当前事件没有可回应的 message_id")
        action = "set_msg_emoji_like"
        parameters = {
            "message_id": coerce_action_identifier(snapshot.message_id, snapshot.protocol or "onebot_v11"),
            "emoji_id": arguments["emoji_id"],
            "set": True,
        }
        api_protocol = "napcat_v11"
    else:
        raise ProtocolExecutionError("未知协议安全封装")
    if not _action_protocol_available(snapshot, api_protocol, action):
        raise ProtocolExecutionError("安全封装底层动作不在能力快照中")
    _enforce_parameter_budget(parameters)
    return _PreparedAction(
        tool_name=policy.tool_name,
        action=action,
        api_protocol=api_protocol,
        parameters=MappingProxyType(parameters),
        effect=policy.effect,
        capability=policy.capability,
        confirmation=policy.confirmation,
        rate_limit=policy.rate_limit,
        redact_fields=policy.redact_fields,
        policy_digest=policy.policy_digest,
        permission=policy.permission,
    )


def _sanitize_string(value: str) -> str:
    if _BASE64_RE.fullmatch(value):
        return "[binary redacted]"
    lowered = value.lower()
    if value.startswith(("/", "~/", "file://")) or _WINDOWS_PATH_RE.match(value):
        return "[path redacted]"
    if any(marker in lowered for marker in ("authorization:", "bearer ", "cookie:")):
        return "[secret redacted]"
    if len(value) > 2_048:
        return value[:2_048] + "...[truncated]"
    return value


def _sanitize_value(
    value: Any,
    *,
    redact_fields: frozenset[str],
    depth: int = 0,
    nodes: list[int] | None = None,
) -> Any:
    counter = [0] if nodes is None else nodes
    counter[0] += 1
    if counter[0] > _MAX_SANITIZED_NODES or depth > _MAX_SANITIZED_DEPTH:
        return "[truncated]"
    if value is None or type(value) is bool:
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return max(-(1 << 63) + 1, min((1 << 63) - 1, value))
    if isinstance(value, float):
        return value if math.isfinite(value) else "[non-finite]"
    if isinstance(value, str):
        return _sanitize_string(value)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for index, (raw_key, item) in enumerate(value.items()):
            if index >= _MAX_SANITIZED_ITEMS:
                result["_truncated"] = True
                break
            key = str(raw_key)[:256]
            if _SENSITIVE_KEY_RE.search(key) or key.casefold() in redact_fields:
                result[key] = "[redacted]"
            else:
                result[key] = _sanitize_value(
                    item,
                    redact_fields=redact_fields,
                    depth=depth + 1,
                    nodes=counter,
                )
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        return [
            _sanitize_value(
                item,
                redact_fields=redact_fields,
                depth=depth + 1,
                nodes=counter,
            )
            for item in list(value)[:_MAX_SANITIZED_ITEMS]
        ]
    return _sanitize_string(str(value))


def sanitize_protocol_result(value: Any, redact_fields: tuple[str, ...]) -> Any:
    sanitized = _sanitize_value(
        value,
        redact_fields=frozenset(item.casefold() for item in redact_fields),
    )
    payload = json.dumps(sanitized, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if len(payload) <= _MAX_RESULT_CHARS:
        return sanitized
    return {
        "truncated": True,
        "preview": payload[:_MAX_RESULT_CHARS],
    }


def _is_network_uncertain(error: BaseException) -> bool:
    if isinstance(error, (TimeoutError, asyncio.TimeoutError, ConnectionError)):
        return True
    return type(error).__name__ in {
        "NetworkError",
        "WebSocketClosed",
        "ConnectionClosed",
        "ConnectionResetError",
    }


class ProtocolBroker:
    def __init__(
        self,
        *,
        pending: ProtocolPendingActionStore | None = None,
        limiter: ProtocolRateLimiter | None = None,
    ) -> None:
        self.pending = pending or ProtocolPendingActionStore()
        self.limiter = limiter or ProtocolRateLimiter()
        self._audits: deque[ProtocolAuditRecord] = deque(maxlen=_MAX_AUDITS)

    def audits(self) -> tuple[ProtocolAuditRecord, ...]:
        return tuple(self._audits)

    def _audit(
        self,
        snapshot: ProtocolCapabilitySnapshot,
        prepared: _PreparedAction,
        status: str,
        result: ToolResult,
    ) -> None:
        rendered = render_tool_result(result, max_chars=8_000)
        record = ProtocolAuditRecord(
            audit_id=uuid.uuid4().hex,
            created_at=time.time(),
            tool_name=prepared.tool_name,
            action=prepared.action,
            protocol=snapshot.protocol or "",
            implementation=snapshot.implementation,
            bot_id=snapshot.bot_id,
            actor_user_id=snapshot.actor_user_id,
            scene=snapshot.scene,
            generation=snapshot.runtime_generation,
            policy_digest=prepared.policy_digest,
            status=status,
            result_digest=_digest_text(rendered),
        )
        self._audits.append(record)
        logger.info(
            f"协议工具审计: tool={record.tool_name} action={record.action} status={record.status} generation={record.generation}"
        )

    async def invoke(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> ProtocolInvocation:
        binding = current_protocol_binding()
        if binding is None:
            raise ProtocolExecutionError("协议工具缺少请求绑定")
        prepared = _prepare_action(binding, tool_name, arguments)
        if not _capability_allowed(tool_name, prepared.capability):
            raise ProtocolExecutionError("协议 Broker Capability 消费门禁拒绝")
        direct_enabled = config_parser.get_config(
            "protocol_tools_low_risk_direct_enabled",
            True,
        )
        requires_confirmation = prepared.confirmation == "required" or (
            prepared.confirmation == "trusted_low_risk_direct" and not direct_enabled
        )
        if requires_confirmation:
            pending = await self.pending.create(binding, prepared)
            remaining = max(1, math.ceil(pending.expires_at - self.pending._clock()))
            result = ToolResult(
                text=(f"协议动作 {prepared.action} 尚未执行。请在 {remaining} 秒内单独发送：确认执行 {pending.nonce}"),
                metadata={"status": "waiting_confirmation"},
            )
            self._audit(binding.snapshot, prepared, "waiting_confirmation", result)
            return ProtocolInvocation(
                status=ProtocolInvocationStatus.WAITING_CONFIRMATION,
                result=result,
                tool_name=tool_name,
                action=prepared.action,
                confirmation_nonce=pending.nonce,
            )
        return await self._execute(binding, prepared)

    async def _execute(
        self,
        binding: ProtocolRequestBinding,
        prepared: _PreparedAction,
    ) -> ProtocolInvocation:
        snapshot = binding.snapshot
        # Every exposed protocol action is bounded.  Read-only calls keep their
        # claims on failure as a conservative abuse-control choice; only an
        # uncertain side effect additionally installs a no-retry tombstone.
        rate_claim = await self.limiter.claim(snapshot, prepared)
        timeout_value = config_parser.get_config("tool_timeout_seconds", 30)
        if not isinstance(timeout_value, (int, float)) or isinstance(timeout_value, bool) or timeout_value <= 0:
            raise ProtocolExecutionError("协议工具超时配置非法")
        inner_timeout = max(0.1, float(timeout_value) - 0.1)
        try:
            raw = await asyncio.wait_for(
                binding.bot.call_api(prepared.action, **dict(prepared.parameters)),
                timeout=inner_timeout,
            )
        except asyncio.CancelledError:
            if prepared.effect == "mutating":
                await self.limiter.mark_result_unknown(rate_claim)
                self._audit(
                    snapshot,
                    prepared,
                    "result_unknown",
                    ToolResult(
                        text="协议副作用在等待响应时被取消，结果不确定且不会自动重试。",
                        metadata={"status": "result_unknown"},
                    ),
                )
            raise
        except Exception as error:
            if prepared.effect == "mutating" and _is_network_uncertain(error):
                await self.limiter.mark_result_unknown(rate_claim)
                result = ToolResult(
                    text=(f"协议动作 {prepared.action} 的响应状态不确定；为避免重复副作用，不会自动重试。请人工核对实际状态。"),
                    metadata={
                        "action": prepared.action,
                        "status": "result_unknown",
                    },
                )
                self._audit(snapshot, prepared, "result_unknown", result)
                return ProtocolInvocation(
                    status=ProtocolInvocationStatus.RESULT_UNKNOWN,
                    result=result,
                    tool_name=prepared.tool_name,
                    action=prepared.action,
                )
            self._audit(
                snapshot,
                prepared,
                "failed",
                ToolResult(text="协议动作执行失败", metadata={"error_type": type(error).__name__}),
            )
            raise ProtocolExecutionError(f"协议动作 {prepared.action} 执行失败（{type(error).__name__}）") from None
        sanitized = sanitize_protocol_result(raw, prepared.redact_fields)
        result = ToolResult(
            text=f"协议动作 {prepared.action} 执行成功。",
            structured=sanitized,
            metadata={
                "action": prepared.action,
                "status": "completed",
            },
        )
        self._audit(snapshot, prepared, "completed", result)
        return ProtocolInvocation(
            status=ProtocolInvocationStatus.COMPLETED,
            result=result,
            tool_name=prepared.tool_name,
            action=prepared.action,
        )

    async def confirm(
        self,
        nonce: str,
        *,
        bot: Any,
        event: Any,
        generation: int,
        is_superuser: bool,
    ) -> ProtocolInvocation:
        pending = await self.pending.consume(
            nonce,
            bot=bot,
            event=event,
            generation=generation,
        )
        snapshot = await probe_protocol_capabilities(
            bot,
            event,
            generation=generation,
            is_superuser=is_superuser,
        )
        if not snapshot.enabled:
            raise ProtocolExecutionError("确认时协议能力探测失败，动作未执行")
        if (
            snapshot.bot_id != pending.bot_id
            or snapshot.adapter_id != pending.adapter_id
            or (snapshot.protocol or "") != pending.protocol
            or snapshot.implementation != pending.implementation
            or snapshot.implementation_version != pending.implementation_version
            or snapshot.supported_actions_digest != pending.supported_actions_digest
            or snapshot.actor_user_id != pending.actor_user_id
            or snapshot.scene != pending.scene
            or _scene_id(snapshot) != pending.scene_id
            or snapshot.runtime_generation != pending.generation
        ):
            raise ProtocolExecutionError("协议能力、Bot、会话或 generation 已变化，动作未执行")
        binding = ProtocolRequestBinding(snapshot=snapshot, bot=bot, event=event)
        stored_parameters = pending.parameters()
        prepared = _prepare_action(
            binding,
            pending.tool_name,
            _model_arguments_from_parameters(
                pending.tool_name,
                stored_parameters,
            ),
        )
        if (
            prepared.action != pending.action
            or prepared.policy_digest != pending.policy_digest
            or _canonical_parameters(prepared.parameters) != pending.parameters_json
            or not _capability_allowed(prepared.tool_name, prepared.capability)
        ):
            raise ProtocolExecutionError("协议动作或审核策略已变化，动作未执行")
        return await self._execute(binding, prepared)


protocol_pending_actions = ProtocolPendingActionStore()
protocol_rate_limiter = ProtocolRateLimiter()
protocol_broker = ProtocolBroker(
    pending=protocol_pending_actions,
    limiter=protocol_rate_limiter,
)


def _model_arguments_from_parameters(
    tool_name: str,
    parameters: Mapping[str, Any],
) -> dict[str, Any]:
    wrapper = protocol_registry.wrapper_by_name.get(tool_name)
    if wrapper is not None:
        if tool_name == "qq__like_me":
            return {"times": parameters.get("times", 1)}
        if tool_name == "qq__poke_current":
            return {}
        if tool_name == "qq__react_current_message":
            return {"emoji_id": parameters.get("emoji_id")}
        raise ProtocolExecutionError("未知协议安全封装")
    action = protocol_registry.action_for_tool(tool_name)
    if action is None:
        raise ProtocolExecutionError("协议工具 action identity 缺失")
    policy = protocol_registry.policy_for(action)
    ignored = set(policy.injected_params)
    if policy.argument_profile == "text_only_message":
        ignored.add("auto_escape")
    return {key: value for key, value in parameters.items() if key not in ignored}


__all__ = [
    "ProtocolAuditRecord",
    "ProtocolBroker",
    "ProtocolExecutionError",
    "ProtocolInvocation",
    "ProtocolInvocationStatus",
    "ProtocolPendingAction",
    "ProtocolPendingActionStore",
    "ProtocolRateLimiter",
    "protocol_broker",
    "protocol_pending_actions",
    "protocol_rate_limiter",
    "sanitize_protocol_result",
]

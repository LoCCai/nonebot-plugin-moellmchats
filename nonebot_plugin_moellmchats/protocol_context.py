from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import json
import re
from types import MappingProxyType
from typing import Any

from nonebot.log import logger

from .config import config_parser
from .onebot_facade import NormalizedOneBotEvent, adapter_identity, onebot_protocol
from .protocol_registry import protocol_registry

_PROBE_TIMEOUT_SECONDS = 3.0


def _digest_strings(values: frozenset[str]) -> str:
    encoded = json.dumps(sorted(values), separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _mapping(value: object) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        result = dump()
        if isinstance(result, Mapping):
            return result
    dictionary = getattr(value, "dict", None)
    if callable(dictionary):
        result = dictionary()
        if isinstance(result, Mapping):
            return result
    return MappingProxyType({})


async def _call_probe_api(bot: Any, action: str) -> Any:
    return await asyncio.wait_for(
        bot.call_api(action),
        timeout=_PROBE_TIMEOUT_SECONDS,
    )


@dataclass(frozen=True)
class ProtocolCapabilitySnapshot:
    enabled: bool
    reason: str
    protocol: str | None
    implementation: str
    implementation_version: str
    onebot_version: str
    supported_actions: frozenset[str]
    supported_actions_digest: str
    adapter_id: str
    bot_id: str
    actor_user_id: str
    is_superuser: bool
    scene: str
    group_id: str | None
    guild_id: str | None
    channel_id: str | None
    message_id: str | None
    reply_message_id: str | None
    runtime_generation: int
    plain_text: str
    cache_digest: str

    @classmethod
    def disabled(
        cls,
        *,
        bot: Any,
        event: Any,
        generation: int,
        is_superuser: bool,
        reason: str,
    ) -> ProtocolCapabilitySnapshot:
        try:
            normalized = NormalizedOneBotEvent.capture(bot, event)
        except ValueError:
            protocol = None
            bot_id = str(getattr(bot, "self_id", ""))
            actor = str(getattr(event, "user_id", ""))
            scene = "private"
            group_id = guild_id = channel_id = message_id = reply_message_id = None
            plain_text = ""
        else:
            protocol = normalized.protocol
            bot_id = normalized.bot_id
            actor = normalized.user_id
            scene = normalized.scene
            group_id = normalized.group_id
            guild_id = normalized.guild_id
            channel_id = normalized.channel_id
            message_id = normalized.message_id
            reply_message_id = normalized.reply_message_id
            plain_text = normalized.plain_text
        supported: frozenset[str] = frozenset()
        support_digest = _digest_strings(supported)
        cache_digest = _snapshot_cache_digest(
            protocol=protocol,
            implementation="",
            implementation_version="",
            support_digest=support_digest,
            adapter_id=adapter_identity(bot),
            bot_id=bot_id,
            actor_user_id=actor,
            is_superuser=is_superuser,
            scene=scene,
            group_id=group_id,
            guild_id=guild_id,
            channel_id=channel_id,
            message_id=message_id,
            reply_message_id=reply_message_id,
            generation=generation,
            enabled=False,
        )
        return cls(
            enabled=False,
            reason=reason,
            protocol=protocol,
            implementation="",
            implementation_version="",
            onebot_version="",
            supported_actions=supported,
            supported_actions_digest=support_digest,
            adapter_id=adapter_identity(bot),
            bot_id=bot_id,
            actor_user_id=actor,
            is_superuser=is_superuser,
            scene=scene,
            group_id=group_id,
            guild_id=guild_id,
            channel_id=channel_id,
            message_id=message_id,
            reply_message_id=reply_message_id,
            runtime_generation=generation,
            plain_text=plain_text,
            cache_digest=cache_digest,
        )


def _snapshot_cache_digest(
    *,
    protocol: str | None,
    implementation: str,
    implementation_version: str,
    support_digest: str,
    adapter_id: str,
    bot_id: str,
    actor_user_id: str,
    is_superuser: bool,
    scene: str,
    group_id: str | None,
    guild_id: str | None,
    channel_id: str | None,
    message_id: str | None,
    reply_message_id: str | None,
    generation: int,
    enabled: bool,
) -> str:
    value = {
        "actor_user_id": actor_user_id,
        "adapter_id": adapter_id,
        "bot_id": bot_id,
        "channel_id": channel_id,
        "enabled": enabled,
        "generation": generation,
        "group_id": group_id,
        "guild_id": guild_id,
        "implementation": implementation,
        "implementation_version": implementation_version,
        "is_superuser": is_superuser,
        "message_id": message_id,
        "protocol": protocol,
        "reply_message_id": reply_message_id,
        "scene": scene,
        "supported_actions_digest": support_digest,
    }
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


async def probe_protocol_capabilities(
    bot: Any,
    event: Any,
    *,
    generation: int,
    is_superuser: bool,
) -> ProtocolCapabilitySnapshot:
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0 or type(is_superuser) is not bool:
        raise ValueError("协议探测 generation/actor 非法")
    if not config_parser.get_config("protocol_tools_enabled", False):
        return ProtocolCapabilitySnapshot.disabled(
            bot=bot,
            event=event,
            generation=generation,
            is_superuser=is_superuser,
            reason="protocol_tools_enabled=false",
        )
    protocol = onebot_protocol(bot, event)
    if protocol is None:
        return ProtocolCapabilitySnapshot.disabled(
            bot=bot,
            event=event,
            generation=generation,
            is_superuser=is_superuser,
            reason="unsupported_adapter",
        )
    try:
        normalized = NormalizedOneBotEvent.capture(bot, event)
        if protocol == "onebot_v11":
            version_info = _mapping(await _call_probe_api(bot, "get_version_info"))
            if not version_info:
                raise ValueError("empty get_version_info")
            app_name = str(version_info.get("app_name") or "").strip()
            implementation_version = str(version_info.get("app_version") or version_info.get("version") or "").strip()
            onebot_version_value = str(version_info.get("protocol_version") or "11").strip()
            is_napcat = app_name == "NapCat.Onebot"
            implementation = "napcat" if is_napcat else (app_name or "generic")
            if is_napcat:
                supported = set(protocol_registry.napcat_actions)
                if not config_parser.get_config(
                    "protocol_tools_napcat_extensions_enabled",
                    True,
                ):
                    supported &= set(protocol_registry.standard_v11_actions)
            else:
                supported = set(protocol_registry.standard_v11_actions)
        else:
            raw_actions = await _call_probe_api(bot, "get_supported_actions")
            if not isinstance(raw_actions, (list, tuple, set, frozenset)) or not all(
                isinstance(item, str) and item for item in raw_actions
            ):
                raise ValueError("get_supported_actions returned invalid data")
            supported = set(raw_actions) & set(protocol_registry.v12_actions)
            implementation = str(getattr(bot, "impl", "") or "generic")
            implementation_version = str(getattr(bot, "version", "") or "")
            onebot_version_value = "12"
        supported_actions = frozenset(supported)
        support_digest = _digest_strings(supported_actions)
        adapter_id = adapter_identity(bot)
        cache_digest = _snapshot_cache_digest(
            protocol=protocol,
            implementation=implementation,
            implementation_version=implementation_version,
            support_digest=support_digest,
            adapter_id=adapter_id,
            bot_id=normalized.bot_id,
            actor_user_id=normalized.user_id,
            is_superuser=is_superuser,
            scene=normalized.scene,
            group_id=normalized.group_id,
            guild_id=normalized.guild_id,
            channel_id=normalized.channel_id,
            message_id=normalized.message_id,
            reply_message_id=normalized.reply_message_id,
            generation=generation,
            enabled=True,
        )
        return ProtocolCapabilitySnapshot(
            enabled=True,
            reason="ok",
            protocol=protocol,
            implementation=implementation,
            implementation_version=implementation_version,
            onebot_version=onebot_version_value,
            supported_actions=supported_actions,
            supported_actions_digest=support_digest,
            adapter_id=adapter_id,
            bot_id=normalized.bot_id,
            actor_user_id=normalized.user_id,
            is_superuser=is_superuser,
            scene=normalized.scene,
            group_id=normalized.group_id,
            guild_id=normalized.guild_id,
            channel_id=normalized.channel_id,
            message_id=normalized.message_id,
            reply_message_id=normalized.reply_message_id,
            runtime_generation=generation,
            plain_text=normalized.plain_text,
            cache_digest=cache_digest,
        )
    except Exception as error:
        logger.warning(f"OneBot 协议工具能力探测失败，当前请求仅关闭协议工具: {type(error).__name__}")
        return ProtocolCapabilitySnapshot.disabled(
            bot=bot,
            event=event,
            generation=generation,
            is_superuser=is_superuser,
            reason=f"probe_failed:{type(error).__name__}",
        )


_ACTIVE_PROTOCOL_SNAPSHOT: ContextVar[ProtocolCapabilitySnapshot | None] = ContextVar(
    "moellm_protocol_snapshot",
    default=None,
)


@dataclass(frozen=True)
class ProtocolRequestBinding:
    snapshot: ProtocolCapabilitySnapshot
    bot: Any
    event: Any


_ACTIVE_PROTOCOL_BINDING: ContextVar[ProtocolRequestBinding | None] = ContextVar(
    "moellm_protocol_binding",
    default=None,
)


def current_protocol_snapshot() -> ProtocolCapabilitySnapshot | None:
    return _ACTIVE_PROTOCOL_SNAPSHOT.get()


def current_protocol_binding() -> ProtocolRequestBinding | None:
    return _ACTIVE_PROTOCOL_BINDING.get()


def current_protocol_cache_digest() -> str:
    snapshot = current_protocol_snapshot()
    if snapshot is None:
        return "0" * 64
    return snapshot.cache_digest


@asynccontextmanager
async def protocol_request_scope(
    bot: Any,
    event: Any,
    *,
    generation: int,
    is_superuser: bool,
) -> AsyncIterator[ProtocolCapabilitySnapshot]:
    snapshot = await probe_protocol_capabilities(
        bot,
        event,
        generation=generation,
        is_superuser=is_superuser,
    )
    token = _ACTIVE_PROTOCOL_SNAPSHOT.set(snapshot)
    binding_token = _ACTIVE_PROTOCOL_BINDING.set(ProtocolRequestBinding(snapshot=snapshot, bot=bot, event=event))
    try:
        yield snapshot
    finally:
        _ACTIVE_PROTOCOL_BINDING.reset(binding_token)
        _ACTIVE_PROTOCOL_SNAPSHOT.reset(token)


def _action_available(
    snapshot: ProtocolCapabilitySnapshot,
    protocol: str,
    action: str,
) -> bool:
    if not snapshot.enabled or action not in snapshot.supported_actions:
        return False
    if protocol == "onebot_v12":
        return snapshot.protocol == "onebot_v12"
    if protocol == "onebot_v11":
        return snapshot.protocol == "onebot_v11"
    return (
        protocol == "napcat_v11"
        and snapshot.protocol == "onebot_v11"
        and snapshot.implementation == "napcat"
        and config_parser.get_config("protocol_tools_napcat_extensions_enabled", True)
    )


def protocol_tool_available(
    tool_name: str,
    *,
    snapshot: ProtocolCapabilitySnapshot | None = None,
    is_superuser: bool | None = None,
) -> bool:
    selected = current_protocol_snapshot() if snapshot is None else snapshot
    if selected is None or not selected.enabled:
        return False
    actor_superuser = selected.is_superuser if is_superuser is None else is_superuser
    wrapper = protocol_registry.wrapper_by_name.get(tool_name)
    if wrapper is not None:
        if selected.scene not in wrapper.allowed_scenes:
            return False
        if wrapper.scope == "current_message" and selected.message_id is None:
            return False
        return any(_action_available(selected, protocol, action) for protocol in wrapper.protocols for action in wrapper.actions)
    action = protocol_registry.action_for_tool(tool_name)
    if action is None:
        return False
    policy = protocol_registry.policy_for(action)
    if policy.exposure not in {"user", "superuser"}:
        return False
    if policy.permission == "superuser" and not actor_superuser:
        return False
    if selected.scene not in policy.allowed_scenes:
        return False
    for source in policy.injected_params.values():
        if source == "event.group_id" and selected.group_id is None:
            return False
        if source == "event.message_id" and selected.message_id is None:
            return False
        if source == "event.reply_message_id" and selected.reply_message_id is None:
            return False
    return _action_available(selected, action.protocol, action.action)


def available_protocol_tool_names(
    *,
    snapshot: ProtocolCapabilitySnapshot | None = None,
    is_superuser: bool | None = None,
) -> frozenset[str]:
    selected = current_protocol_snapshot() if snapshot is None else snapshot
    if selected is None or not selected.enabled:
        return frozenset()
    names = {
        action.tool_name
        for action in protocol_registry.public_runtime_actions()
        if protocol_tool_available(
            action.tool_name,
            snapshot=selected,
            is_superuser=is_superuser,
        )
    }
    names.update(
        wrapper.tool_name
        for wrapper in protocol_registry.wrappers
        if protocol_tool_available(
            wrapper.tool_name,
            snapshot=selected,
            is_superuser=is_superuser,
        )
    )
    return frozenset(names)


_TRIGGER_SPLIT_RE = re.compile(r"(?:\s+或\s+|或|/|、|，|,|\||；|;)")


def _literal_trigger_tokens(value: str) -> tuple[str, ...]:
    cleaned = value.replace("`", "").strip()
    tokens: list[str] = []
    for part in _TRIGGER_SPLIT_RE.split(cleaned):
        part = re.sub(r"<[^>]+>|\[[^]]+]", "", part).strip()
        part = re.sub(r"^(?:命令|消息|关键词|直接消息)[:：]?", "", part).strip()
        if len(part) >= 2 and len(part) <= 40:
            tokens.append(part)
    return tuple(tokens)


def business_conflicting_protocol_tools(
    plugin_info: Mapping[str, Mapping[str, Any]],
    *,
    snapshot: ProtocolCapabilitySnapshot | None = None,
) -> frozenset[str]:
    """Return protocol tools suppressed by normalized business-menu triggers."""

    selected = current_protocol_snapshot() if snapshot is None else snapshot
    if (
        selected is None
        or not selected.enabled
        or not selected.plain_text.strip()
        or not config_parser.get_config("protocol_tools_business_first", True)
    ):
        return frozenset()
    from .tool_discovery import discovery_features

    plain = selected.plain_text.casefold()
    matched_tokens: set[str] = set()
    for info in plugin_info.values():
        for feature in discovery_features(info):
            if feature.get("invocable") is not True or feature.get("hidden") is True:
                continue
            triggers = feature.get("triggers")
            if not isinstance(triggers, (list, tuple)):
                continue
            for trigger in triggers:
                if not isinstance(trigger, Mapping) or trigger.get("type") not in {"command", "direct", "message", "regex"}:
                    continue
                value = trigger.get("value")
                if not isinstance(value, str):
                    continue
                for token in _literal_trigger_tokens(value):
                    if token.casefold() in plain:
                        matched_tokens.add(token.casefold())
    if not matched_tokens:
        return frozenset()
    conflicts: set[str] = set()
    for wrapper in protocol_registry.wrappers:
        if any(
            token in keyword.casefold() or keyword.casefold() in token
            for token in matched_tokens
            for keyword in wrapper.intent_keywords
        ):
            conflicts.add(wrapper.tool_name)
            for action in protocol_registry.public_runtime_actions():
                if action.action in wrapper.actions:
                    conflicts.add(action.tool_name)
    for action in protocol_registry.public_runtime_actions():
        policy = protocol_registry.policy_for(action)
        if any(
            token in keyword.casefold() or keyword.casefold() in token
            for token in matched_tokens
            for keyword in policy.intent_keywords
        ):
            conflicts.add(action.tool_name)
    return frozenset(conflicts)


__all__ = [
    "ProtocolCapabilitySnapshot",
    "ProtocolRequestBinding",
    "available_protocol_tool_names",
    "business_conflicting_protocol_tools",
    "current_protocol_binding",
    "current_protocol_cache_digest",
    "current_protocol_snapshot",
    "probe_protocol_capabilities",
    "protocol_request_scope",
    "protocol_tool_available",
]

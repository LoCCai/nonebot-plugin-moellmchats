from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from .protocol_registry import (
    ProtocolActionPolicy,
    ProtocolWrapperPolicy,
    protocol_registry,
)
from .tool_contracts import (
    ToolCapabilityV2,
    ToolConfirmationMode,
    ToolEffect,
    ToolPolicy,
    ToolSpec,
)


async def execute_web_search(
    query: str,
    *,
    tool_snapshot: object | None = None,
    is_superuser: bool = False,
) -> object:
    """Run the existing search adapter without changing its result semantics."""

    if type(is_superuser) is not bool:
        raise TypeError("web_search is_superuser 必须是布尔值")

    # Keep the import lazy: search.py retains a bootstrap fallback to the
    # ToolManager mirror, while runtime execution passes a transaction snapshot.
    from .search import Search

    return await Search(
        query,
        tool_snapshot=tool_snapshot,
        is_superuser=is_superuser,
    ).get_search()


WEB_SEARCH_TOOL_SPEC = ToolSpec(
    name="web_search",
    description="进行互联网搜索以获取最新信息或解答未知问题。",
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索关键词或短语",
            }
        },
        "required": ["query"],
    },
    handler=execute_web_search,
)


def _protocol_capability_policy(capability: str) -> ToolPolicy:
    if capability == "bot_read":
        profile = ToolCapabilityV2(bot_read=True)
    elif capability == "bot_send":
        profile = ToolCapabilityV2(bot_read=True, bot_send=True)
    elif capability == "bot_manage":
        profile = ToolCapabilityV2(
            bot_read=True,
            bot_send=True,
            bot_manage=True,
        )
    else:
        raise ValueError(f"未知协议 capability: {capability}")
    return ToolPolicy.configured(profile)


def _protocol_handler(tool_name: str) -> Callable[..., Awaitable[Any]]:
    async def execute(**arguments: Any) -> Any:
        from .protocol_broker import protocol_broker

        return await protocol_broker.invoke(tool_name, arguments)

    execute.__name__ = f"execute_{tool_name}"
    return execute


def _confirmation_mode(value: str) -> ToolConfirmationMode:
    return {
        "default": ToolConfirmationMode.DEFAULT,
        "required": ToolConfirmationMode.REQUIRED,
        "trusted_low_risk_direct": (ToolConfirmationMode.TRUSTED_LOW_RISK_DIRECT),
    }[value]


def _action_spec(policy: ProtocolActionPolicy) -> ToolSpec:
    action = protocol_registry.by_id[policy.action_id]
    return ToolSpec(
        name=action.tool_name,
        description=(
            f"[{action.protocol}] {action.summary}。"
            f"权限={policy.permission}，范围={policy.scope}，风险={policy.risk}。"
            "只能调用此固定 API；未声明参数会被拒绝。"
        ),
        parameters=protocol_registry.model_schema(action, policy),
        handler=_protocol_handler(action.tool_name),
        effect=(ToolEffect.MUTATING if policy.effect == "mutating" else ToolEffect.READ_ONLY),
        permission=policy.permission,
        timeout_seconds=30,
        result_limit=24_000,
        policy=_protocol_capability_policy(policy.capability),
        confirmation_mode=_confirmation_mode(policy.confirmation),
    )


def _wrapper_schema(policy: ProtocolWrapperPolicy) -> dict[str, Any]:
    if policy.tool_name == "qq__like_me":
        return {
            "type": "object",
            "properties": {
                "times": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "default": 1,
                    "description": "给当前发起用户点赞的次数，1 到 10",
                }
            },
            "required": [],
            "additionalProperties": False,
        }
    if policy.tool_name == "qq__poke_current":
        return {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        }
    if policy.tool_name == "qq__react_current_message":
        return {
            "type": "object",
            "properties": {
                "emoji_id": {
                    "anyOf": [
                        {"type": "integer"},
                        {"type": "string"},
                    ],
                    "description": "NapCat 表情 ID",
                }
            },
            "required": ["emoji_id"],
            "additionalProperties": False,
        }
    raise ValueError(f"未知协议 wrapper: {policy.tool_name}")


def _wrapper_spec(policy: ProtocolWrapperPolicy) -> ToolSpec:
    descriptions = {
        "qq__like_me": "给当前发起用户本人点赞；目标由事件注入，不能改为他人。",
        "qq__poke_current": "在当前会话戳一戳当前发起用户；目标和群由事件注入。",
        "qq__react_current_message": "给当前触发消息添加表情回应；消息 ID 由事件注入。",
    }
    return ToolSpec(
        name=policy.tool_name,
        description=descriptions[policy.tool_name],
        parameters=_wrapper_schema(policy),
        handler=_protocol_handler(policy.tool_name),
        effect=ToolEffect.MUTATING,
        permission="user",
        timeout_seconds=30,
        result_limit=8_000,
        policy=_protocol_capability_policy(policy.capability),
        confirmation_mode=ToolConfirmationMode.TRUSTED_LOW_RISK_DIRECT,
    )


_PROTOCOL_TOOL_SPECS = tuple(
    _action_spec(protocol_registry.policy_for(action)) for action in protocol_registry.public_runtime_actions()
) + tuple(_wrapper_spec(policy) for policy in protocol_registry.wrappers)
_PROTOCOL_TOOL_SPEC_BY_NAME = {spec.name: spec for spec in _PROTOCOL_TOOL_SPECS}

_BUILTIN_TOOL_SPECS = (WEB_SEARCH_TOOL_SPEC, *_PROTOCOL_TOOL_SPECS)


def builtin_tool_specs() -> tuple[ToolSpec, ...]:
    """Return the immutable code-defined builtin registry for one candidate."""

    return _BUILTIN_TOOL_SPECS


def builtin_protocol_specs() -> tuple[ToolSpec, ...]:
    return _PROTOCOL_TOOL_SPECS


def builtin_protocol_spec(name: str) -> ToolSpec | None:
    return _PROTOCOL_TOOL_SPEC_BY_NAME.get(name)


def is_trusted_protocol_tool_spec(spec: ToolSpec) -> bool:
    return _PROTOCOL_TOOL_SPEC_BY_NAME.get(spec.name) is spec

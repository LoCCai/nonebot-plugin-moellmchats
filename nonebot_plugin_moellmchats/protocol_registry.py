from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
from importlib.resources import files
import json
import re
from types import MappingProxyType
from typing import Any

_EXPECTED_COUNTS = MappingProxyType({"onebot_v11": 38, "onebot_v12": 31, "napcat_v11": 175})
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_INJECTION_SOURCES = frozenset(
    {
        "bot.self_id",
        "event.group_id",
        "event.message_id",
        "event.reply_message_id",
        "event.user_id",
    }
)
_POLICY_FIELDS = frozenset(
    {
        "allowed_scenes",
        "capability",
        "confirmation",
        "denial_reason",
        "effect",
        "exposure",
        "id",
        "injected_params",
        "intent_keywords",
        "permission",
        "rate_limit",
        "redact_fields",
        "retry",
        "reviewed",
        "risk",
        "scope",
    }
)
_POLICY_OPTIONAL_FIELDS = frozenset({"argument_profile"})
_ARGUMENT_PROFILES = frozenset({"strict", "text_only_message"})
_TEXT_MESSAGE_ACTIONS = frozenset(
    {
        "napcat_v11:send_group_msg",
        "napcat_v11:send_msg",
        "napcat_v11:send_private_msg",
        "onebot_v11:send_group_msg",
        "onebot_v11:send_msg",
        "onebot_v11:send_private_msg",
        "onebot_v12:send_message",
    }
)


class ProtocolRegistryError(RuntimeError):
    """Packaged protocol inventory or policy failed a fail-closed check."""


def _canonical_digest(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _mutable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _mutable(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_mutable(item) for item in value]
    return value


def _load_resource(name: str) -> dict[str, Any]:
    resource = files("nonebot_plugin_moellmchats.protocol_resources").joinpath(name)
    try:
        value = json.loads(resource.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProtocolRegistryError(f"协议资源 {name} 无法加载") from error
    if not isinstance(value, dict):
        raise ProtocolRegistryError(f"协议资源 {name} 顶层必须是对象")
    return value


@dataclass(frozen=True)
class ProtocolAction:
    action_id: str
    protocol: str
    action: str
    tool_name: str
    summary: str
    request_schema: Mapping[str, Any]
    deprecated: bool
    source_path: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ProtocolAction:
        required = {
            "id",
            "protocol",
            "action",
            "tool_name",
            "summary",
            "request_schema",
            "deprecated",
            "source_path",
        }
        if set(value) != required:
            raise ProtocolRegistryError("协议动作字段集合不完整")
        protocol = value["protocol"]
        action = value["action"]
        action_id = value["id"]
        tool_name = value["tool_name"]
        schema = value["request_schema"]
        if (
            not isinstance(protocol, str)
            or protocol not in _EXPECTED_COUNTS
            or not isinstance(action, str)
            or not action
            or action_id != f"{protocol}:{action}"
            or not isinstance(tool_name, str)
            or not _TOOL_NAME_RE.fullmatch(tool_name)
            or not isinstance(value["summary"], str)
            or not value["summary"].strip()
            or not isinstance(schema, Mapping)
            or schema.get("type") != "object"
            or schema.get("additionalProperties") is not False
            or type(value["deprecated"]) is not bool
            or not isinstance(value["source_path"], str)
        ):
            raise ProtocolRegistryError(f"协议动作非法: {action_id!r}")
        return cls(
            action_id=action_id,
            protocol=protocol,
            action=action,
            tool_name=tool_name,
            summary=value["summary"],
            request_schema=_freeze(schema),
            deprecated=value["deprecated"],
            source_path=value["source_path"],
        )


@dataclass(frozen=True)
class ProtocolActionPolicy:
    action_id: str
    exposure: str
    denial_reason: str
    effect: str
    risk: str
    scope: str
    permission: str
    confirmation: str
    capability: str
    injected_params: Mapping[str, str]
    allowed_scenes: tuple[str, ...]
    rate_limit: Mapping[str, Any]
    redact_fields: tuple[str, ...]
    retry: str
    intent_keywords: tuple[str, ...]
    argument_profile: str
    policy_digest: str

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        allow_trusted_direct: bool = False,
    ) -> ProtocolActionPolicy:
        if value.get("reviewed") is not True:
            raise ProtocolRegistryError(f"协议动作未经审核: {value.get('id')}")
        action_id = value.get("id")
        injection = value.get("injected_params")
        scenes = value.get("allowed_scenes")
        rate_limit = value.get("rate_limit")
        redact_fields = value.get("redact_fields")
        keywords = value.get("intent_keywords")
        exposure = value.get("exposure")
        denial_reason = value.get("denial_reason")
        argument_profile = value.get("argument_profile", "strict")
        if (
            not _POLICY_FIELDS <= set(value)
            or not set(value) <= _POLICY_FIELDS | _POLICY_OPTIONAL_FIELDS
            or not isinstance(action_id, str)
            or exposure not in {"user", "superuser", "denied", "internal"}
            or value.get("effect") not in {"read_only", "mutating"}
            or value.get("risk") not in {"low", "medium", "high", "forbidden"}
            or value.get("permission") not in {"user", "superuser"}
            or value.get("confirmation") not in {"default", "required", "trusted_low_risk_direct"}
            or value.get("capability") not in {"bot_read", "bot_send", "bot_manage"}
            or not isinstance(injection, Mapping)
            or not all(isinstance(key, str) and isinstance(item, str) for key, item in injection.items())
            or not set(injection.values()) <= _INJECTION_SOURCES
            or not isinstance(scenes, list)
            or not scenes
            or not set(scenes) <= {"group", "private", "channel"}
            or not isinstance(rate_limit, Mapping)
            or not isinstance(rate_limit.get("limit"), int)
            or isinstance(rate_limit.get("limit"), bool)
            or rate_limit["limit"] <= 0
            or not isinstance(rate_limit.get("window_seconds"), int)
            or isinstance(rate_limit.get("window_seconds"), bool)
            or rate_limit["window_seconds"] <= 0
            or not isinstance(rate_limit.get("key_fields"), list)
            or not rate_limit["key_fields"]
            or not all(isinstance(item, str) and item for item in rate_limit["key_fields"])
            or not isinstance(redact_fields, list)
            or not all(isinstance(item, str) for item in redact_fields)
            or value.get("retry") not in {"never", "safe_once"}
            or not isinstance(keywords, list)
            or not all(isinstance(item, str) and item for item in keywords)
            or not isinstance(denial_reason, str)
            or argument_profile not in _ARGUMENT_PROFILES
            or (exposure in {"denied", "internal"} and (not denial_reason.strip() or value.get("risk") != "forbidden"))
            or (exposure in {"user", "superuser"} and (denial_reason or value.get("risk") == "forbidden"))
            or (
                value.get("effect") == "mutating"
                and exposure in {"user", "superuser"}
                and value.get("confirmation") != "required"
                and not (allow_trusted_direct and value.get("confirmation") == "trusted_low_risk_direct")
            )
        ):
            raise ProtocolRegistryError(f"协议策略非法: {action_id!r}")
        return cls(
            action_id=action_id,
            exposure=value["exposure"],
            denial_reason=str(value.get("denial_reason") or ""),
            effect=value["effect"],
            risk=value["risk"],
            scope=str(value.get("scope") or "global"),
            permission=value["permission"],
            confirmation=value["confirmation"],
            capability=value["capability"],
            injected_params=MappingProxyType(dict(injection)),
            allowed_scenes=tuple(scenes),
            rate_limit=_freeze(rate_limit),
            redact_fields=tuple(sorted(set(redact_fields))),
            retry=value["retry"],
            intent_keywords=tuple(keywords),
            argument_profile=argument_profile,
            policy_digest=_canonical_digest(value),
        )


@dataclass(frozen=True)
class ProtocolWrapperPolicy:
    tool_name: str
    protocols: tuple[str, ...]
    actions: tuple[str, ...]
    effect: str
    risk: str
    scope: str
    permission: str
    confirmation: str
    capability: str
    injected_params: Mapping[str, str]
    allowed_scenes: tuple[str, ...]
    rate_limit: Mapping[str, Any]
    redact_fields: tuple[str, ...]
    retry: str
    intent_keywords: tuple[str, ...]
    policy_digest: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ProtocolWrapperPolicy:
        if value.get("reviewed") is not True:
            raise ProtocolRegistryError("协议安全封装未经审核")
        name = value.get("tool_name")
        protocols = value.get("protocols")
        actions = value.get("actions")
        if (
            not isinstance(name, str)
            or not _TOOL_NAME_RE.fullmatch(name)
            or not isinstance(protocols, list)
            or not protocols
            or not set(protocols) <= set(_EXPECTED_COUNTS)
            or not isinstance(actions, list)
            or not actions
            or not all(isinstance(item, str) and item for item in actions)
            or value.get("effect") != "mutating"
            or value.get("risk") != "low"
            or value.get("permission") != "user"
            or value.get("confirmation") != "trusted_low_risk_direct"
            or value.get("capability") != "bot_send"
        ):
            raise ProtocolRegistryError(f"协议安全封装非法: {name!r}")
        base_policy = ProtocolActionPolicy.from_mapping(
            {
                "id": f"wrapper:{name}",
                "reviewed": True,
                "exposure": "user",
                "denial_reason": "",
                **{
                    key: _mutable(item)
                    for key, item in value.items()
                    if key not in {"tool_name", "protocols", "actions", "reviewed"}
                },
            },
            allow_trusted_direct=True,
        )
        return cls(
            tool_name=name,
            protocols=tuple(protocols),
            actions=tuple(actions),
            effect=base_policy.effect,
            risk=base_policy.risk,
            scope=base_policy.scope,
            permission=base_policy.permission,
            confirmation=base_policy.confirmation,
            capability=base_policy.capability,
            injected_params=base_policy.injected_params,
            allowed_scenes=base_policy.allowed_scenes,
            rate_limit=base_policy.rate_limit,
            redact_fields=base_policy.redact_fields,
            retry=base_policy.retry,
            intent_keywords=base_policy.intent_keywords,
            policy_digest=_canonical_digest(value),
        )


class ProtocolRegistry:
    """Immutable package-owned inventory and reviewed policy index."""

    def __init__(self) -> None:
        source_document = _load_resource("sources.json")
        inventory = _load_resource("actions.json")
        policy_document = _load_resource("policies.json")
        actions_raw = inventory.get("actions")
        policies_raw = policy_document.get("policies")
        wrappers_raw = policy_document.get("wrappers")
        if (
            set(source_document) != {"schema_version", "sources"}
            or source_document.get("schema_version") != 1
            or not isinstance(source_document.get("sources"), dict)
            or set(inventory)
            != {
                "actions",
                "actions_sha256",
                "counts",
                "schema_version",
                "source_lock_sha256",
            }
            or inventory.get("schema_version") != 1
            or inventory.get("source_lock_sha256") != _canonical_digest(source_document)
            or set(policy_document)
            != {
                "action_inventory_sha256",
                "policies",
                "policies_sha256",
                "review_basis",
                "reviewed_at",
                "schema_version",
                "wrappers",
                "wrappers_sha256",
            }
            or policy_document.get("schema_version") != 1
            or not isinstance(actions_raw, list)
            or not isinstance(policies_raw, list)
            or not isinstance(wrappers_raw, list)
            or inventory.get("actions_sha256") != _canonical_digest(actions_raw)
            or policy_document.get("action_inventory_sha256") != inventory.get("actions_sha256")
            or policy_document.get("policies_sha256") != _canonical_digest(policies_raw)
            or policy_document.get("wrappers_sha256") != _canonical_digest(wrappers_raw)
            or not isinstance(policy_document.get("reviewed_at"), str)
            or not policy_document["reviewed_at"].strip()
            or not isinstance(policy_document.get("review_basis"), str)
            or not policy_document["review_basis"].strip()
        ):
            raise ProtocolRegistryError("协议清单摘要或 schema version 不一致")
        actions = tuple(ProtocolAction.from_mapping(item) for item in actions_raw)
        policies = tuple(ProtocolActionPolicy.from_mapping(item) for item in policies_raw)
        wrappers = tuple(ProtocolWrapperPolicy.from_mapping(item) for item in wrappers_raw)
        if any(item.confirmation == "trusted_low_risk_direct" for item in policies):
            raise ProtocolRegistryError("只有包内安全封装可声明 trusted_low_risk_direct")
        action_map = {item.action_id: item for item in actions}
        policy_map = {item.action_id: item for item in policies}
        if len(action_map) != len(actions) or set(action_map) != set(policy_map):
            raise ProtocolRegistryError("协议动作和审核策略集合不一致")
        for action in actions:
            policy = policy_map[action.action_id]
            properties = action.request_schema.get("properties", {})
            if not isinstance(properties, Mapping) or not set(policy.injected_params) <= set(properties):
                raise ProtocolRegistryError(f"协议注入参数不在请求 Schema 中: {action.action_id}")
            if (policy.argument_profile == "text_only_message") != (action.action_id in _TEXT_MESSAGE_ACTIONS):
                raise ProtocolRegistryError(f"协议参数策略与动作不匹配: {action.action_id}")
        counts = {protocol: sum(item.protocol == protocol for item in actions) for protocol in _EXPECTED_COUNTS}
        if counts != dict(_EXPECTED_COUNTS) or inventory.get("counts") != dict(_EXPECTED_COUNTS):
            raise ProtocolRegistryError("协议动作数量门禁失败")
        tools = [item.tool_name for item in actions]
        wrapper_names = [item.tool_name for item in wrappers]
        if len(tools) != len(set(tools)) or set(tools) & set(wrapper_names) or len(wrapper_names) != len(set(wrapper_names)):
            raise ProtocolRegistryError("协议工具名发生碰撞")
        action_names_by_protocol = {
            protocol: {action.action for action in actions if action.protocol == protocol} for protocol in _EXPECTED_COUNTS
        }
        for wrapper in wrappers:
            for protocol in wrapper.protocols:
                missing = set(wrapper.actions) - action_names_by_protocol[protocol]
                if missing:
                    raise ProtocolRegistryError(f"协议安全封装引用不存在动作: {wrapper.tool_name} {protocol} {sorted(missing)}")
        self.actions = actions
        self.policies = policies
        self.wrappers = wrappers
        self.by_id = MappingProxyType(action_map)
        self.by_tool_name = MappingProxyType({item.tool_name: item for item in actions})
        self.policy_by_id = MappingProxyType(policy_map)
        self.wrapper_by_name = MappingProxyType({item.tool_name: item for item in wrappers})
        self.actions_by_protocol = MappingProxyType(
            {protocol: tuple(item for item in actions if item.protocol == protocol) for protocol in _EXPECTED_COUNTS}
        )
        self.standard_v11_actions = frozenset(item.action for item in self.actions_by_protocol["onebot_v11"])
        self.napcat_actions = frozenset(item.action for item in self.actions_by_protocol["napcat_v11"])
        self.v12_actions = frozenset(item.action for item in self.actions_by_protocol["onebot_v12"])
        self.inventory_digest = str(inventory["actions_sha256"])
        self.policy_digest = str(policy_document["policies_sha256"])

    def policy_for(self, action: ProtocolAction) -> ProtocolActionPolicy:
        try:
            return self.policy_by_id[action.action_id]
        except KeyError:
            raise ProtocolRegistryError(f"协议动作缺少策略: {action.action_id}") from None

    def action_for_tool(self, tool_name: str) -> ProtocolAction | None:
        return self.by_tool_name.get(tool_name)

    def public_runtime_actions(self) -> tuple[ProtocolAction, ...]:
        """Return specs without duplicate NapCat copies of v11 standard actions."""

        return tuple(
            action
            for action in self.actions
            if self.policy_for(action).exposure in {"user", "superuser"}
            and not (action.protocol == "napcat_v11" and action.action in self.standard_v11_actions)
        )

    @staticmethod
    def model_schema(
        action: ProtocolAction,
        policy: ProtocolActionPolicy,
    ) -> dict[str, Any]:
        if policy.argument_profile == "text_only_message":
            return ProtocolRegistry._text_message_schema(action)
        schema = _mutable(action.request_schema)
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        for name in policy.injected_params:
            properties.pop(name, None)
        schema["properties"] = properties
        schema["required"] = [name for name in required if name in properties]
        schema["additionalProperties"] = False
        return schema

    @staticmethod
    def _text_message_schema(action: ProtocolAction) -> dict[str, Any]:
        properties = action.request_schema.get("properties", {})
        if not isinstance(properties, Mapping):
            raise ProtocolRegistryError(f"纯文本消息动作缺少请求字段: {action.action_id}")

        if action.action == "send_group_msg":
            target_fields = ("group_id",)
            required = ["group_id", "message"]
        elif action.action == "send_private_msg":
            target_fields = ("user_id",)
            required = ["user_id", "message"]
        elif action.action == "send_msg":
            target_fields = ("message_type", "group_id", "user_id")
            required = ["message_type", "message"]
        elif action.action_id == "onebot_v12:send_message":
            target_fields = (
                "detail_type",
                "user_id",
                "group_id",
                "guild_id",
                "channel_id",
            )
            required = ["detail_type", "message"]
        else:
            raise ProtocolRegistryError(f"未知纯文本消息动作: {action.action_id}")

        safe_properties: dict[str, Any] = {}
        for name in target_fields:
            source = properties.get(name)
            if not isinstance(source, Mapping):
                raise ProtocolRegistryError(f"纯文本消息动作缺少目标字段 {name}: {action.action_id}")
            safe_property = _mutable(source)
            if safe_property.get("type") == "string":
                safe_property["minLength"] = max(
                    1,
                    int(safe_property.get("minLength", 0)),
                )
            for union_name in ("anyOf", "oneOf"):
                branches = safe_property.get(union_name)
                if not isinstance(branches, list):
                    continue
                for branch in branches:
                    if isinstance(branch, dict) and branch.get("type") == "string":
                        branch["minLength"] = max(
                            1,
                            int(branch.get("minLength", 0)),
                        )
            safe_properties[name] = safe_property
        if "message_type" in safe_properties:
            safe_properties["message_type"] = {
                "type": "string",
                "enum": ["private", "group"],
                "description": "发送目标类型，只允许 private 或 group",
            }
        if "detail_type" in safe_properties:
            safe_properties["detail_type"] = {
                "type": "string",
                "enum": ["private", "group", "channel"],
                "description": "发送目标类型，只允许 private、group 或 channel",
            }
        safe_properties["message"] = {
            "type": "string",
            "minLength": 1,
            "maxLength": 4000,
            "description": "纯文本消息；禁止 CQ 码、消息段、文件、URL 和 Base64 载荷",
        }
        return {
            "type": "object",
            "properties": safe_properties,
            "required": required,
            "additionalProperties": False,
        }


protocol_registry = ProtocolRegistry()


__all__ = [
    "ProtocolAction",
    "ProtocolActionPolicy",
    "ProtocolRegistry",
    "ProtocolRegistryError",
    "ProtocolWrapperPolicy",
    "protocol_registry",
]

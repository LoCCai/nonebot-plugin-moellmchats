from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import re
from types import MappingProxyType
from typing import Any, cast
import unicodedata

DISCOVERY_FEATURES_KEY = "discovery_features"
COMPAT_COMMAND_PREFIXES_KEY = "_moellm_compat_command_prefixes"

MAX_DISCOVERY_FEATURES = 128
MAX_DISCOVERY_PLUGIN_CHARS = 48_000
MAX_DISCOVERY_TRIGGERS = 16
MAX_LLM_INTENTS = 16
MAX_DISCOVERY_CATALOG_CHARS = 96_000

_MAX_TITLE_CHARS = 80
_MAX_SUMMARY_CHARS = 240
_MAX_DETAIL_CHARS = 600
_MAX_TRIGGER_CHARS = 240
_MAX_PERMISSION_CHARS = 200
_MAX_CATALOG_TRIGGER_CHARS = 640
_MAX_COMPAT_DESCRIPTION_CHARS = 28_000
_MIN_LLM_INTENT_CHARS = 4
_MAX_LLM_INTENT_CHARS = 80

_FT_TAG_RE = re.compile(r"</?ft(?:\s+[^>]*)?>", re.IGNORECASE)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WHITESPACE_RE = re.compile(r"\s+")
_MARKDOWN_TRIGGER_RE = re.compile(r"`([^`]+)`")
_PERMISSION_RE = re.compile(r"权限[：:]\s*([^。；;\n]+)")

_USER_TRIGGER_TYPES = frozenset({"command", "direct", "message", "regex"})
_TRIGGER_LABELS = {
    "command": "命令",
    "direct": "直接消息",
    "message": "消息",
    "regex": "消息模式",
    "event": "事件",
    "schedule": "定时",
}


def _freeze_projection(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_projection(item) for key, item in value.items()}
        )
    if isinstance(value, list | tuple):
        return tuple(_freeze_projection(item) for item in value)
    return value


@dataclass(frozen=True)
class PicMenuProjectionSnapshot:
    """Detached identity for one already-installed PicMenu memory generation."""

    plugins: Mapping[str, Mapping[str, object]]
    plugin_count: int
    feature_count: int
    digest: str

    @classmethod
    def empty(cls) -> "PicMenuProjectionSnapshot":
        return cls.from_infos(())

    @classmethod
    def from_infos(cls, raw_infos: object) -> "PicMenuProjectionSnapshot":
        projected = project_picmenu_infos(raw_infos)
        canonical = json.dumps(
            projected,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        detached = json.loads(canonical.decode("utf-8"))
        feature_count = sum(
            len(item.get("menu_data", ()))
            for item in detached.values()
            if isinstance(item, Mapping)
            and isinstance(item.get("menu_data"), list)
        )
        return cls(
            plugins=MappingProxyType(
                {
                    str(plugin_id): cast(
                        "Mapping[str, object]",
                        _freeze_projection(item),
                    )
                    for plugin_id, item in detached.items()
                }
            ),
            plugin_count=len(detached),
            feature_count=feature_count,
            digest=hashlib.sha256(canonical).hexdigest(),
        )

    @property
    def fingerprint(self) -> tuple[str, int, int]:
        return self.digest, self.plugin_count, self.feature_count


@dataclass(frozen=True)
class BusinessIntentResolution:
    """Fail-closed exact business-intent ownership decision."""

    normalized_intent: str
    status: str
    owners: tuple[str, ...] = ()

    @property
    def owner(self) -> str | None:
        return self.owners[0] if self.status == "unique" else None


def _clean_text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = _FT_TAG_RE.sub("", value)
    cleaned = _CONTROL_RE.sub("", cleaned)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip()
    return cleaned[:limit]


def normalize_llm_intent(value: object) -> str:
    """Normalize one user phrase for exact alias matching.

    NFKC and case-folding close width/case drift. Whitespace and Unicode
    punctuation are removed so cosmetic chat punctuation cannot change the
    owner, while lexical differences still require an explicit alias.
    """

    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(
        character
        for character in normalized
        if not character.isspace()
        and not unicodedata.category(character).startswith("P")
    )


def _llm_intents(item: Mapping[str, Any]) -> tuple[str, ...]:
    raw = item.get("pmn_llm_intents")
    if raw is None:
        raw = item.get("llm_intents")
    if not isinstance(raw, (list, tuple)) or len(raw) > MAX_LLM_INTENTS:
        return ()
    result: list[str] = []
    seen: set[str] = set()
    for value in raw:
        cleaned = _clean_text(value, _MAX_LLM_INTENT_CHARS + 1)
        if not _MIN_LLM_INTENT_CHARS <= len(cleaned) <= _MAX_LLM_INTENT_CHARS:
            continue
        identity = normalize_llm_intent(cleaned)
        if not identity or identity in seen:
            continue
        result.append(cleaned)
        seen.add(identity)
    return tuple(result)


def _infer_trigger_type(method: str) -> str:
    lowered = method.casefold()
    if any(token in lowered for token in ("定时", "计划", "schedule", "cron")):
        return "schedule"
    if any(
        token in lowered
        for token in (
            "事件",
            "被动",
            "自动",
            "notice",
            "request",
            "event",
        )
    ):
        return "event"
    if any(token in lowered for token in ("正则", "regex")):
        return "regex"
    if any(token in lowered for token in ("命令", "指令", "command")):
        return "command"
    if any(token in lowered for token in ("直接", "关键词", "direct")):
        return "direct"
    return "message"


def _normalize_trigger_type(value: object, *, fallback: str) -> str:
    raw = _clean_text(value, 32).casefold()
    aliases = {
        "cmd": "command",
        "command": "command",
        "命令": "command",
        "direct": "direct",
        "keyword": "direct",
        "关键词": "direct",
        "regex": "regex",
        "正则": "regex",
        "message": "message",
        "消息": "message",
        "event": "event",
        "notice": "event",
        "request": "event",
        "事件": "event",
        "schedule": "schedule",
        "cron": "schedule",
        "定时": "schedule",
    }
    return aliases.get(raw, fallback)


def _append_trigger(
    result: list[dict[str, str]],
    seen: set[tuple[str, str]],
    *,
    trigger_type: str,
    value: object,
) -> None:
    if len(result) >= MAX_DISCOVERY_TRIGGERS:
        return
    cleaned = _clean_text(value, _MAX_TRIGGER_CHARS)
    identity = (trigger_type, cleaned)
    if not cleaned or identity in seen:
        return
    result.append({"type": trigger_type, "value": cleaned})
    seen.add(identity)


def _menu_triggers(item: Mapping[str, Any]) -> tuple[dict[str, str], ...]:
    method = _clean_text(item.get("trigger_method"), _MAX_DETAIL_CHARS)
    fallback_type = _infer_trigger_type(method)
    triggers: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    raw_structured = item.get("pmn_triggers")
    if isinstance(raw_structured, (list, tuple)):
        for raw_trigger in raw_structured:
            if not isinstance(raw_trigger, Mapping):
                continue
            trigger_type = _normalize_trigger_type(
                raw_trigger.get("type"),
                fallback=fallback_type,
            )
            _append_trigger(
                triggers,
                seen,
                trigger_type=trigger_type,
                value=raw_trigger.get("value"),
            )

    if not triggers:
        condition = item.get("trigger_condition")
        _append_trigger(
            triggers,
            seen,
            trigger_type=fallback_type,
            value=condition,
        )

    if not triggers and method:
        markdown_values = _MARKDOWN_TRIGGER_RE.findall(method)
        for value in markdown_values:
            _append_trigger(
                triggers,
                seen,
                trigger_type=fallback_type,
                value=value,
            )

    return tuple(triggers)


def _permission_hint(item: Mapping[str, Any], *, has_structured: bool) -> str:
    explicit = _clean_text(item.get("permission"), _MAX_PERMISSION_CHARS)
    if explicit:
        return explicit
    if has_structured:
        condition = _clean_text(
            item.get("trigger_condition"),
            _MAX_PERMISSION_CHARS,
        )
        if condition:
            return condition
    detail = _clean_text(item.get("detail_des"), _MAX_DETAIL_CHARS)
    match = _PERMISSION_RE.search(detail)
    return match.group(1)[:_MAX_PERMISSION_CHARS].strip() if match else ""


def normalize_menu_data(raw_menu: object) -> tuple[dict[str, object], ...]:
    """Turn PicMenu/QWeb-compatible metadata into bounded discovery hints.

    Invalid fields are ignored instead of being coerced with ``str()``.  The
    result deliberately contains no handler, API name, credential or authority
    bit: it is an intent-discovery projection, not an execution contract.
    """

    if not isinstance(raw_menu, (list, tuple)):
        return ()

    features: list[dict[str, object]] = []
    consumed_chars = 0
    for raw_item in raw_menu:
        if len(features) >= MAX_DISCOVERY_FEATURES:
            break
        if not isinstance(raw_item, Mapping):
            continue

        title = _clean_text(raw_item.get("func"), _MAX_TITLE_CHARS)
        if not title:
            continue
        summary = _clean_text(raw_item.get("brief_des"), _MAX_SUMMARY_CHARS)
        detail = _clean_text(raw_item.get("detail_des"), _MAX_DETAIL_CHARS)
        if not summary:
            summary = detail[:_MAX_SUMMARY_CHARS] or title

        triggers = _menu_triggers(raw_item)
        has_structured = isinstance(raw_item.get("pmn_triggers"), (list, tuple))
        permission = _permission_hint(raw_item, has_structured=has_structured)
        invocable = any(trigger["type"] in _USER_TRIGGER_TYPES for trigger in triggers)
        feature: dict[str, object] = {
            "id": f"menu_{len(features) + 1:03d}",
            "name": title,
            "summary": summary,
            "triggers": triggers,
            "invocable": invocable,
            "hidden": (raw_item.get("pmn_hidden") if type(raw_item.get("pmn_hidden")) is bool else False),
        }
        intents = _llm_intents(raw_item)
        if intents:
            feature["llm_intents"] = intents
        if detail and detail != summary:
            feature["details"] = detail
        if permission:
            feature["permission"] = permission

        feature_chars = sum(len(value) for value in (title, summary, detail, permission)) + sum(
            len(trigger["type"]) + len(trigger["value"]) for trigger in triggers
        ) + sum(len(intent) for intent in intents)
        if consumed_chars + feature_chars > MAX_DISCOVERY_PLUGIN_CHARS:
            break
        features.append(feature)
        consumed_chars += feature_chars

    return tuple(features)


def discovery_features(info: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    raw = info.get(DISCOVERY_FEATURES_KEY)
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(item for item in raw if isinstance(item, Mapping))


def with_menu_discovery(
    info: Mapping[str, Any],
    raw_menu: object,
) -> dict[str, Any]:
    """Detach one plugin description and attach only normalized menu hints."""

    result = dict(info)
    result.pop("menu_data", None)
    result.pop(DISCOVERY_FEATURES_KEY, None)
    features = normalize_menu_data(raw_menu)
    if features:
        result[DISCOVERY_FEATURES_KEY] = features
    return result


def _object_field(value: object, name: str) -> object:
    if isinstance(value, Mapping):
        return value.get(name)
    try:
        return getattr(value, name)
    except (AttributeError, TypeError, ValueError):
        return None


def project_picmenu_infos(raw_infos: object) -> dict[str, dict[str, object]]:
    """Project PicMenu Next's installed in-memory catalog without importing it.

    PicMenu models are intentionally consumed by duck typing, so this package
    does not gain a runtime dependency on PicMenu or QWeb.  Only the public
    display fields shared with ``menu_data`` cross the optional bridge.
    """

    if not isinstance(raw_infos, (list, tuple)):
        return {}
    result: dict[str, dict[str, object]] = {}
    for raw_info in raw_infos:
        plugin_id = _clean_text(_object_field(raw_info, "plugin_id"), 128)
        if not plugin_id or plugin_id in result:
            continue
        menu: list[dict[str, object]] = []
        raw_items = _object_field(raw_info, "pm_data")
        if isinstance(raw_items, (list, tuple)):
            for raw_item in raw_items:
                item: dict[str, object] = {}
                for field_name in (
                    "func",
                    "trigger_method",
                    "trigger_condition",
                    "brief_des",
                    "detail_des",
                    "permission",
                ):
                    field_value = _object_field(raw_item, field_name)
                    if isinstance(field_value, str):
                        item[field_name] = field_value
                hidden = _object_field(raw_item, "hidden")
                if type(hidden) is bool:
                    item["pmn_hidden"] = hidden
                structured: list[dict[str, str]] = []
                raw_triggers = _object_field(raw_item, "triggers")
                if raw_triggers is None:
                    raw_triggers = _object_field(raw_item, "pmn_triggers")
                if isinstance(raw_triggers, (list, tuple)):
                    for raw_trigger in raw_triggers:
                        trigger_type = _object_field(raw_trigger, "type")
                        trigger_value = _object_field(raw_trigger, "value")
                        if isinstance(trigger_value, str):
                            structured.append(
                                {
                                    "type": (trigger_type if isinstance(trigger_type, str) else "message"),
                                    "value": trigger_value,
                                }
                            )
                if structured:
                    item["pmn_triggers"] = structured
                raw_intents = _object_field(raw_item, "llm_intents")
                if raw_intents is None:
                    raw_intents = _object_field(raw_item, "pmn_llm_intents")
                if isinstance(raw_intents, (list, tuple)):
                    intents = [
                        value
                        for value in raw_intents
                        if isinstance(value, str)
                    ]
                    if intents:
                        item["pmn_llm_intents"] = intents
                if item.get("func"):
                    menu.append(item)

        projected: dict[str, object] = {"menu_data": menu}
        for field_name in ("name", "description", "usage"):
            field_value = _object_field(raw_info, field_name)
            if isinstance(field_value, str) and field_value:
                projected[field_name] = field_value
        result[plugin_id] = projected
    return result


def build_intent_owner_index(
    plugin_info: Mapping[str, Mapping[str, Any]],
    *,
    picmenu_plugins: Mapping[str, Mapping[str, object]] | None = None,
) -> Mapping[str, tuple[tuple[str, bool], ...]]:
    """Build a detached alias -> (plugin, hidden) index for one generation."""

    owners: dict[str, dict[str, bool]] = {}

    def add_features(plugin_id: str, features: tuple[Mapping[str, Any], ...]) -> None:
        for feature in features:
            raw_intents = feature.get("llm_intents")
            if not isinstance(raw_intents, (list, tuple)):
                continue
            hidden = feature.get("hidden") is True
            for raw_intent in raw_intents:
                normalized = normalize_llm_intent(raw_intent)
                if not normalized:
                    continue
                by_plugin = owners.setdefault(normalized, {})
                # If any declaration for the same plugin is public, the owner is
                # public. Duplicate declarations do not create false ambiguity.
                by_plugin[plugin_id] = by_plugin.get(plugin_id, True) and hidden

    if picmenu_plugins is not None:
        for plugin_id, info in picmenu_plugins.items():
            if not isinstance(plugin_id, str) or not isinstance(info, Mapping):
                continue
            add_features(
                plugin_id,
                normalize_menu_data(info.get("menu_data")),
            )
    for plugin_id, info in plugin_info.items():
        if not isinstance(plugin_id, str) or not isinstance(info, Mapping):
            continue
        add_features(plugin_id, discovery_features(info))

    return MappingProxyType(
        {
            intent: tuple(sorted(by_plugin.items()))
            for intent, by_plugin in sorted(owners.items())
        }
    )


def resolve_business_intent(
    plain: str,
    *,
    owners: Mapping[str, tuple[tuple[str, bool], ...]],
    loaded_plugins: Mapping[str, object],
    is_superuser: bool,
    is_blacklisted,
) -> BusinessIntentResolution:
    """Resolve a normalized exact alias without falling through on denial."""

    normalized = normalize_llm_intent(plain)
    matches = owners.get(normalized, ())
    if not matches:
        return BusinessIntentResolution(normalized, "no_match")
    plugin_ids = tuple(sorted({plugin_id for plugin_id, _hidden in matches}))
    if len(plugin_ids) != 1:
        return BusinessIntentResolution(normalized, "ambiguous", plugin_ids)
    plugin_id, hidden = matches[0]
    if hidden and not is_superuser:
        return BusinessIntentResolution(normalized, "unavailable", (plugin_id,))
    if plugin_id not in loaded_plugins or is_blacklisted(plugin_id):
        return BusinessIntentResolution(normalized, "unavailable", (plugin_id,))
    return BusinessIntentResolution(normalized, "unique", (plugin_id,))


def discovery_directory_identity(
    plugin_info: Mapping[str, Mapping[str, Any]],
    intent_owners: Mapping[str, tuple[tuple[str, bool], ...]],
) -> tuple[int, str]:
    """Return a deterministic count/digest without handlers or credentials."""

    detached: dict[str, object] = {}
    for plugin_id, info in sorted(plugin_info.items()):
        detached[plugin_id] = {
            "name": _clean_text(info.get("name"), 512),
            "description": _clean_text(info.get("description"), 2_000),
            "usage": _clean_text(info.get("usage"), 8_000),
            "features": discovery_features(info),
        }
    payload = {
        "plugins": detached,
        "intent_owners": {
            intent: list(values) for intent, values in sorted(intent_owners.items())
        },
    }
    def mutable(value: object) -> object:
        if isinstance(value, Mapping):
            return {str(key): mutable(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [mutable(item) for item in value]
        return value

    encoded = json.dumps(
        mutable(payload),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return len(detached), hashlib.sha256(encoded).hexdigest()


def preferred_command_prefix(prefixes: tuple[str, ...]) -> str:
    if not isinstance(prefixes, tuple) or not all(
        isinstance(prefix, str) for prefix in prefixes
    ):
        raise TypeError("command_start 必须是字符串元组")
    unique = tuple(sorted(set(prefixes), key=lambda item: (len(item), item)))
    if "/" in unique:
        return "/"
    return unique[0] if unique else ""


def render_command_prefix_contract(
    description: str,
    *,
    command_prefixes: tuple[str, ...],
) -> str:
    """Replace PicMenu placeholders and state the exact runtime prefix set."""

    preferred = preferred_command_prefix(command_prefixes)
    rendered = description.replace("<命令前缀>", preferred)
    ordered = tuple(sorted(set(command_prefixes), key=lambda item: (len(item), item)))
    alternatives = tuple(prefix for prefix in ordered if prefix != preferred)

    def label(prefix: str) -> str:
        return "无前缀" if prefix == "" else repr(prefix)

    suffix = f"当前首选命令前缀为 {label(preferred)}。"
    if alternatives:
        suffix += "其他有效前缀：" + "、".join(label(prefix) for prefix in alternatives) + "。"
    suffix += "command 必须使用这里列出的真实前缀；不要输出命令前缀占位文本。"
    return f"{rendered}\n{suffix}"


def _trigger_text(feature: Mapping[str, Any], *, limit: int) -> str:
    raw = feature.get("triggers")
    if not isinstance(raw, (list, tuple)):
        return ""
    parts: list[str] = []
    for trigger in raw:
        if not isinstance(trigger, Mapping):
            continue
        trigger_type = trigger.get("type")
        value = trigger.get("value")
        if not isinstance(trigger_type, str) or not isinstance(value, str):
            continue
        label = _TRIGGER_LABELS.get(trigger_type, "触发")
        candidate = f"{label}: {value}"
        rendered = " / ".join((*parts, candidate))
        if len(rendered) > limit:
            break
        parts.append(candidate)
    return " / ".join(parts)


def build_plugin_catalog_entries(
    plugin_id: str,
    info: Mapping[str, Any],
    *,
    is_superuser: bool = False,
) -> list[str]:
    """Render one compact classifier line per discoverable plugin feature."""

    if type(is_superuser) is not bool:
        raise TypeError("is_superuser 必须是布尔值")

    display_name = str(info.get("name") or plugin_id)
    description = str(info.get("description") or "无描述")
    features = discovery_features(info)
    if not features:
        return [f"- {plugin_id} | {display_name} | {description[:160]}"]

    entries: list[str] = []
    for feature in features:
        if feature.get("hidden") is True and not is_superuser:
            continue
        name = feature.get("name")
        summary = feature.get("summary")
        if not isinstance(name, str) or not isinstance(summary, str):
            continue
        parts = [f"- {plugin_id} | {display_name} > {name} | {summary}"]
        triggers = _trigger_text(
            feature,
            limit=_MAX_CATALOG_TRIGGER_CHARS,
        )
        if triggers:
            parts.append(triggers)
        permission = feature.get("permission")
        if isinstance(permission, str) and permission:
            parts.append(f"条件: {permission}")
        entries.append(" | ".join(parts))
    return entries


def finalize_discovery_catalog(entries: list[str]) -> str:
    """Join classifier rows under one process-wide prompt budget."""

    result: list[str] = []
    consumed = 0
    truncated = False
    for entry in entries:
        if not isinstance(entry, str):
            continue
        cost = len(entry) + (1 if result else 0)
        if consumed + cost > MAX_DISCOVERY_CATALOG_CHARS:
            truncated = True
            break
        result.append(entry)
        consumed += cost
    if truncated:
        result.append("[工具目录达到安全上限，其余条目未注入；不要猜测未列出的工具]")
    return "\n".join(result)


def build_compatibility_description(
    plugin_id: str,
    info: Mapping[str, Any],
    *,
    is_superuser: bool = False,
    command_prefixes: tuple[str, ...] | None = None,
) -> str:
    """Build the selected-plugin description from usage plus bounded menu hints."""

    if type(is_superuser) is not bool:
        raise TypeError("is_superuser 必须是布尔值")
    if command_prefixes is None:
        frozen_prefixes = info.get(COMPAT_COMMAND_PREFIXES_KEY, ("/",))
        if not isinstance(frozen_prefixes, (list, tuple)) or not all(
            isinstance(prefix, str) for prefix in frozen_prefixes
        ):
            raise TypeError("generation 内冻结的 command_start 非法")
        command_prefixes = tuple(frozen_prefixes)
    if not isinstance(command_prefixes, tuple) or not all(
        isinstance(prefix, str) for prefix in command_prefixes
    ):
        raise TypeError("command_prefixes 必须是字符串元组")

    display_name = str(info.get("name") or plugin_id)
    description = str(info.get("description") or "无描述")
    usage = str(info.get("usage") or "无用法说明")
    base = f"插件名称：{display_name}。功能描述：{description}。原始用法说明：{usage}"
    features = discovery_features(info)
    if not features:
        return render_command_prefix_contract(
            base,
            command_prefixes=command_prefixes,
        )

    lines = [
        base,
        "菜单功能提示（仅用于选择和生成消息，不代表权限；目标插件仍会复核权限与业务规则）：",
    ]
    passive_names: list[str] = []
    for feature in features:
        if feature.get("hidden") is True and not is_superuser:
            continue
        name = feature.get("name")
        summary = feature.get("summary")
        if not isinstance(name, str) or not isinstance(summary, str):
            continue
        if feature.get("invocable") is not True:
            passive_names.append(name)
            continue
        feature_parts = [f"- {name}: {summary}"]
        triggers = _trigger_text(feature, limit=_MAX_CATALOG_TRIGGER_CHARS)
        if triggers:
            feature_parts.append(triggers)
        details = feature.get("details")
        if isinstance(details, str) and details and details != summary:
            feature_parts.append(f"说明: {details}")
        permission = feature.get("permission")
        if isinstance(permission, str) and permission:
            feature_parts.append(f"条件: {permission}")
        lines.append("；".join(feature_parts))
    if passive_names:
        rendered = "、".join(passive_names)
        lines.append(f"以下功能只由真实事件或定时任务触发，不得通过 command 伪造：{rendered}")
    lines.append(
        "生成 command 时只使用上面标为消息/命令触发的真实格式；"
        "不要输出命令前缀占位文本，也不要改为直接调用 Bot/NapCat API。"
    )
    result = render_command_prefix_contract(
        "\n".join(lines),
        command_prefixes=command_prefixes,
    )
    if len(result) <= _MAX_COMPAT_DESCRIPTION_CHARS:
        return result
    return result[: _MAX_COMPAT_DESCRIPTION_CHARS - 12] + "\n...[菜单已截断]"

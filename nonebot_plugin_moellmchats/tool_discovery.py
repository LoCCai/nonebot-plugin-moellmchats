from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any

DISCOVERY_FEATURES_KEY = "discovery_features"

MAX_DISCOVERY_FEATURES = 128
MAX_DISCOVERY_PLUGIN_CHARS = 48_000
MAX_DISCOVERY_TRIGGERS = 16
MAX_DISCOVERY_CATALOG_CHARS = 96_000

_MAX_TITLE_CHARS = 80
_MAX_SUMMARY_CHARS = 240
_MAX_DETAIL_CHARS = 600
_MAX_TRIGGER_CHARS = 240
_MAX_PERMISSION_CHARS = 200
_MAX_CATALOG_TRIGGER_CHARS = 640
_MAX_COMPAT_DESCRIPTION_CHARS = 28_000

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


def _clean_text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = _FT_TAG_RE.sub("", value)
    cleaned = _CONTROL_RE.sub("", cleaned)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip()
    return cleaned[:limit]


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
        if detail and detail != summary:
            feature["details"] = detail
        if permission:
            feature["permission"] = permission

        feature_chars = sum(len(value) for value in (title, summary, detail, permission)) + sum(
            len(trigger["type"]) + len(trigger["value"]) for trigger in triggers
        )
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
                if item.get("func"):
                    menu.append(item)

        projected: dict[str, object] = {"menu_data": menu}
        for field_name in ("name", "description", "usage"):
            field_value = _object_field(raw_info, field_name)
            if isinstance(field_value, str) and field_value:
                projected[field_name] = field_value
        result[plugin_id] = projected
    return result


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
) -> str:
    """Build the selected-plugin description from usage plus bounded menu hints."""

    if type(is_superuser) is not bool:
        raise TypeError("is_superuser 必须是布尔值")

    display_name = str(info.get("name") or plugin_id)
    description = str(info.get("description") or "无描述")
    usage = str(info.get("usage") or "无用法说明")
    base = f"插件名称：{display_name}。功能描述：{description}。原始用法说明：{usage}"
    features = discovery_features(info)
    if not features:
        return base

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
        "不要原样输出 <命令前缀> 占位符，也不要改为直接调用 Bot/NapCat API。"
    )
    result = "\n".join(lines)
    if len(result) <= _MAX_COMPAT_DESCRIPTION_CHARS:
        return result
    return result[: _MAX_COMPAT_DESCRIPTION_CHARS - 12] + "\n...[菜单已截断]"

"""Read effective routes without selecting a model; atomically edit role pools."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
import json
import re

from .private_files import atomic_write_private_text
from .runtime_snapshot import mutable_value, runtime_snapshots

POOL_ROLES = {"分类": "category", "category": "category", "0": "0", "1": "1", "2": "2"}
POOL_HELP = (
    "超级管理员命令（参数用空格分隔，多个模型用分号分隔）：\n"
    "!查看LLM链路 [全部|文本|图片]\n!查看模型池 [分类|0|1|2]\n"
    "!设置模型池 分类 模型编号;完整模型名 (供应商)\n"
    "!添加模型池 0 模型编号\n!移除模型池 0 模型编号\n!关闭模型池 0\n"
    "0=简单、1=中等、2=复杂。编号从 !查看模型 获取。\n"
    "关闭池后恢复对应固定模型；!恢复模型额度 模型编号 可清除耗尽标记。"
)


def pool_role(raw: str) -> str:
    role = POOL_ROLES.get(raw.lower())
    if role is None:
        raise ValueError("模型池类型只能是 分类、0（简单）、1（中等）、2（复杂）。")
    return role


def _status(selector, model: Mapping, name: str) -> str:
    if selector.is_model_exhausted(name):
        return "额度耗尽，已退出"
    keys = list(model.get("keys", ())) if isinstance(model.get("keys"), (list, tuple)) else []
    legacy = model.get("key")
    keys.extend(legacy if isinstance(legacy, (list, tuple)) else [legacy])
    if not any(isinstance(key, str) and key.strip() for key in keys):
        return "未配置密钥"
    return "可参与选择"


def _thinking(model: Mapping) -> str:
    # Only known scalar options are exposed, never arbitrary provider payloads.
    payload = model.get("extra_payload", {})
    paths = (
        ("思考开关", ("enable_thinking",)),
        ("思考强度", ("reasoning_effort",)),
        ("思考模式", ("thinking", "type")),
        ("思考预算", ("thinking", "budget_tokens")),
        ("思考预算", ("thinking_budget",)),
        ("思考强度", ("reasoning", "effort")),
        ("思考开关", ("extra_body", "enable_thinking")),
        ("思考强度", ("extra_body", "reasoning_effort")),
        ("思考模式", ("extra_body", "thinking", "type")),
        ("思考强度", ("extra_body", "google", "thinking_config", "thinking_level")),
        ("思考预算", ("extra_body", "google", "thinking_config", "thinking_budget")),
    )
    values = []
    for label, path in paths:
        value = payload
        for key in path:
            value = value.get(key) if isinstance(value, Mapping) else None
        if isinstance(value, (bool, int)):
            display = ("开" if value else "关") if isinstance(value, bool) else str(value)
        elif isinstance(value, str) and value in {
            "none",
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
            "auto",
            "enabled",
            "disabled",
            "adaptive",
        }:
            display = value
        else:
            continue
        values.append(f"{label}={display}")
    return "、".join(values) or "思考参数未指定，遵循服务商默认"


def format_pools(selector, role: str | None = None) -> str:
    state = selector._active_state()
    pools = state.model_config.get("model_pools", {})
    roles = (role,) if role is not None else ("category", "0", "1", "2")
    lines = ["模型池（逐次轮转；额度耗尽自动跳过，工具/识图请求还会筛选能力）"]
    for key in roles:
        label = "分类" if key == "category" else f"{key}档（{'简单中等复杂'[int(key) * 2 : int(key) * 2 + 2]}）"
        names = pools.get(key, ())
        lines.append(f"{label}：" + ("已关闭，使用固定模型" if not names else ""))
        for name in names:
            model = state.models[name]
            lines.append(f"  {name} · {_status(selector, model, name)}")
    lines.append("管理方法：!模型池帮助；完整链路：!查看LLM链路")
    return "\n".join(lines)


def _model_view(selector, state, name: str) -> dict:
    """One model's display facts; secrets never leave the provider config."""
    model = state.models.get(name, {})
    return {
        "name": name,
        "status": _status(selector, model, name),
        "exhausted": selector.is_model_exhausted(name),
        "thinking": _thinking(model),
        "vision": bool(model.get("is_vision")),
        "tools": not model.get("no_tools"),
        "stream": bool(model.get("stream")),
    }


def chain_data(selector, settings: Mapping, include_archived: bool = False) -> dict:
    """Read the effective routing table as structured data for text/image views.

    Quota-exhausted models are archived unless ``include_archived`` is true:
    they drop out of explicit-backup chains and the parameter list (a summary
    count replaces them). An exhausted fixed-role model stays visible with a
    marker because the role itself is still bound to it.
    """
    state = selector._active_state()
    cfg = state.model_config
    pools = cfg.get("model_pools", {})
    moe = cfg.get("use_moe", False)
    routing = cfg.get("capability_routing", {}).get("enabled", False)
    snapshot = runtime_snapshots.active()
    routes: list[dict] = []
    used: list[str] = []

    def use(name):
        if name and name not in used:
            used.append(name)

    def route(label, key, role=None, fallback=None):
        if routing and key in {"selected_model", "category_model", "vision_model", "summary_model"}:
            routes.append({"kind": "capability", "label": label})
            return
        names = tuple(pools.get(role, ())) if role is not None else ()
        if names:
            routes.append(
                {"kind": "pool", "label": label, "pool": role, "models": [_model_view(selector, state, name) for name in names]}
            )
            for name in names:
                use(name)
            return
        name = fallback if fallback is not None else cfg.get(key)
        backups = tuple(cfg.get("model_fallbacks", {}).get(name, ())) if name else ()
        visible = backups if include_archived else tuple(backup for backup in backups if not selector.is_model_exhausted(backup))
        routes.append(
            {
                "kind": "fixed",
                "label": label,
                "model": _model_view(selector, state, name) if name else None,
                "backups": [_model_view(selector, state, backup) for backup in visible],
                "dropped_backups": len(backups) - len(visible),
                "all_backups_exhausted": bool(backups) and not visible,
            }
        )
        use(name)

    if moe:
        route("意图分类（仅超级管理员）", "category_model", "category")
        efforts = cfg.get("moe_reasoning_efforts", {})
        for key, label in (("0", "简单"), ("1", "中等"), ("2", "复杂")):
            effort = efforts.get(key) if isinstance(efforts, Mapping) else None
            suffix = f"·{effort}" if effort in {"low", "medium", "high", "max"} else ""
            route(f"超级管理员文本 {key}档·{label}{suffix}", "selected_model", key, cfg.get("moe_models", {}).get(key))
        route("普通用户文本·固定0档·不调用分类模型", "selected_model", "0")
    else:
        route("文本对话·固定0档·不调用分类模型", "selected_model", "0")
    route("用户图片识图（优先于文本分档）", "vision_model")
    route("绘图/改图工具", "image_model")
    route("音视频/文件多模态工具", "multimodal_model")
    route("群聊总结工具", "chat_summary_model", "2")
    from .model_selector import MEDIA_ROLES

    for role, (label, _kind) in MEDIA_ROLES.items():
        if role not in {"image_model", "multimodal_model", "chat_summary_model"}:
            route(label + "工具", role)
    route("工具草稿复核", "summary_model", "category")
    if routing:
        used = list(cfg.get("capability_routing", {}).get("models", {}))
    else:
        # Include the parameters/status of explicitly configured fallback models.
        for name in used:
            for backup in cfg.get("model_fallbacks", {}).get(name, ()):
                if backup not in used:
                    used.append(backup)
    # Disabled/archived providers no longer appear in active routes. Retain
    # their quota inventory in the existing explicit "全部" archive view.
    for name in selector._quota_status().exhausted:
        use(name)
    entries = [_model_view(selector, state, name) for name in used]
    return {
        "moe": moe,
        "tools": bool(cfg.get("use_tools")),
        "web_search": bool(cfg.get("use_web_search")),
        "capability_routing": routing,
        "generation": snapshot.generation if snapshot is not None else None,
        "routes": routes,
        "tool_image_vision_enabled": bool(settings.get("tool_image_vision_enabled", True)),
        "tool_image_vision_excluded_tools": list(settings.get("tool_image_vision_excluded_tools", ())),
        "classification_timeout_seconds": settings.get("classification_timeout_seconds", 20),
        "models": [entry for entry in entries if include_archived or not entry["exhausted"]],
        "archived": [entry for entry in entries if entry["exhausted"]],
        "include_archived": include_archived,
    }


def _route_lines(entry: Mapping) -> list[str]:
    label = entry["label"]
    if entry["kind"] == "capability":
        return [f"{label}：由能力路由按请求选择（固定模型仅为绑定配置）"]
    if entry["kind"] == "pool":
        return [f"{label}：{'分类' if entry['pool'] == 'category' else entry['pool']}模型池，逐次轮转"]
    model = entry["model"]
    if model is None:
        return [f"{label}：未配置"]
    name = model["name"] + ("（额度耗尽）" if model["exhausted"] else "")
    lines = [f"{label}：{name}"]
    if entry["all_backups_exhausted"]:
        lines.append("  显式备用：均额度耗尽")
    elif entry["backups"]:
        lines.append("  显式备用：" + " → ".join(backup["name"] for backup in entry["backups"]))
    return lines


def format_chain(selector, settings: Mapping, include_archived: bool = False) -> str:
    data = chain_data(selector, settings, include_archived)
    lines = [
        "当前 LLM 链路（生效配置）",
        f"MoE：{'开' if data['moe'] else '关'}；工具：{'开' if data['tools'] else '关'}；"
        f"联网：{'开' if data['web_search'] else '关'}",
    ]
    if data["generation"] is not None:
        lines.append(f"运行资源版本：{data['generation']}")
    for entry in data["routes"]:
        lines.extend(_route_lines(entry))
    lines.extend(
        [
            "工具产图二次识图：" + ("开" if data["tool_image_vision_enabled"] else "关"),
            "免二次识图工具：" + ("、".join(data["tool_image_vision_excluded_tools"]) or "无"),
            f"分类总预算：{data['classification_timeout_seconds']}秒；超时使用降级路径。",
            "模型参数（已合并默认/供应商/模型配置；能力为配置声明）：",
        ]
    )
    for entry in data["models"]:
        lines.append(
            f"• {entry['name']} · {entry['status']}\n"
            f"  {entry['thinking']}；识图={'是' if entry['vision'] else '否'}；"
            f"工具={'否' if not entry['tools'] else '是'}；流式={'开' if entry['stream'] else '关'}"
        )
    if data["archived"] and not include_archived:
        lines.append(f"已归档 {len(data['archived'])} 个额度耗尽模型（!查看LLM链路 全部 查看）。")
    lines.extend(
        [
            "池优先于固定模型。临时限流尝试同池备用；额度耗尽标记会跨重启保留。",
            "固定模型不可用时按显式备用筛选；模型配置不替代宿主的绘图授权。",
            "!查看模型池 ／ !模型池帮助；固定角色沿用 !设置视觉模型、!设置媒体模型 等命令。",
        ]
    )
    return "\n".join(lines)


def manage_pool(selector, action: str, role: str, query: str = "") -> str:
    """No await between candidate preparation, private atomic write and snapshot patch."""
    role = pool_role(role)
    state = selector._active_state()
    cfg = mutable_value(state.model_config)
    pools = cfg.setdefault("model_pools", {})
    if action == "关闭":
        if query.strip():
            raise ValueError("关闭模型池只需填写类型，例如：!关闭模型池 0")
        pools.pop(role, None)
    else:
        if action not in {"设置", "添加", "移除"}:
            raise ValueError("未知模型池操作。")
        parts = re.split(r"[;；\n]", query)
        if not 1 <= len(parts) <= 32 or any(not part.strip() for part in parts):
            raise ValueError("请填写1至32个模型名或编号，多个模型用分号分隔，不能留空。")
        names = []
        for part in parts:
            name = selector.resolve_model_name(part.strip())
            if not name:
                raise ValueError("模型不存在或供应商不唯一。请用 !查看模型 查询编号或完整模型名。")
            if name in names:
                raise ValueError("本次输入包含重复模型，请检查后重试。")
            names.append(name)
        current = list(pools.get(role, ()))
        if action == "设置":
            pools[role] = names
        elif action == "添加":
            pools[role] = current + [name for name in names if name not in current]
        else:
            if any(name not in current for name in names):
                raise ValueError("要移除的模型不在此池中，本次未修改。")
            pools[role] = [name for name in current if name not in names]
            if not pools[role]:
                raise ValueError("不能移除最后一个模型；如需恢复固定模型，请使用 !关闭模型池。")
    selector.validate_model_pools(pools, state.models)
    if pools and cfg.get("capability_routing", {}).get("enabled"):
        raise ValueError("能力路由已启用，与模型池不能同时启用。本次未修改。")
    candidate = replace(state, model_config=cfg)
    atomic_write_private_text(selector.model_config_file, json.dumps(cfg, ensure_ascii=False, indent=4))
    selector.model_config = cfg
    # Match the selector's existing fixed-role setters, preserving bound requests.
    from .model_selector import model_selector

    if selector is model_selector:
        runtime_snapshots.patch_current(model_state=candidate)
    return "模型池已保存，对后续请求生效（进行中的请求保留原配置）。\n" + format_pools(selector, role)

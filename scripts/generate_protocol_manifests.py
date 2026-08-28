#!/usr/bin/env python3
"""Generate the pinned, offline OneBot/NapCat action inventory.

This maintainer command is the only code in the project that reads upstream
checkouts.  Runtime imports and normal package builds consume the generated
JSON files and never access the network.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Mapping
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RESOURCE_DIR = ROOT / "nonebot_plugin_moellmchats" / "protocol_resources"
LOCK_PATH = RESOURCE_DIR / "sources.json"
INVENTORY_PATH = RESOURCE_DIR / "actions.json"
POLICY_PATH = RESOURCE_DIR / "policies.json"
ACTION_DOC_PATH = ROOT / "docs" / "protocol-actions.md"

PROTOCOL_COUNTS = {
    "onebot_v11": 38,
    "onebot_v12": 31,
    "napcat_v11": 175,
}
TEXT_MESSAGE_ACTION_IDS = frozenset(
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

_HEADING_RE = re.compile(r"^## `([^`]+)`\s*(.*)$", re.MULTILINE)
_CODE_RE = re.compile(r"`([^`]+)`")
_SAFE_TOOL_CHAR_RE = re.compile(r"[A-Za-z0-9_-]")


class ManifestGenerationError(RuntimeError):
    """The pinned upstream input or reviewed policy is inconsistent."""


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ManifestGenerationError(f"cannot load JSON {path}: {error}") from error
    if not isinstance(value, dict):
        raise ManifestGenerationError(f"JSON root must be an object: {path}")
    return value


def _canonical_json(value: Any) -> str:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return _sha256_bytes(encoded)


def _git_head(path: Path) -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise ManifestGenerationError(f"cannot resolve pinned checkout {path}") from error


def _verify_checkout(path: Path, expected: str, label: str) -> None:
    actual = _git_head(path)
    if actual != expected:
        raise ManifestGenerationError(f"{label} checkout drift: expected {expected}, got {actual}")


def escape_action_name(action: str, protocol: str) -> str:
    """Map an API name to a deterministic, collision-checkable tool name."""

    escaped = "".join(char if _SAFE_TOOL_CHAR_RE.fullmatch(char) else f"_x{ord(char):02x}_" for char in action)
    name = f"{protocol}__{escaped}"
    if len(name) > 64:
        digest = hashlib.sha256(action.encode("utf-8")).hexdigest()[:12]
        name = f"{protocol}__{escaped[: 64 - len(protocol) - 16]}__{digest}"
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
        raise ManifestGenerationError(f"unsafe generated tool name for {protocol}:{action}")
    return name


def _schema_type(raw: str) -> dict[str, Any]:
    text = raw.lower().replace(" ", "")
    if "message" in text:
        return {
            "oneOf": [
                {"type": "string"},
                {
                    "type": "array",
                    "items": {"type": "object"},
                },
            ]
        }
    if text.startswith("list") or text.startswith("array"):
        return {"type": "array", "items": {}}
    if "bool" in text:
        return {"type": "boolean"}
    if "float" in text or "number" in text:
        return {"type": "number"}
    if "int" in text:
        return {"type": "integer"}
    if "object" in text or "self" in text:
        return {"type": "object", "properties": {}}
    return {"type": "string"}


def _strip_markdown(value: str) -> str:
    value = re.sub(r"\[([^]]+)]\([^)]*\)", r"\1", value)
    value = value.replace("**", "").replace("`", "")
    return re.sub(r"\s+", " ", value).strip()


def _table_rows(block: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped or "|" not in stripped:
            if rows:
                break
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in cells):
            continue
        rows.append(cells)
    return rows


def _markdown_request_schema(section: str, *, version: int) -> dict[str, Any]:
    marker = "### 参数" if version == 11 else '=== "请求参数"'
    start = section.find(marker)
    if start < 0:
        return {"type": "object", "properties": {}, "additionalProperties": False}
    request = section[start + len(marker) :]
    end_markers = ("### 响应数据", '=== "响应数据"')
    end_positions = [request.find(item) for item in end_markers if request.find(item) >= 0]
    if end_positions:
        request = request[: min(end_positions)]
    if re.search(r"(?:^|\n)\s*无[。.]?\s*(?:\n|$)", request):
        return {"type": "object", "properties": {}, "additionalProperties": False}

    rows = _table_rows(request)
    if rows and any("字段名" in cell for cell in rows[0]):
        rows = rows[1:]
    properties: dict[str, Any] = {}
    required: list[str] = []
    for cells in rows:
        if len(cells) < 3:
            continue
        names = _CODE_RE.findall(cells[0])
        if not names:
            raw_name = _strip_markdown(cells[0])
            names = [raw_name] if re.fullmatch(r"[A-Za-z0-9_.-]+", raw_name) else []
        if not names:
            continue
        data_type = cells[1]
        has_default_column = len(cells) >= 4
        default = cells[2] if has_default_column else "-"
        description = cells[3] if has_default_column else cells[2]
        for name in names:
            prop = _schema_type(data_type)
            prop["description"] = _strip_markdown(description)[:800]
            normalized_default = _strip_markdown(default)
            if normalized_default not in {"", "-", "无"}:
                lowered = normalized_default.lower()
                if lowered in {"true", "false"}:
                    prop["default"] = lowered == "true"
                elif re.fullmatch(r"-?\d+", normalized_default):
                    prop["default"] = int(normalized_default)
                elif re.fullmatch(r"-?\d+(?:\.\d+)?", normalized_default):
                    prop["default"] = float(normalized_default)
            properties[name] = prop
            conditional = any(token in prop["description"] for token in ("可选", "当 ", "当`", "时必须", "任选"))
            if normalized_default in {"", "-"} and not conditional:
                required.append(name)

    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = sorted(set(required))
    return schema


def _parse_markdown_actions(files: Iterable[Path], protocol: str, version: int) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        matches = list(_HEADING_RE.finditer(text))
        for index, match in enumerate(matches):
            action = match.group(1).strip()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            section = text[match.end() : end]
            summary = _strip_markdown(match.group(2)) or action
            actions.append(
                {
                    "id": f"{protocol}:{action}",
                    "protocol": protocol,
                    "action": action,
                    "tool_name": escape_action_name(action, protocol),
                    "summary": summary[:500],
                    "request_schema": _markdown_request_schema(section, version=version),
                    "deprecated": False,
                    "source_path": str(path.name if version == 11 else path.relative_to(path.parents[2])),
                }
            )
    return actions


def _resolve_openapi_schema(value: Any, document: Mapping[str, Any], stack: tuple[str, ...] = ()) -> Any:
    if isinstance(value, list):
        return [_resolve_openapi_schema(item, document, stack) for item in value]
    if not isinstance(value, Mapping):
        return deepcopy(value)
    ref = value.get("$ref")
    if isinstance(ref, str):
        if not ref.startswith("#/") or ref in stack:
            raise ManifestGenerationError(f"unsupported or recursive OpenAPI ref: {ref}")
        target: Any = document
        for part in ref[2:].split("/"):
            target = target[part.replace("~1", "/").replace("~0", "~")]
        merged = _resolve_openapi_schema(target, document, (*stack, ref))
        if not isinstance(merged, dict):
            raise ManifestGenerationError(f"OpenAPI ref is not an object: {ref}")
        for key, item in value.items():
            if key != "$ref":
                merged[key] = _resolve_openapi_schema(item, document, stack)
        return merged
    result: dict[str, Any] = {}
    for key, item in value.items():
        if key in {"examples", "example", "xml", "externalDocs", "discriminator"}:
            continue
        result[str(key)] = _resolve_openapi_schema(item, document, stack)
    return result


def _normalize_schema(value: Any, *, outer: bool = False) -> Any:
    if isinstance(value, list):
        return [_normalize_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    normalized = {key: _normalize_schema(item) for key, item in value.items()}
    raw_type = normalized.get("type")
    if raw_type == "object" or (isinstance(raw_type, list) and "object" in raw_type):
        normalized.setdefault("properties", {})
    if outer:
        normalized.setdefault("type", "object")
        normalized.setdefault("properties", {})
        normalized["additionalProperties"] = False
    required = normalized.get("required")
    if isinstance(required, list):
        normalized["required"] = sorted(set(map(str, required)))
    properties = normalized.get("properties")
    if isinstance(properties, dict):
        normalized["properties"] = {key: properties[key] for key in sorted(properties)}
    return normalized


def _parse_napcat(openapi_path: Path) -> list[dict[str, Any]]:
    document = _load_json(openapi_path)
    paths = document.get("paths")
    if not isinstance(paths, dict):
        raise ManifestGenerationError("NapCat OpenAPI paths is not an object")
    actions: list[dict[str, Any]] = []
    for raw_path, path_item in sorted(paths.items()):
        if not isinstance(raw_path, str) or not raw_path.startswith("/") or not isinstance(path_item, dict):
            raise ManifestGenerationError(f"invalid NapCat path: {raw_path!r}")
        action = raw_path[1:]
        operation = path_item.get("post")
        if not isinstance(operation, dict):
            raise ManifestGenerationError(f"NapCat action is not POST: {action}")
        content = (operation.get("requestBody") or {}).get("content", {})
        schema = (content.get("application/json") or {}).get("schema", {})
        schema = _normalize_schema(
            _resolve_openapi_schema(schema, document),
            outer=True,
        )
        actions.append(
            {
                "id": f"napcat_v11:{action}",
                "protocol": "napcat_v11",
                "action": action,
                "tool_name": escape_action_name(action, "napcat_v11"),
                "summary": str(operation.get("summary") or action).strip()[:500],
                "request_schema": schema,
                "deprecated": bool(operation.get("deprecated", False)),
                "source_path": "src/api/4.18.19/openapi.json",
            }
        )
    return actions


def _validate_actions(actions: list[dict[str, Any]]) -> None:
    ids = [str(item.get("id")) for item in actions]
    tool_names = [str(item.get("tool_name")) for item in actions]
    if len(ids) != len(set(ids)):
        raise ManifestGenerationError("duplicate action identity")
    if len(tool_names) != len(set(tool_names)):
        raise ManifestGenerationError("generated tool-name collision")
    counts: dict[str, int] = {}
    for item in actions:
        protocol = str(item.get("protocol"))
        counts[protocol] = counts.get(protocol, 0) + 1
        schema = item.get("request_schema")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise ManifestGenerationError(f"missing object schema: {item.get('id')}")
        if schema.get("additionalProperties") is not False:
            raise ManifestGenerationError(f"outer schema is not strict: {item.get('id')}")
    if counts != PROTOCOL_COUNTS:
        raise ManifestGenerationError(f"action-set drift: expected {PROTOCOL_COUNTS}, got {counts}")


def _validate_policies(actions: list[dict[str, Any]], policies: list[dict[str, Any]]) -> None:
    action_ids = {str(item["id"]) for item in actions}
    policy_ids = [str(item.get("id")) for item in policies]
    if set(policy_ids) != action_ids or len(policy_ids) != len(set(policy_ids)):
        raise ManifestGenerationError("reviewed policy set does not exactly match action set")
    for policy in policies:
        if policy.get("reviewed") is not True:
            raise ManifestGenerationError(f"unreviewed action policy: {policy.get('id')}")
        required_fields = {
            "id",
            "reviewed",
            "denial_reason",
            "exposure",
            "effect",
            "risk",
            "scope",
            "permission",
            "confirmation",
            "capability",
            "rate_limit",
            "redact_fields",
            "retry",
            "allowed_scenes",
            "injected_params",
            "intent_keywords",
        }
        allowed_fields = required_fields | {"argument_profile"}
        if not required_fields <= set(policy) or not set(policy) <= allowed_fields:
            raise ManifestGenerationError(
                f"policy {policy.get('id')} field set drifted: "
                f"missing={sorted(required_fields - set(policy))}, "
                f"extra={sorted(set(policy) - allowed_fields)}"
            )
        argument_profile = policy.get("argument_profile", "strict")
        if argument_profile not in {"strict", "text_only_message"}:
            raise ManifestGenerationError(f"policy {policy.get('id')} argument profile is invalid")
        if (argument_profile == "text_only_message") != (policy.get("id") in TEXT_MESSAGE_ACTION_IDS):
            raise ManifestGenerationError(f"policy {policy.get('id')} text-message profile drifted")
        exposure = policy.get("exposure")
        denied = exposure in {"denied", "internal"}
        denial_reason = policy.get("denial_reason")
        if denied != bool(isinstance(denial_reason, str) and denial_reason.strip()):
            raise ManifestGenerationError(f"policy {policy.get('id')} denial reason does not match exposure")
        if denied != (policy.get("risk") == "forbidden"):
            raise ManifestGenerationError(f"policy {policy.get('id')} forbidden risk does not match exposure")
        rate_limit = policy.get("rate_limit")
        if not isinstance(rate_limit, Mapping) or set(rate_limit) != {
            "key_fields",
            "limit",
            "window_seconds",
        }:
            raise ManifestGenerationError(f"policy {policy.get('id')} rate-limit field set drifted")


def _validate_wrappers(actions: list[dict[str, Any]], wrappers: list[dict[str, Any]]) -> None:
    action_names: dict[str, set[str]] = {}
    for action in actions:
        action_names.setdefault(str(action["protocol"]), set()).add(str(action["action"]))
    expected_fields = {
        "actions",
        "allowed_scenes",
        "capability",
        "confirmation",
        "effect",
        "injected_params",
        "intent_keywords",
        "permission",
        "protocols",
        "rate_limit",
        "redact_fields",
        "retry",
        "reviewed",
        "risk",
        "scope",
        "tool_name",
    }
    names: list[str] = []
    for wrapper in wrappers:
        name = str(wrapper.get("tool_name") or "")
        names.append(name)
        if set(wrapper) != expected_fields or wrapper.get("reviewed") is not True:
            raise ManifestGenerationError(f"reviewed wrapper field set drifted: {name or '<missing>'}")
        protocols = wrapper.get("protocols")
        actions_for_wrapper = wrapper.get("actions")
        if not isinstance(protocols, list) or not isinstance(actions_for_wrapper, list):
            raise ManifestGenerationError(f"wrapper {name} protocol/action list invalid")
        for protocol in protocols:
            if not isinstance(protocol, str) or protocol not in action_names:
                raise ManifestGenerationError(f"wrapper {name} references unknown protocol {protocol!r}")
            missing = set(actions_for_wrapper) - action_names[protocol]
            if missing:
                raise ManifestGenerationError(f"wrapper {name} references missing {protocol} actions: {sorted(missing)}")
    if len(names) != len(set(names)) or any(not name for name in names):
        raise ManifestGenerationError("reviewed wrapper names are missing or duplicated")


def generate(
    *,
    onebot_v11_root: Path,
    onebot_v12_root: Path,
    adapter_root: Path,
    napcat_openapi: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    locks = _load_json(LOCK_PATH)
    sources = locks.get("sources")
    if not isinstance(sources, dict):
        raise ManifestGenerationError("sources.json lacks sources")
    _verify_checkout(onebot_v11_root, str(sources["onebot_v11"]["commit"]), "OneBot v11")
    _verify_checkout(onebot_v12_root, str(sources["onebot_v12"]["commit"]), "OneBot v12")
    _verify_checkout(adapter_root, str(sources["nonebot_adapter_onebot"]["commit"]), "NoneBot adapter")
    expected_napcat_path = Path("src/api/4.18.19/openapi.json")
    try:
        napcat_root = napcat_openapi.parents[3]
    except IndexError as error:
        raise ManifestGenerationError("NapCat OpenAPI path is not inside a checkout") from error
    if napcat_openapi.relative_to(napcat_root) != expected_napcat_path:
        raise ManifestGenerationError(f"NapCat OpenAPI path drift: expected {expected_napcat_path}")
    _verify_checkout(
        napcat_root,
        str(sources["napcat_docs"]["commit"]),
        "NapCatDocs",
    )
    expected_napcat_sha = str(sources["napcat_docs"]["sha256"])
    actual_napcat_sha = _sha256_bytes(napcat_openapi.read_bytes())
    if actual_napcat_sha != expected_napcat_sha:
        raise ManifestGenerationError(f"NapCat OpenAPI drift: expected {expected_napcat_sha}, got {actual_napcat_sha}")

    v11 = _parse_markdown_actions(
        [onebot_v11_root / "api" / "public.md"],
        "onebot_v11",
        11,
    )
    v12_files = sorted((onebot_v12_root / "specs" / "interface").glob("*/actions.md"))
    v12 = _parse_markdown_actions(v12_files, "onebot_v12", 12)
    napcat = _parse_napcat(napcat_openapi)
    actions = sorted([*v11, *v12, *napcat], key=lambda item: str(item["id"]))
    _validate_actions(actions)
    # The inventory is generated from protocol sources, but authorization is a
    # separately reviewed package artifact.  Never synthesize a permissive
    # policy for a newly discovered action: an action-set change must stop here
    # until a maintainer classifies every new identity in policies.json.
    reviewed_document = _load_json(POLICY_PATH)
    policies_raw = reviewed_document.get("policies")
    wrappers_raw = reviewed_document.get("wrappers")
    if not isinstance(policies_raw, list) or not isinstance(wrappers_raw, list):
        raise ManifestGenerationError("policies.json lacks reviewed policies/wrappers")
    policies = sorted(policies_raw, key=lambda item: str(item.get("id")))
    wrappers = sorted(wrappers_raw, key=lambda item: str(item.get("tool_name")))
    _validate_policies(actions, policies)
    _validate_wrappers(actions, wrappers)

    source_digest = _sha256_json(locks)
    inventory = {
        "schema_version": 1,
        "source_lock_sha256": source_digest,
        "counts": PROTOCOL_COUNTS,
        "actions_sha256": _sha256_json(actions),
        "actions": actions,
    }
    policy_document = {
        "schema_version": 1,
        "reviewed_at": reviewed_document.get("reviewed_at"),
        "review_basis": reviewed_document.get("review_basis"),
        "action_inventory_sha256": inventory["actions_sha256"],
        "policies_sha256": _sha256_json(policies),
        "policies": policies,
        "wrappers_sha256": _sha256_json(wrappers),
        "wrappers": wrappers,
    }
    return inventory, policy_document


def _write_or_check(path: Path, content: str, *, check: bool) -> None:
    if check:
        actual = path.read_text(encoding="utf-8") if path.exists() else ""
        if actual != content:
            raise ManifestGenerationError(f"generated resource is stale: {path.relative_to(ROOT)}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _markdown_cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def _render_action_reference(
    inventory: Mapping[str, Any],
    policy_document: Mapping[str, Any],
) -> str:
    actions = inventory["actions"]
    policies = {str(item["id"]): item for item in policy_document["policies"]}
    lines = [
        "# OneBot / NapCat 动作总表",
        "",
        "<!-- This file is generated by scripts/generate_protocol_manifests.py. -->",
        "",
        "本页列出包内离线清单的全部 244 个动作；“完整收录”不等于“允许模型执行”。",
        "请求参数的完整 JSON Schema 以包内 `protocol_resources/actions.json` 为准，",
        "权限与拒绝原因以 `protocol_resources/policies.json` 为准。",
        "",
        f"- OneBot v11：{inventory['counts']['onebot_v11']} 个公开动作。",
        f"- OneBot v12：{inventory['counts']['onebot_v12']} 个标准动作。",
        f"- NapCat v11 4.18.19：{inventory['counts']['napcat_v11']} 个动作。",
        f"- 动作清单 SHA-256：`{inventory['actions_sha256']}`。",
        f"- 人工策略 SHA-256：`{policy_document['policies_sha256']}`。",
        f"- 安全封装 SHA-256：`{policy_document['wrappers_sha256']}`。",
        "",
        "`denied` / `internal` 动作只保留在管理员可审计总表和包内清单中，",
        "不会生成 LLM Tool Schema；`superuser` 动作也不会向普通用户出现。",
        "",
        "| 协议 | API 动作 | LLM 工具名 | 暴露 | 权限 | 效果 / 风险 | 范围 / 确认 / 参数策略 | 请求字段 | 摘要或拒绝原因 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for action in actions:
        policy = policies[str(action["id"])]
        properties = action["request_schema"].get("properties", {})
        injected = policy["injected_params"]
        fields = []
        for name in sorted(properties):
            marker = "（事件注入）" if name in injected else ""
            fields.append(f"`{name}`{marker}")
        exposure = str(policy["exposure"])
        tool_name = f"`{action['tool_name']}`" if exposure in {"user", "superuser"} else "—"
        summary = policy["denial_reason"] or action["summary"]
        lines.append(
            "| "
            + " | ".join(
                (
                    _markdown_cell(action["protocol"]),
                    f"`{_markdown_cell(action['action'])}`",
                    tool_name,
                    _markdown_cell(exposure),
                    _markdown_cell(policy["permission"]),
                    _markdown_cell(f"{policy['effect']} / {policy['risk']}"),
                    _markdown_cell(f"{policy['scope']} / {policy['confirmation']} / {policy.get('argument_profile', 'strict')}"),
                    ", ".join(fields) or "—",
                    _markdown_cell(summary),
                )
            )
            + " |"
        )
    lines.extend(
        (
            "",
            "## 包内安全封装",
            "",
            "| 工具名 | 固定目标 | 底层动作 | 默认确认 |",
            "| --- | --- | --- | --- |",
        )
    )
    for wrapper in policy_document["wrappers"]:
        lines.append(
            "| "
            + " | ".join(
                (
                    f"`{_markdown_cell(wrapper['tool_name'])}`",
                    _markdown_cell(wrapper["scope"]),
                    ", ".join(f"`{_markdown_cell(item)}`" for item in wrapper["actions"]),
                    _markdown_cell(wrapper["confirmation"]),
                )
            )
            + " |"
        )
    lines.extend(
        (
            "",
            "安全封装的目标 ID 来自当前事件，模型不能改成其他用户、群或消息。",
            "管理员关闭低风险直执开关后，它们也会进入二阶段确认。",
            "",
        )
    )
    return "\n".join(lines)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--onebot-v11-root", type=Path, required=True)
    parser.add_argument("--onebot-v12-root", type=Path, required=True)
    parser.add_argument("--adapter-root", type=Path, required=True)
    parser.add_argument("--napcat-openapi", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        inventory, policies = generate(
            onebot_v11_root=args.onebot_v11_root.resolve(),
            onebot_v12_root=args.onebot_v12_root.resolve(),
            adapter_root=args.adapter_root.resolve(),
            napcat_openapi=args.napcat_openapi.resolve(),
        )
        _write_or_check(INVENTORY_PATH, _canonical_json(inventory), check=args.check)
        _write_or_check(POLICY_PATH, _canonical_json(policies), check=args.check)
        _write_or_check(
            ACTION_DOC_PATH,
            _render_action_reference(inventory, policies),
            check=args.check,
        )
    except (KeyError, OSError, ManifestGenerationError) as error:
        print(  # noqa: T201 - maintainer CLI needs a diagnostic on failure
            f"protocol manifest generation failed: {error}", file=sys.stderr
        )
        return 1
    print(  # noqa: T201 - maintainer CLI reports the verified inventory
        "protocol manifests verified" if args.check else "protocol manifests generated",
        json.dumps(PROTOCOL_COUNTS, sort_keys=True),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

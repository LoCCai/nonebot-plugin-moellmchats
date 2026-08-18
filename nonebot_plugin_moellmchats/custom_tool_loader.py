from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path
import re
from typing import Any

from .generated_tool_runner import generated_tool_runner
from .generated_tools import _validate_top_level
from .tool_contracts import ToolContext, ToolEffect, ToolSpec

_TYPE_NAMES = {
    "str": "string",
    "int": "integer",
    "float": "number",
    "bool": "boolean",
    "list": "array",
    "dict": "object",
}
_FORBIDDEN_CONTEXT = {"_bot", "_event", "_tool_manager"}
_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _annotation(annotation: ast.expr | None) -> tuple[str, str | None]:
    description = None
    value = annotation
    if isinstance(annotation, ast.Subscript):
        root = annotation.value
        root_name = root.id if isinstance(root, ast.Name) else getattr(root, "attr", "")
        if root_name == "Annotated":
            elements = annotation.slice.elts if isinstance(annotation.slice, ast.Tuple) else [annotation.slice]
            value = elements[0] if elements else None
            for item in elements[1:]:
                if isinstance(item, ast.Constant) and isinstance(item.value, str):
                    description = item.value
                    break
    type_name = value.id if isinstance(value, ast.Name) else "str"
    return _TYPE_NAMES.get(type_name, "string"), description


def _function_schema(node: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, Any]:
    arguments = [*node.args.posonlyargs, *node.args.args]
    default_offset = len(arguments) - len(node.args.defaults)
    properties: dict[str, Any] = {}
    required: list[str] = []
    for index, argument in enumerate(arguments):
        if argument.arg in {"self", "cls"} or argument.arg.startswith("_"):
            continue
        json_type, description = _annotation(argument.annotation)
        properties[argument.arg] = {
            "type": json_type,
            "description": description or f"参数 {argument.arg}",
        }
        if index < default_offset:
            required.append(argument.arg)
    for argument, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
        if argument.arg.startswith("_"):
            continue
        json_type, description = _annotation(argument.annotation)
        properties[argument.arg] = {
            "type": json_type,
            "description": description or f"参数 {argument.arg}",
        }
        if default is None:
            required.append(argument.arg)
    return {
        "name": node.name,
        "description": ast.get_docstring(node) or "未提供功能描述",
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
        },
    }


def _assignment(tree: ast.Module, name: str) -> ast.expr | None:
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            return node.value
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
            return node.value
    return None


def _parse_dependencies(tree: ast.Module) -> dict[str, set[str]]:
    value = _assignment(tree, "TOOL_DEPENDENCIES")
    if value is None:
        return {}
    try:
        loaded = ast.literal_eval(value)
    except (TypeError, ValueError) as error:
        raise ValueError("TOOL_DEPENDENCIES 必须是字符串列表映射") from error
    if not isinstance(loaded, dict):
        raise ValueError("TOOL_DEPENDENCIES 必须是对象")
    result: dict[str, set[str]] = {}
    for key, items in loaded.items():
        if not isinstance(key, str) or not isinstance(items, list) or not all(
            isinstance(item, str) and _TOOL_NAME.fullmatch(item) for item in items
        ):
            raise ValueError("TOOL_DEPENDENCIES 必须是字符串列表映射")
        if not _TOOL_NAME.fullmatch(key) or len(set(items)) != len(items):
            raise ValueError("TOOL_DEPENDENCIES 包含非法或重复工具名")
        result[key] = set(items)
    return result


def _literal_field(fields: dict[str, ast.expr], name: str, default: Any = None) -> Any:
    value = fields.get(name)
    if value is None:
        return default
    try:
        return ast.literal_eval(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"TOOLS_REGISTRY.{name} 必须是静态字面量") from error


def _registry_entries(tree: ast.Module) -> list[dict[str, Any]] | None:
    value = _assignment(tree, "TOOLS_REGISTRY")
    if value is None:
        return None
    if not isinstance(value, (ast.List, ast.Tuple)):
        raise ValueError("TOOLS_REGISTRY 必须是数组")
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value.elts:
        if not isinstance(item, ast.Dict):
            raise ValueError("TOOLS_REGISTRY 元素必须是对象")
        fields: dict[str, ast.expr] = {}
        for key, field_value in zip(item.keys, item.values):
            if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                raise ValueError("TOOLS_REGISTRY 字段名必须是字符串")
            if key.value in fields:
                raise ValueError(f"TOOLS_REGISTRY 字段重复: {key.value}")
            fields[key.value] = field_value
        unknown = set(fields) - {
            "name",
            "description",
            "parameters",
            "func",
            "handler",
            "permission",
            "effect",
            "timeout_seconds",
            "result_limit",
            "dependencies",
        }
        if unknown:
            raise ValueError(f"TOOLS_REGISTRY 包含未知字段: {sorted(unknown)}")
        name = _literal_field(fields, "name")
        func_node = fields.get("func") or fields.get("handler")
        if not isinstance(name, str) or not _TOOL_NAME.fullmatch(name):
            raise ValueError("TOOLS_REGISTRY.name 必须是字符串")
        if not isinstance(func_node, ast.Name):
            raise ValueError("TOOLS_REGISTRY.func 必须直接引用本文件函数")
        if name != func_node.id:
            raise ValueError("TOOLS_REGISTRY.name 必须与函数名一致")
        if name in seen:
            raise ValueError(f"TOOLS_REGISTRY 工具重名: {name}")
        seen.add(name)
        description = _literal_field(fields, "description")
        parameters = _literal_field(fields, "parameters")
        if not isinstance(description, str) or not description.strip():
            raise ValueError(f"TOOLS_REGISTRY.{name}.description 不能为空")
        if not isinstance(parameters, dict):
            raise ValueError(f"TOOLS_REGISTRY.{name}.parameters 必须是对象 Schema")
        entries.append(
            {
                "name": name,
                "handler": func_node.id,
                "description": description,
                "parameters": parameters,
                "permission": _literal_field(fields, "permission", "user"),
                "effect": _literal_field(fields, "effect", "read_only"),
                "timeout_seconds": _literal_field(fields, "timeout_seconds"),
                "result_limit": _literal_field(fields, "result_limit"),
                "dependencies": _literal_field(fields, "dependencies", []),
            }
        )
    return entries


def _function_context_parameters(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    return {
        argument.arg
        for argument in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
        if argument.arg.startswith("_")
    }


def _make_handler(source: Path, handler_name: str):
    async def handler(_tool_context: ToolContext | None = None, **kwargs):
        context = {}
        if _tool_context is not None:
            event = _tool_context.event
            context = {
                "request_id": _tool_context.request_id,
                "confirmed": _tool_context.confirmed,
                "user_id": str(getattr(event, "user_id", "")),
                "group_id": str(getattr(event, "group_id", "")),
                "message_type": getattr(event, "message_type", ""),
            }
        return await generated_tool_runner.execute(source, handler_name, kwargs, context)

    return handler


def load_file_tools(files: Iterable[Path]) -> tuple[dict[str, dict[str, Any]], dict[str, set[str]]]:
    tools: dict[str, dict[str, Any]] = {}
    dependencies: dict[str, set[str]] = {}
    for path in sorted(files):
        if path.name.startswith("_") or path.name == "example.py":
            continue
        source = path.read_text(encoding="utf-8")
        if len(source.encode("utf-8")) > 65_536:
            raise ValueError(f"{path.name} 超过 64 KiB")
        try:
            tree = ast.parse(source, filename=path.name)
        except SyntaxError as error:
            raise ValueError(f"自定义工具 {path.name} 语法错误: {error}") from error
        errors = _validate_top_level(
            tree,
            allowed_dynamic_assignments=frozenset({"TOOLS_REGISTRY"}),
        )
        if errors:
            raise ValueError(f"{path.name}: {'; '.join(errors[:3])}")
        functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and not node.name.startswith("_")
        }
        registry_entries = _registry_entries(tree)
        entries = registry_entries if registry_entries is not None else [
            {
                **_function_schema(node),
                "handler": name,
                "permission": "user",
                "effect": "read_only",
                "timeout_seconds": None,
                "result_limit": None,
                "dependencies": [],
            }
            for name, node in functions.items()
        ]
        for entry in entries:
            name = entry["name"]
            node = functions.get(name)
            if node is None:
                raise ValueError(f"{path.name}: handler 不存在: {name}")
            forbidden = _function_context_parameters(node) & _FORBIDDEN_CONTEXT
            if forbidden:
                raise ValueError(
                    f"{path.name}:{name} 请求主进程对象 {sorted(forbidden)}；"
                    "请迁移为可信 NoneBot 插件 register_tool(ToolSpec)"
                )
            if name in tools:
                raise ValueError(f"文件型工具重名: {name}")
            dependencies_value = entry.get("dependencies")
            if not isinstance(dependencies_value, list):
                raise ValueError(f"{path.name}:{name} dependencies 必须是数组")
            try:
                effect = ToolEffect(entry["effect"])
            except (TypeError, ValueError) as error:
                raise ValueError(f"{path.name}:{name} effect 非法") from error
            spec = ToolSpec(
                name=name,
                description=entry["description"],
                parameters=entry["parameters"],
                handler=_make_handler(path.resolve(), name),
                effect=effect,
                permission=entry["permission"],
                timeout_seconds=entry["timeout_seconds"],
                result_limit=entry["result_limit"],
                dependencies=tuple(dependencies_value),
            )
            entry = spec.as_legacy_schema()
            entry["source"] = "custom_file"
            tools[name] = entry
            if spec.dependencies:
                dependencies.setdefault(name, set()).update(spec.dependencies)
        for trigger, items in _parse_dependencies(tree).items():
            dependencies.setdefault(trigger, set()).update(items)
    return tools, dependencies

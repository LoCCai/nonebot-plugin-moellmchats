from __future__ import annotations

import ast
import asyncio
from copy import deepcopy
from dataclasses import dataclass
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
import time
from typing import Any
import uuid

from nonebot.log import logger

from .generated_tool_runner import generated_tool_runner
from .model_selector import config_path
from .tool_contracts import (
    ToolContext,
    ToolEffect,
    ToolSpec,
    validate_parameters_schema,
)

_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:sk|rk)-[A-Za-z0-9_-]{16,}"),
    re.compile(r"(?:postgres(?:ql)?|redis)://[^\s'\"]+", re.IGNORECASE),
)
_RISK_IMPORTS = {"ctypes", "multiprocessing", "os", "pickle", "shutil", "socket", "subprocess"}
_RISK_CALLS = {"eval", "exec", "open", "compile", "__import__"}


@dataclass(frozen=True)
class BundleValidation:
    manifest: dict[str, Any]
    digest: str
    risks: tuple[str, ...]


def _atomic_json(path: Path, value: Any, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(value, file, ensure_ascii=False, indent=2, sort_keys=True)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _literal_assignment(node: ast.Assign | ast.AnnAssign) -> bool:
    value = node.value
    if value is None:
        return True
    try:
        ast.literal_eval(value)
        return True
    except (ValueError, TypeError):
        return False


def _validate_top_level(
    tree: ast.Module,
    *,
    allowed_dynamic_assignments: frozenset[str] = frozenset(),
) -> list[str]:
    errors: list[str] = []
    allowed = (
        ast.Import,
        ast.ImportFrom,
        ast.FunctionDef,
        ast.AsyncFunctionDef,
        ast.ClassDef,
        ast.Pass,
    )
    for node in tree.body:
        if isinstance(node, allowed):
            continue
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and _literal_assignment(node):
            continue
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in allowed_dynamic_assignments
            for target in node.targets
        ):
            continue
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id in allowed_dynamic_assignments
        ):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(
            node.value.value, str
        ):
            continue
        errors.append(f"line {getattr(node, 'lineno', '?')}: 禁止模块顶层执行 {type(node).__name__}")
    return errors


def _scan_risks(tree: ast.Module) -> tuple[str, ...]:
    risks: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root in _RISK_IMPORTS:
                    risks.add(f"导入高权限模块: {root}")
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".", 1)[0]
            if root in _RISK_IMPORTS:
                risks.add(f"导入高权限模块: {root}")
        elif isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else ""
            if name in _RISK_CALLS:
                risks.add(f"调用高风险函数: {name}")
    return tuple(sorted(risks))


class GeneratedToolStore:
    def __init__(self) -> None:
        self._mutation_lock = threading.RLock()
        self.root = Path(config_path / "generated_tools")
        self.drafts_dir = self.root / "drafts"
        self.versions_dir = self.root / "versions"
        self.active_file = self.root / "active.json"

    def _init_files(self) -> None:
        self.drafts_dir.mkdir(parents=True, exist_ok=True)
        self.versions_dir.mkdir(parents=True, exist_ok=True)
        if not self.active_file.exists():
            _atomic_json(self.active_file, {})

    def ensure_initialized(self) -> None:
        with self._mutation_lock:
            self._init_files()

    def watched_paths(self) -> list[Path]:
        paths = [self.active_file]
        active = self.read_active()
        for bundle_id, digest in active.items():
            bundle = self.version_path(bundle_id, digest)
            paths.extend(bundle / name for name in ("manifest.json", "tool.py", "tests.py"))
        return paths

    def read_active(self) -> dict[str, str]:
        try:
            value = json.loads(self.active_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        if not isinstance(value, dict) or not all(
            isinstance(key, str) and isinstance(item, str)
            for key, item in value.items()
        ):
            raise ValueError("generated_tools/active.json 必须是字符串映射")
        return value

    def replace_active(self, active: dict[str, str]) -> None:
        with self._mutation_lock:
            self._init_files()
            for bundle_id, digest in active.items():
                self.version_path(bundle_id, digest)
            _atomic_json(self.active_file, dict(active))

    def version_path(self, bundle_id: str, digest: str) -> Path:
        if not _IDENTIFIER.fullmatch(bundle_id) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("工具包标识或版本哈希非法")
        path = self.versions_dir / bundle_id / digest
        resolved = path.resolve()
        if self.versions_dir.resolve() not in resolved.parents:
            raise ValueError("工具包路径越界")
        return resolved

    def validate_bundle(self, path: Path) -> BundleValidation:
        manifest_path = path / "manifest.json"
        source_path = path / "tool.py"
        tests_path = path / "tests.py"
        for item in (manifest_path, source_path, tests_path):
            if not item.is_file() or item.stat().st_size > 65_536:
                raise ValueError(f"{item.name} 缺失或超过 64 KiB")
        source = source_path.read_text(encoding="utf-8")
        tests_source = tests_path.read_text(encoding="utf-8")
        for pattern in _SECRET_PATTERNS:
            if pattern.search(source) or pattern.search(tests_source):
                raise ValueError("源码疑似包含凭据或私钥字面量")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise ValueError(f"manifest.json 无效: {error}") from error
        if not isinstance(manifest, dict):
            raise ValueError("manifest.json 顶层必须是对象")
        bundle_id = str(manifest.get("bundle_id") or "")
        if not _IDENTIFIER.fullmatch(bundle_id):
            raise ValueError("bundle_id 必须是安全标识符")
        description = manifest.get("description")
        if not isinstance(description, str) or not description.strip():
            raise ValueError("工具包 description 不能为空")
        tools = manifest.get("tools")
        if not isinstance(tools, list) or not tools:
            raise ValueError("工具包 tools 必须是非空数组")
        try:
            tree = ast.parse(source, filename="tool.py")
            tests_tree = ast.parse(tests_source, filename="tests.py")
        except SyntaxError as error:
            raise ValueError(f"Python 语法错误: {error}") from error
        top_errors = _validate_top_level(tree) + _validate_top_level(tests_tree)
        if top_errors:
            raise ValueError("; ".join(top_errors[:5]))
        functions = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        test_functions = {
            node.name
            for node in tests_tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        if "run_tests" not in test_functions:
            raise ValueError("tests.py 必须定义 run_tests(tool_module)")
        names: set[str] = set()
        for tool in tools:
            if not isinstance(tool, dict):
                raise ValueError("tools 数组元素必须是对象")
            name = str(tool.get("name") or "")
            if not _IDENTIFIER.fullmatch(name) or name in names:
                raise ValueError(f"工具名非法或重复: {name}")
            names.add(name)
            if not isinstance(tool.get("description"), str) or not tool["description"].strip():
                raise ValueError(f"工具 {name} description 不能为空")
            validate_parameters_schema(tool.get("parameters"))
            handler = str(tool.get("handler") or "")
            if handler not in functions:
                raise ValueError(f"工具 {name} handler 不存在: {handler}")
            if tool.get("permission") not in {"user", "superuser"}:
                raise ValueError(f"工具 {name} permission 非法")
            if tool.get("effect") not in {"read_only", "mutating"}:
                raise ValueError(f"工具 {name} effect 非法")
            timeout = tool.get("timeout_seconds", 30)
            result_limit = tool.get("result_limit", 6000)
            if not isinstance(timeout, (int, float)) or not 0 < timeout <= 30:
                raise ValueError(f"工具 {name} timeout_seconds 必须在 0 到 30 秒")
            if not isinstance(result_limit, int) or not 0 < result_limit <= 6000:
                raise ValueError(f"工具 {name} result_limit 必须在 1 到 6000")
            dependencies = tool.get("dependencies", [])
            if not isinstance(dependencies, list) or not all(
                isinstance(item, str) and _IDENTIFIER.fullmatch(item)
                for item in dependencies
            ):
                raise ValueError(f"工具 {name} dependencies 必须是安全工具名数组")
            if len(set(dependencies)) != len(dependencies):
                raise ValueError(f"工具 {name} dependencies 不得重复")
        canonical_manifest = json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        digest = hashlib.sha256(
            canonical_manifest + b"\0" + source.encode() + b"\0" + tests_source.encode()
        ).hexdigest()
        risks = tuple(sorted(set(_scan_risks(tree) + _scan_risks(tests_tree))))
        return BundleValidation(manifest=manifest, digest=digest, risks=risks)

    def create_draft(
        self,
        manifest: dict[str, Any],
        source: str,
        tests_source: str,
        *,
        request: str,
        review: dict[str, Any],
        status: str = "reviewed",
    ) -> tuple[str, BundleValidation]:
        self.ensure_initialized()
        draft_id = uuid.uuid4().hex[:12]
        temporary = Path(tempfile.mkdtemp(prefix=".draft-", dir=self.drafts_dir))
        try:
            _atomic_json(temporary / "manifest.json", manifest)
            (temporary / "tool.py").write_text(source, encoding="utf-8")
            (temporary / "tests.py").write_text(tests_source, encoding="utf-8")
            validation = self.validate_bundle(temporary)
            _atomic_json(
                temporary / "metadata.json",
                {
                    "draft_id": draft_id,
                    "request": request,
                    "digest": validation.digest,
                    "risks": list(validation.risks),
                    "review": review,
                    "status": status,
                    "created_at": time.time(),
                },
            )
            for item in temporary.iterdir():
                os.chmod(item, 0o644)
            os.chmod(temporary, 0o755)
            os.replace(temporary, self.drafts_dir / draft_id)
        finally:
            shutil.rmtree(temporary, ignore_errors=True)
        return draft_id, validation

    def get_draft(self, draft_id: str) -> tuple[Path, dict[str, Any], BundleValidation]:
        if not re.fullmatch(r"[a-f0-9]{12}", draft_id):
            raise ValueError("草稿 ID 非法")
        path = self.drafts_dir / draft_id
        metadata = json.loads((path / "metadata.json").read_text(encoding="utf-8"))
        validation = self.validate_bundle(path)
        if metadata.get("digest") != validation.digest:
            raise ValueError("草稿内容已变化，哈希校验失败")
        return path, metadata, validation

    def approve(self, draft_id: str, short_hash: str) -> tuple[str, str]:
        with self._mutation_lock:
            path, metadata, validation = self.get_draft(draft_id)
            if metadata.get("status") != "reviewed":
                raise ValueError("草稿当前状态不可批准")
            if len(short_hash) < 8 or not validation.digest.startswith(short_hash.lower()):
                raise ValueError("确认哈希不匹配")
            bundle_id = validation.manifest["bundle_id"]
            version = self.version_path(bundle_id, validation.digest)
            version.parent.mkdir(parents=True, exist_ok=True)
            if not version.exists():
                temporary = version.parent / f".{validation.digest}.{uuid.uuid4().hex}.tmp"
                try:
                    shutil.copytree(path, temporary)
                    for item in temporary.rglob("*"):
                        os.chmod(item, 0o555 if item.is_dir() else 0o444)
                    os.chmod(temporary, 0o555)
                    os.replace(temporary, version)
                finally:
                    shutil.rmtree(temporary, ignore_errors=True)
            stored = self.validate_bundle(version)
            if stored.digest != validation.digest:
                raise ValueError("批准版本哈希校验失败")
            previous_active = self.read_active()
            active = dict(previous_active)
            active[bundle_id] = validation.digest
            try:
                _atomic_json(self.active_file, active)
                metadata["status"] = "approved"
                metadata["approved_at"] = time.time()
                _atomic_json(path / "metadata.json", metadata)
            except Exception:
                _atomic_json(self.active_file, previous_active)
                raise
            return bundle_id, validation.digest

    def restore_draft_metadata(self, draft_id: str, metadata: dict[str, Any]) -> None:
        with self._mutation_lock:
            path, _, validation = self.get_draft(draft_id)
            restored = deepcopy(metadata)
            if restored.get("digest") != validation.digest:
                raise ValueError("不能恢复不匹配的草稿元数据")
            _atomic_json(path / "metadata.json", restored)

    def draft_diff(self, draft_id: str, *, limit: int = 4000) -> str:
        path, _, validation = self.get_draft(draft_id)
        bundle_id = validation.manifest["bundle_id"]
        new_source = (path / "tool.py").read_text(encoding="utf-8").splitlines()
        active_digest = self.read_active().get(bundle_id)
        old_source: list[str] = []
        old_label = "/dev/null"
        if active_digest:
            active_path = self.version_path(bundle_id, active_digest) / "tool.py"
            old_source = active_path.read_text(encoding="utf-8").splitlines()
            old_label = f"{bundle_id}@{active_digest[:12]}/tool.py"
        diff = "\n".join(
            difflib.unified_diff(
                old_source,
                new_source,
                fromfile=old_label,
                tofile=f"draft:{draft_id}/tool.py",
                lineterm="",
            )
        )
        if len(diff) > limit:
            return diff[:limit] + "\n...[diff 已截断]"
        return diff or "源码与当前激活版本一致"

    def reject(self, draft_id: str) -> None:
        with self._mutation_lock:
            path, metadata, _ = self.get_draft(draft_id)
            metadata["status"] = "rejected"
            metadata["rejected_at"] = time.time()
            _atomic_json(path / "metadata.json", metadata)

    def deactivate(self, bundle_id: str) -> bool:
        with self._mutation_lock:
            active = self.read_active()
            removed = active.pop(bundle_id, None)
            if removed is None:
                return False
            _atomic_json(self.active_file, active)
            return True

    def rollback(self, bundle_id: str, digest_or_prefix: str) -> str:
        with self._mutation_lock:
            directory = self.versions_dir / bundle_id
            matches = [
                item.name
                for item in directory.iterdir()
                if item.is_dir() and item.name.startswith(digest_or_prefix.lower())
            ] if directory.is_dir() else []
            if len(matches) != 1:
                raise ValueError("回滚版本不存在或前缀不唯一")
            validation = self.validate_bundle(directory / matches[0])
            if validation.digest != matches[0]:
                raise ValueError("回滚版本内容哈希不匹配")
            active = self.read_active()
            active[bundle_id] = matches[0]
            _atomic_json(self.active_file, active)
            return matches[0]

    def list_status(self) -> dict[str, Any]:
        drafts = []
        paths = sorted(self.drafts_dir.iterdir()) if self.drafts_dir.is_dir() else []
        for path in paths:
            try:
                metadata = json.loads((path / "metadata.json").read_text(encoding="utf-8"))
                drafts.append(
                    {key: metadata.get(key) for key in ("draft_id", "digest", "status", "request")}
                )
            except (OSError, ValueError, TypeError):
                continue
        return {"active": self.read_active(), "drafts": drafts}

    def load_active_tools(self) -> tuple[dict[str, dict[str, Any]], dict[str, set[str]]]:
        schemas: dict[str, dict[str, Any]] = {}
        names: set[str] = set()
        dependencies: dict[str, set[str]] = {}
        for bundle_id, digest in self.read_active().items():
            path = self.version_path(bundle_id, digest)
            validation = self.validate_bundle(path)
            if validation.digest != digest:
                raise ValueError(f"工具包 {bundle_id} 内容哈希不匹配")
            for item in validation.manifest["tools"]:
                name = item["name"]
                if name in names:
                    raise ValueError(f"生成工具重名: {name}")
                names.add(name)
                handler_name = item["handler"]

                async def handler(
                    _tool_context: ToolContext | None = None,
                    _bundle=path,
                    _handler=handler_name,
                    _tool_name=name,
                    _bundle_digest=digest,
                    **kwargs,
                ):
                    context = {}
                    caller_permission = "user"
                    if _tool_context is not None:
                        event = _tool_context.event
                        superusers = {
                            str(user_id)
                            for user_id in getattr(
                                getattr(_tool_context.bot, "config", None),
                                "superusers",
                                set(),
                            )
                        }
                        if str(getattr(event, "user_id", "")) in superusers:
                            caller_permission = "superuser"
                        context = {
                            "request_id": _tool_context.request_id,
                            "confirmed": _tool_context.confirmed,
                            "user_id": str(getattr(event, "user_id", "")),
                            "group_id": str(getattr(event, "group_id", "")),
                            "message_type": getattr(event, "message_type", ""),
                        }
                    started = time.monotonic()
                    result_kind = "failure"
                    try:
                        result = await generated_tool_runner.execute(
                            _bundle, _handler, kwargs, context
                        )
                        result_kind = "success"
                        return result
                    except asyncio.CancelledError:
                        result_kind = "cancelled"
                        raise
                    finally:
                        logger.info(
                            f"generated_tool_audit tool={_tool_name} "
                            f"version={_bundle_digest[:12]} "
                            f"caller_permission={caller_permission} "
                            f"elapsed={time.monotonic() - started:.3f}s "
                            f"result={result_kind}"
                        )

                spec = ToolSpec(
                    name=name,
                    description=item["description"],
                    parameters=deepcopy(item["parameters"]),
                    handler=handler,
                    effect=ToolEffect(item["effect"]),
                    permission=item["permission"],
                    timeout_seconds=float(item.get("timeout_seconds", 30)),
                    result_limit=int(item.get("result_limit", 6000)),
                    dependencies=tuple(item.get("dependencies") or ()),
                )
                schema = spec.as_legacy_schema()
                schema["source"] = "generated"
                schema["bundle_id"] = bundle_id
                schema["bundle_digest"] = digest
                schemas[name] = schema
                if spec.dependencies:
                    dependencies[name] = set(spec.dependencies)
        return schemas, dependencies


generated_tool_store = GeneratedToolStore()
generated_tool_lifecycle_lock = asyncio.Lock()

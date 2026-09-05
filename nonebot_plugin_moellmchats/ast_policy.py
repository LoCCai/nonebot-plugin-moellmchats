from __future__ import annotations

import ast
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
import os
from types import MappingProxyType
from typing import Any

from .tool_contracts import ToolCapability, ToolEffect, ToolPolicy


class PolicyDecision(str, Enum):
    """Structured outcome emitted by the static preflight policy engine."""

    ALLOW = "ALLOW"
    DENY = "DENY"
    CAPABILITY_REQUIRED = "CAPABILITY_REQUIRED"
    RISK = "RISK"


@dataclass(frozen=True)
class PolicyFinding:
    decision: PolicyDecision
    code: str
    message: str
    line: int | None = None
    capability: str | None = None
    scope: str | None = None


def _decision(findings: Iterable[PolicyFinding]) -> PolicyDecision:
    decisions = {item.decision for item in findings}
    if PolicyDecision.DENY in decisions:
        return PolicyDecision.DENY
    if PolicyDecision.CAPABILITY_REQUIRED in decisions:
        return PolicyDecision.CAPABILITY_REQUIRED
    if PolicyDecision.RISK in decisions:
        return PolicyDecision.RISK
    return PolicyDecision.ALLOW


def _allowed(findings: Iterable[PolicyFinding]) -> bool:
    return not any(item.decision in {PolicyDecision.DENY, PolicyDecision.CAPABILITY_REQUIRED} for item in findings)


def _blocking_findings(
    findings: Iterable[PolicyFinding],
) -> tuple[PolicyFinding, ...]:
    return tuple(item for item in findings if item.decision in {PolicyDecision.DENY, PolicyDecision.CAPABILITY_REQUIRED})


def _effective_effect(
    declared: ToolEffect,
    detected: ToolEffect,
) -> ToolEffect:
    if declared is ToolEffect.MUTATING or detected is ToolEffect.MUTATING:
        return ToolEffect.MUTATING
    return ToolEffect.READ_ONLY


@dataclass(frozen=True)
class HandlerPolicyReport:
    """Preflight result for one handler and its reachable local helpers."""

    handler: str
    findings: tuple[PolicyFinding, ...]
    detected_effect: ToolEffect
    direct_calls: tuple[str, ...] = ()
    reachable_functions: tuple[str, ...] = ()

    @property
    def allowed(self) -> bool:
        return _allowed(self.findings)

    @property
    def blocking_findings(self) -> tuple[PolicyFinding, ...]:
        return _blocking_findings(self.findings)

    @property
    def decision(self) -> PolicyDecision:
        return _decision(self.findings)

    def effective_effect(self, declared: ToolEffect) -> ToolEffect:
        return _effective_effect(declared, self.detected_effect)

    @property
    def detected_capabilities(self) -> ToolCapability:
        """Return coarse static evidence, never an authorization decision."""

        names = {
            item.capability
            for item in self.findings
            if item.capability is not None
        }
        return ToolCapability(
            network="network" in names,
            process="process" in names,
            workspace="workspace" in names,
            host_filesystem="host_filesystem" in names,
            secrets="secrets" in names,
        )

    def messages(self) -> tuple[str, ...]:
        return tuple(_format_finding(item) for item in self.findings)


@dataclass(frozen=True)
class AstPolicyReport:
    """Conservative AST preflight evidence, not an OS sandbox or seccomp layer."""

    module_findings: tuple[PolicyFinding, ...]
    handler_reports: Mapping[str, HandlerPolicyReport]
    call_graph: Mapping[str, tuple[str, ...]]
    module_effect: ToolEffect = ToolEffect.READ_ONLY

    @property
    def handlers(self) -> Mapping[str, HandlerPolicyReport]:
        return self.handler_reports

    def for_handler(self, name: str) -> HandlerPolicyReport:
        try:
            return self.handler_reports[name]
        except KeyError:
            raise KeyError(f"AST policy 未分析 handler: {name}") from None

    @property
    def findings(self) -> tuple[PolicyFinding, ...]:
        if not self.handler_reports:
            return self.module_findings
        combined: list[PolicyFinding] = []
        seen: set[PolicyFinding] = set()
        for report in self.handler_reports.values():
            for item in report.findings:
                if item not in seen:
                    seen.add(item)
                    combined.append(item)
        return _sort_findings(combined)

    @property
    def detected_effect(self) -> ToolEffect:
        if self.module_effect is ToolEffect.MUTATING or any(
            report.detected_effect is ToolEffect.MUTATING for report in self.handler_reports.values()
        ):
            return ToolEffect.MUTATING
        return ToolEffect.READ_ONLY

    @property
    def allowed(self) -> bool:
        if self.handler_reports:
            return all(report.allowed for report in self.handler_reports.values())
        return _allowed(self.module_findings)

    @property
    def blocking_findings(self) -> tuple[PolicyFinding, ...]:
        return _blocking_findings(self.findings)

    @property
    def decision(self) -> PolicyDecision:
        return _decision(self.findings)

    def effective_effect(self, declared: ToolEffect) -> ToolEffect:
        return _effective_effect(declared, self.detected_effect)

    @property
    def detected_capabilities(self) -> ToolCapability:
        detected = ToolCapability.none()
        if self.handler_reports:
            for report in self.handler_reports.values():
                detected = detected.union(report.detected_capabilities)
            return detected
        names = {
            item.capability
            for item in self.module_findings
            if item.capability is not None
        }
        return ToolCapability(
            network="network" in names,
            process="process" in names,
            workspace="workspace" in names,
            host_filesystem="host_filesystem" in names,
            secrets="secrets" in names,
        )

    def messages(self) -> tuple[str, ...]:
        return tuple(_format_finding(item) for item in self.findings)


def _format_finding(item: PolicyFinding) -> str:
    location = item.scope or ""
    if item.line is not None:
        location = f"{location}:line {item.line}" if location else f"line {item.line}"
    return f"{location}: {item.message}" if location else item.message


def _sort_findings(findings: Iterable[PolicyFinding]) -> tuple[PolicyFinding, ...]:
    return tuple(
        sorted(
            findings,
            key=lambda item: (
                item.scope or "",
                item.line if item.line is not None else -1,
                item.decision.value,
                item.code,
                item.capability or "",
            ),
        )
    )


@dataclass(frozen=True)
class _FindingTemplate:
    decision: PolicyDecision
    code: str
    message: str
    line: int | None
    capability: str | None
    scope: str


@dataclass
class _ScopeAnalysis:
    findings: list[_FindingTemplate]
    detected_effect: ToolEffect
    direct_calls: set[str]


_PROCESS_MODULES = {"multiprocessing", "pty", "subprocess"}
_NETWORK_MODULES = {
    "aiohttp",
    "ftplib",
    "http",
    "httpx",
    "requests",
    "smtplib",
    "socket",
    "urllib",
    "websockets",
}
# safe_public_get 与 safe_request 同为安全 HTTP 门面：自定义文件工具
# import 任一都必须触发 network capability 检查，不能借公开门面静默绕过
_SAFE_HTTP_CALLS = {"safe_request", "safe_public_get"}
_GENERATED_DENIED_MODULES = {"builtins", "ctypes", "pickle", "posix"}
_GENERATED_DENIED_BUILTINS = {
    "__import__",
    "compile",
    "delattr",
    "eval",
    "exec",
    "getattr",
    "globals",
    "locals",
    "setattr",
    "vars",
}
_GENERATED_DENIED_ATTRIBUTES = {
    "__bases__",
    "__builtins__",
    "__class__",
    "__closure__",
    "__code__",
    "__dict__",
    "__getattribute__",
    "__globals__",
    "__mro__",
    "__self__",
    "__subclasses__",
    "__func__",
}
_GENERATED_DENIED_SUBSCRIPT_KEYS = {
    "__builtins__",
    "__import__",
    "compile",
    "eval",
    "exec",
}
_DYNAMIC_IMPORT_CALLS = {
    "importlib.__import__",
    "importlib.import_module",
    "importlib.reload",
    "pkgutil.resolve_name",
    "runpy.run_module",
    "runpy.run_path",
}
_PROCESS_CALL_PREFIXES = (
    "multiprocessing.",
    "pty.",
    "subprocess.",
)
_PROCESS_CALLS = {
    "asyncio.create_subprocess_exec",
    "asyncio.create_subprocess_shell",
    "concurrent.futures.ProcessPoolExecutor",
    "concurrent.futures.process.ProcessPoolExecutor",
    "os.execl",
    "os.execle",
    "os.execlp",
    "os.execlpe",
    "os.execv",
    "os.execve",
    "os.execvp",
    "os.execvpe",
    "os.fork",
    "os.forkpty",
    "os.popen",
    "os.posix_spawn",
    "os.posix_spawnp",
    "os.spawnl",
    "os.spawnle",
    "os.spawnlp",
    "os.spawnlpe",
    "os.spawnv",
    "os.spawnve",
    "os.spawnvp",
    "os.spawnvpe",
    "os.system",
    "pty.fork",
    "pty.spawn",
}
_PROCESS_METHOD_NAMES = {
    "create_subprocess_exec",
    "create_subprocess_shell",
    "subprocess_exec",
    "subprocess_shell",
}
_PROCESS_NAMESPACE_MODULES = {
    "asyncio",
    "multiprocessing",
    "os",
    "subprocess",
}
_HTTP_READ_ONLY_METHODS = {"GET", "HEAD"}
_HTTP_READ_ONLY_CONSTANTS = {
    "aiohttp.hdrs.METH_GET",
    "aiohttp.hdrs.METH_HEAD",
    "http.HTTPMethod.GET",
    "http.HTTPMethod.GET.value",
    "http.HTTPMethod.HEAD",
    "http.HTTPMethod.HEAD.value",
}
_HTTP_UNBOUND_REQUEST_METHODS = {
    "aiohttp.ClientSession.request",
    "http.client.HTTPConnection.request",
    "http.client.HTTPSConnection.request",
    "httpx.AsyncClient.request",
    "httpx.Client.request",
    "requests.Session.request",
    "requests.sessions.Session.request",
    "urllib3.PoolManager.request",
}
_UNBOUND_ATTRIBUTE_ACCESSORS = {
    "object.__getattribute__",
    "type.__getattribute__",
}
_MUTATING_CALLS = {
    "os.chmod",
    "os.chown",
    "os.link",
    "os.makedirs",
    "os.mkdir",
    "os.remove",
    "os.rename",
    "os.renames",
    "os.replace",
    "os.rmdir",
    "os.symlink",
    "os.truncate",
    "os.unlink",
    "pathlib.Path.chmod",
    "pathlib.Path.mkdir",
    "pathlib.Path.rename",
    "pathlib.Path.replace",
    "pathlib.Path.rmdir",
    "pathlib.Path.symlink_to",
    "pathlib.Path.touch",
    "pathlib.Path.unlink",
    "pathlib.Path.write_bytes",
    "pathlib.Path.write_text",
    "shutil.copy",
    "shutil.copy2",
    "shutil.copyfile",
    "shutil.copytree",
    "shutil.move",
    "shutil.rmtree",
}
_MUTATING_METHOD_NAMES = {
    "add",
    "add_all",
    "bulk_create",
    "bulk_insert_mappings",
    "bulk_save_objects",
    "bulk_update",
    "bulk_update_mappings",
    "callproc",
    "clear",
    "chmod",
    "chown",
    "commit",
    "create",
    "delete",
    "drop",
    "executemany",
    "execute",
    "flush",
    "get_or_create",
    "insert",
    "merge",
    "mkdir",
    "patch",
    "persist",
    "post",
    "put",
    "remove",
    "rename",
    "replace",
    "rmdir",
    "save",
    "save_all",
    "save_changes",
    "send",
    "set",
    "touch",
    "truncate",
    "unlink",
    "update",
    "update_or_create",
    "upsert",
    "write",
    "write_bytes",
    "write_text",
    "writelines",
}
_HTTP_URL_OPEN_CALLS = {
    "urllib.request.urlopen",
    "urllib3.PoolManager.urlopen",
}
_OS_OPEN_WRITE_FLAGS = {
    "O_APPEND",
    "O_CREAT",
    "O_RDWR",
    "O_TMPFILE",
    "O_TRUNC",
    "O_WRONLY",
}
_OS_OPEN_READ_FLAGS = {
    "O_BINARY",
    "O_CLOEXEC",
    "O_DIRECT",
    "O_DIRECTORY",
    "O_DSYNC",
    "O_EXCL",
    "O_NDELAY",
    "O_NOINHERIT",
    "O_NOATIME",
    "O_NOCTTY",
    "O_NOFOLLOW",
    "O_NONBLOCK",
    "O_PATH",
    "O_RANDOM",
    "O_RDONLY",
    "O_RSYNC",
    "O_SEQUENTIAL",
    "O_SHORT_LIVED",
    "O_SYNC",
    "O_TEXT",
}
_MAX_CALL_GRAPH_DEPTH = 32
_MAX_CALL_GRAPH_FUNCTIONS = 256


def _literal(value: ast.AST | None) -> tuple[bool, Any]:
    if value is None:
        return True, None
    if any(isinstance(item, ast.Call) for item in ast.walk(value)):
        return False, None
    try:
        return True, ast.literal_eval(value)
    except (TypeError, ValueError):
        return False, None


def _root_module(name: str) -> str:
    return name.split(".", 1)[0]


def _syntactic_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _syntactic_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    if isinstance(node, ast.NamedExpr):
        return _syntactic_name(node.value)
    return ""


def _is_process_name(name: str) -> bool:
    method = name.rsplit(".", 1)[-1]
    return (
        name in _PROCESS_CALLS
        or method in _PROCESS_METHOD_NAMES
        or any(name.startswith(prefix) for prefix in _PROCESS_CALL_PREFIXES)
    )


def _is_process_namespace_access(name: str) -> bool:
    return any(name == f"{module}.__dict__" or name.startswith(f"{module}.__dict__.") for module in _PROCESS_NAMESPACE_MODULES)


def _materialize(
    findings: Iterable[_FindingTemplate],
    policy: ToolPolicy,
) -> tuple[PolicyFinding, ...]:
    materialized: list[PolicyFinding] = []
    seen: set[tuple[str, int | None, str | None, str]] = set()
    for item in findings:
        decision = item.decision
        if decision is PolicyDecision.CAPABILITY_REQUIRED and item.capability:
            if bool(getattr(policy.effective, item.capability)):
                decision = PolicyDecision.RISK
        key = (item.code, item.line, item.capability, item.scope)
        if key in seen:
            continue
        seen.add(key)
        materialized.append(
            PolicyFinding(
                decision=decision,
                code=item.code,
                message=item.message,
                line=item.line,
                capability=item.capability,
                scope=item.scope,
            )
        )
    return _sort_findings(materialized)


class _AliasCallCollector(ast.NodeVisitor):
    """Collect call sites without crossing into a nested Python scope."""

    def __init__(self) -> None:
        self.calls: dict[str, list[ast.Call]] = {}

    def visit_Call(self, node: ast.Call) -> None:
        name = _syntactic_name(node.func)
        if name:
            self.calls.setdefault(name, []).append(node)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return


def _collect_alias_calls(statements: Iterable[ast.stmt]) -> Mapping[str, tuple[ast.Call, ...]]:
    collector = _AliasCallCollector()
    for statement in statements:
        collector.visit(statement)
    return MappingProxyType({name: tuple(calls) for name, calls in collector.calls.items()})


class _ScopeVisitor(ast.NodeVisitor):
    def __init__(
        self,
        *,
        source_type: str,
        scope: str,
        known_functions: set[str],
        aliases: Mapping[str, str] | None = None,
        alias_calls: Mapping[str, tuple[ast.Call, ...]] | None = None,
        scan_nested_function_bodies: bool,
    ) -> None:
        self.source_type = source_type
        self.scope = scope
        self.known_functions = known_functions
        self.aliases = dict(aliases or {})
        self.alias_calls = dict(alias_calls or {})
        self.scan_nested_function_bodies = scan_nested_function_bodies
        self.findings: list[_FindingTemplate] = []
        self.detected_effect = ToolEffect.READ_ONLY
        self.direct_calls: set[str] = set()
        self._seen: set[tuple[str, int | None, str | None, str]] = set()

    @property
    def generated(self) -> bool:
        return self.source_type == "generated"

    @property
    def custom_file(self) -> bool:
        return self.source_type == "custom_file"

    def result(self) -> _ScopeAnalysis:
        return _ScopeAnalysis(
            findings=self.findings,
            detected_effect=self.detected_effect,
            direct_calls=self.direct_calls,
        )

    def _add(
        self,
        decision: PolicyDecision,
        code: str,
        message: str,
        node: ast.AST,
        *,
        capability: str | None = None,
    ) -> None:
        line = getattr(node, "lineno", None)
        key = (code, line, capability, self.scope)
        if key in self._seen:
            return
        self._seen.add(key)
        self.findings.append(
            _FindingTemplate(
                decision=decision,
                code=code,
                message=message,
                line=line,
                capability=capability,
                scope=self.scope,
            )
        )

    def _capability(
        self,
        capability: str,
        code: str,
        message: str,
        node: ast.AST,
    ) -> None:
        decision = PolicyDecision.CAPABILITY_REQUIRED
        if self.generated and capability == "process":
            decision = PolicyDecision.DENY
        self._add(
            decision,
            code,
            message,
            node,
            capability=capability,
        )

    def _mark_mutating(self, message: str, node: ast.AST) -> None:
        self.detected_effect = ToolEffect.MUTATING
        self._add(
            PolicyDecision.RISK,
            "effect.mutating",
            message,
            node,
        )

    def _call_name(self, node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return self.aliases.get(node.id, node.id)
        if isinstance(node, ast.NamedExpr):
            return self._call_name(node.value)
        if isinstance(node, ast.Attribute):
            raw = _syntactic_name(node)
            if raw in self.aliases:
                return self.aliases[raw]
            parent = self._call_name(node.value)
            candidate = f"{parent}.{node.attr}" if parent else node.attr
            return self.aliases.get(candidate, candidate)
        if isinstance(node, ast.Subscript):
            container = self._call_name(node.value)
            literal, key = _literal(node.slice)
            if container.endswith(".__dict__") and literal and isinstance(key, str) and key.isidentifier():
                owner = container.removesuffix(".__dict__")
                return f"{owner}.{key}" if owner else ""
            if (
                container in {"__builtins__", "builtins"}
                and literal
                and isinstance(key, str)
                and key.isidentifier()
            ):
                return f"builtins.{key}"
            if (
                container == "sys.modules"
                and literal
                and isinstance(key, str)
                and key.isidentifier()
            ):
                return key
        if isinstance(node, ast.Call):
            callable_name = self._call_name(node.func)
            normalized = callable_name.removeprefix("builtins.")
            if normalized == "vars" and len(node.args) == 1:
                owner = self._call_name(node.args[0])
                return f"{owner}.__dict__" if owner else ""
            if normalized == "getattr" and len(node.args) >= 2:
                owner = self._call_name(node.args[0])
                literal, attribute = _literal(node.args[1])
                if owner and literal and isinstance(attribute, str) and attribute.isidentifier():
                    return f"{owner}.{attribute}"
            if callable_name in _UNBOUND_ATTRIBUTE_ACCESSORS and len(node.args) >= 2:
                owner = self._call_name(node.args[0])
                literal, attribute = _literal(node.args[1])
                if owner and literal and isinstance(attribute, str) and attribute.isidentifier():
                    return f"{owner}.{attribute}"
            if callable_name.endswith(".__getattribute__") and callable_name not in _UNBOUND_ATTRIBUTE_ACCESSORS and node.args:
                owner = callable_name.removesuffix(".__getattribute__")
                literal, attribute = _literal(node.args[0])
                if owner and literal and isinstance(attribute, str) and attribute.isidentifier():
                    return f"{owner}.{attribute}"
            for accessor in (".get", ".__getitem__"):
                if callable_name.endswith(f".__dict__{accessor}") and node.args:
                    literal, key = _literal(node.args[0])
                    if literal and isinstance(key, str) and key.isidentifier():
                        container = callable_name.removesuffix(accessor)
                        owner = container.removesuffix(".__dict__")
                        return f"{owner}.{key}" if owner else ""
        return ""

    def _inspect_process_reference(
        self,
        node: ast.AST,
        *,
        code: str = "process.reference",
    ) -> None:
        name = self._call_name(node)
        if _is_process_name(name):
            self._capability(
                "process",
                code,
                f"引用进程执行原语 {name}",
                node,
            )
        elif _is_process_namespace_access(name):
            self._capability(
                "process",
                "process.dynamic_namespace",
                f"动态访问可暴露进程执行原语的模块命名空间 {name}",
                node,
            )

    def _bind_alias(self, target: ast.AST, value: ast.AST) -> None:
        target_name = _syntactic_name(target)
        if not target_name:
            return
        value_name = self._call_name(value)
        if value_name and isinstance(
            value,
            ast.Name | ast.Attribute | ast.Subscript | ast.Call,
        ):
            self.aliases[target_name] = value_name
            # A later branch may overwrite this alias before the ordinary
            # visitor reaches its call. Analyze every statically possible
            # binding at the call site so helper effects and HTTP writes cannot
            # disappear merely because the safe branch is visited last.
            for call in self.alias_calls.get(target_name, ()):
                self._inspect_named_call(call, value_name)
        else:
            self.aliases.pop(target_name, None)

    def _inspect_assignment_target(self, target: ast.AST, value: ast.AST) -> None:
        target_name = self._call_name(target)
        value_name = self._call_name(value)
        if _is_process_name(target_name):
            self._capability(
                "process",
                "process.attribute_assignment",
                f"修改进程执行原语属性 {target_name}",
                target,
            )
            self._mark_mutating("检测到进程执行属性赋值", target)
        if _is_process_name(value_name):
            self._capability(
                "process",
                "process.alias",
                f"为进程执行原语 {value_name} 创建别名",
                target,
            )
        if isinstance(target, ast.Attribute | ast.Subscript):
            self._mark_mutating("检测到属性或下标赋值", target)
        self._bind_alias(target, value)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.asname:
                self.aliases[alias.asname] = alias.name
            else:
                # ``import os.path`` binds ``os``, not ``os.path``. Mapping the
                # root name to the dotted import would turn ``os.system`` into
                # the fictitious ``os.path.system`` and hide a process call.
                root = alias.name.split(".", 1)[0]
                self.aliases[root] = root
            self._inspect_import(alias.name, node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        self._inspect_import(module, node)
        for alias in node.names:
            if alias.name == "*":
                if self.generated:
                    self._add(
                        PolicyDecision.DENY,
                        "import.star",
                        "Generated Tool 禁止星号导入",
                        node,
                    )
                continue
            canonical = f"{module}.{alias.name}" if module else alias.name
            self.aliases[alias.asname or alias.name] = canonical
            if _is_process_name(canonical):
                self._capability(
                    "process",
                    "process.reference",
                    f"导入进程执行原语 {canonical}",
                    node,
                )
            elif _is_process_namespace_access(canonical):
                self._capability(
                    "process",
                    "process.dynamic_namespace",
                    f"导入可暴露进程执行原语的模块命名空间 {canonical}",
                    node,
                )

    def _inspect_import(self, name: str, node: ast.AST) -> None:
        root = _root_module(name)
        if root in _PROCESS_MODULES:
            self._capability(
                "process",
                "process.import",
                f"导入进程模块 {root}",
                node,
            )
        elif root in _NETWORK_MODULES:
            if self.custom_file:
                self._add(
                    PolicyDecision.DENY,
                    "network.raw_client",
                    f"Custom File 禁止直接导入网络客户端 {root}；请使用 safe_request",
                    node,
                    capability="network",
                )
            else:
                self._capability(
                    "network",
                    "capability.network",
                    f"导入 {root} 需要 network capability",
                    node,
                )
        elif self.generated and root in _GENERATED_DENIED_MODULES:
            self._add(
                PolicyDecision.DENY,
                "import.unsafe",
                f"Generated Tool 禁止导入高风险模块 {root}",
                node,
            )
        elif root in {"os", "pathlib", "shutil"}:
            self._add(
                PolicyDecision.RISK,
                "filesystem.import",
                f"导入文件系统相关模块 {root}",
                node,
            )

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._add(
            PolicyDecision.DENY if self.generated else PolicyDecision.RISK,
            "syntax.class",
            ("Generated Tool 第一阶段禁止定义类" if self.generated else "类定义及其类体会在所在作用域加载时执行"),
            node,
        )
        for item in [*node.decorator_list, *node.bases]:
            self.visit(item)
        for keyword in node.keywords:
            self.visit(keyword.value)
        for statement in node.body:
            if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef):
                self._visit_function_definition(statement, scan_body=False)
            else:
                self.visit(statement)
        self.aliases[node.name] = node.name

    def _visit_function_definition(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        *,
        scan_body: bool,
    ) -> None:
        if node.decorator_list:
            self._add(
                PolicyDecision.DENY if self.generated else PolicyDecision.RISK,
                "syntax.decorator",
                ("Generated Tool 禁止函数装饰器" if self.generated else "函数装饰器会在函数定义时执行"),
                node,
            )
        defaults: list[ast.AST | None] = [
            *node.args.defaults,
            *node.args.kw_defaults,
        ]
        if any(not _literal(item)[0] for item in defaults):
            self._add(
                PolicyDecision.DENY if self.generated else PolicyDecision.RISK,
                "syntax.dynamic_default",
                ("Generated Tool 禁止非字面量默认参数" if self.generated else "非字面量默认参数会在函数定义时执行"),
                node,
            )
        for expression in [*node.decorator_list, *defaults]:
            if expression is not None:
                self.visit(expression)
        if scan_body:
            for statement in node.body:
                self.visit(statement)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function_definition(
            node,
            scan_body=self.scan_nested_function_bodies,
        )
        self.aliases[node.name] = node.name

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function_definition(
            node,
            scan_body=self.scan_nested_function_bodies,
        )
        self.aliases[node.name] = node.name

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        for target in node.targets:
            self._inspect_assignment_target(target, node.value)
            self.visit(target)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self.visit(node.value)
            self._inspect_assignment_target(node.target, node.value)
        self.visit(node.target)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        # Walrus bindings are real aliases at the surrounding scope.  Bind
        # them before subsequent expressions so ``(f := os.system)(...)`` and
        # the equivalent network/mutating forms cannot evade policy evidence.
        self.visit(node.value)
        self._inspect_assignment_target(node.target, node.value)
        self.visit(node.target)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self.visit(node.value)
        self.visit(node.target)
        if isinstance(node.target, ast.Attribute | ast.Subscript):
            self._mark_mutating("检测到属性或下标增量赋值", node.target)

    def visit_Delete(self, node: ast.Delete) -> None:
        for target in node.targets:
            self.visit(target)
            if isinstance(target, ast.Attribute | ast.Subscript):
                self._mark_mutating("检测到属性或下标删除", target)

    @staticmethod
    def _mode_node(node: ast.Call, positional_index: int) -> ast.AST | None:
        mode_node = node.args[positional_index] if len(node.args) > positional_index else None
        for keyword in node.keywords:
            if keyword.arg == "mode":
                mode_node = keyword.value
        return mode_node

    @classmethod
    def _open_is_mutating(cls, node: ast.Call, positional_index: int) -> bool:
        mode_node = cls._mode_node(node, positional_index)
        if mode_node is None:
            return False
        literal, mode = _literal(mode_node)
        if not literal or not isinstance(mode, str):
            return True
        return any(flag in mode for flag in "wax+")

    def _os_open_is_mutating(self, node: ast.Call) -> bool:
        flags_node: ast.AST | None = node.args[1] if len(node.args) >= 2 else None
        for keyword in node.keywords:
            if keyword.arg == "flags":
                flags_node = keyword.value
        if flags_node is None:
            return True
        literal, flags = _literal(flags_node)
        if literal and isinstance(flags, int) and not isinstance(flags, bool):
            write_mask = 0
            for name in _OS_OPEN_WRITE_FLAGS:
                write_mask |= int(getattr(os, name, 0))
            return bool(flags & write_mask)

        complete, names = self._os_open_flag_names(flags_node)
        if not complete or not names:
            return True
        if any(name in _OS_OPEN_WRITE_FLAGS for name in names):
            return True
        return any(name not in _OS_OPEN_READ_FLAGS for name in names)

    def _os_open_flag_names(self, node: ast.AST) -> tuple[bool, set[str]]:
        if isinstance(node, ast.Constant):
            valid = isinstance(node.value, int) and not isinstance(node.value, bool)
            return valid, set()
        if isinstance(node, ast.Name | ast.Attribute):
            name = self._call_name(node)
            if name.startswith("os.O_"):
                return True, {name.rsplit(".", 1)[-1]}
            return False, set()
        if isinstance(node, ast.BinOp) and isinstance(
            node.op,
            ast.BitOr | ast.BitAnd | ast.BitXor,
        ):
            left_complete, left = self._os_open_flag_names(node.left)
            right_complete, right = self._os_open_flag_names(node.right)
            return left_complete and right_complete, left | right
        return False, set()

    def _dynamic_attribute_lookup(
        self,
        node: ast.Call,
        name: str,
    ) -> tuple[str, str, ast.AST] | None:
        operation = name.removeprefix("builtins.")
        if operation in {"delattr", "getattr", "setattr"}:
            if len(node.args) < 2:
                return None
            return operation, self._call_name(node.args[0]), node.args[1]
        if name in _UNBOUND_ATTRIBUTE_ACCESSORS:
            if len(node.args) < 2:
                return None
            return "getattr", self._call_name(node.args[0]), node.args[1]
        if name.endswith(".__getattribute__") and node.args:
            return (
                "getattr",
                name.removesuffix(".__getattribute__"),
                node.args[0],
            )
        return None

    def _inspect_dynamic_process_lookup(self, node: ast.Call, name: str) -> None:
        lookup = self._dynamic_attribute_lookup(node, name)
        if lookup is None:
            return
        operation, owner, attribute_node = lookup
        literal, attribute = _literal(attribute_node)
        if not literal or not isinstance(attribute, str):
            if owner in _PROCESS_NAMESPACE_MODULES:
                self._capability(
                    "process",
                    "process.dynamic_namespace",
                    f"动态{operation}可暴露进程执行原语的模块命名空间 {owner}",
                    node,
                )
            elif self.generated and name.endswith(".__getattribute__"):
                self._add(
                    PolicyDecision.DENY,
                    "call.dynamic_attribute",
                    "Generated Tool 禁止动态 __getattribute__ 访问",
                    node,
                )
            if operation in {"delattr", "setattr"}:
                self._mark_mutating("检测到属性动态修改", node)
            return
        candidate = f"{owner}.{attribute}" if owner else attribute
        if _is_process_name(candidate):
            self._capability(
                "process",
                ("process.dynamic_reference" if operation == "getattr" else "process.dynamic_assignment"),
                f"动态{operation}进程执行原语 {candidate}",
                node,
            )
            if operation in {"delattr", "setattr"}:
                self._mark_mutating("检测到进程执行属性动态修改", node)
        elif _is_process_namespace_access(candidate):
            self._capability(
                "process",
                "process.dynamic_namespace",
                f"动态{operation}可暴露进程执行原语的模块命名空间 {candidate}",
                node,
            )

    def _inspect_generated_dynamic_lookup(self, node: ast.Call, name: str) -> None:
        if not self.generated:
            return
        lookup = self._dynamic_attribute_lookup(node, name)
        if lookup is None:
            return
        operation, owner, attribute_node = lookup
        literal, attribute = _literal(attribute_node)
        if owner in {"__builtins__", "builtins", "importlib", "sys.modules"}:
            self._add(
                PolicyDecision.DENY,
                "import.dynamic_namespace",
                f"Generated Tool 禁止动态{operation}执行命名空间 {owner}",
                node,
            )
            return
        if literal and isinstance(attribute, str):
            candidate = f"{owner}.{attribute}" if owner else attribute
            normalized = candidate.removeprefix("builtins.")
            if (
                normalized in _GENERATED_DENIED_BUILTINS
                or candidate in _DYNAMIC_IMPORT_CALLS
            ):
                self._add(
                    PolicyDecision.DENY,
                    "import.dynamic_reference",
                    f"Generated Tool 禁止动态引用 {candidate}",
                    node,
                )

    def _http_url_open_is_mutating(
        self,
        node: ast.Call,
        name: str,
    ) -> bool | None:
        method = name.rsplit(".", 1)[-1]
        if method not in {"putrequest", "urlopen"}:
            return None
        if name == "urllib.request.urlopen":
            data_node = node.args[1] if len(node.args) > 1 else None
            for keyword in node.keywords:
                if keyword.arg == "data":
                    data_node = keyword.value
            if data_node is not None:
                literal, data = _literal(data_node)
                if not literal or data is not None:
                    return True
            request_node = node.args[0] if node.args else None
            literal, request = _literal(request_node)
            return not (literal and isinstance(request, str))

        positional_index = 1 if name in _HTTP_URL_OPEN_CALLS else 0
        method_node = (
            node.args[positional_index]
            if len(node.args) > positional_index
            else None
        )
        for keyword in node.keywords:
            if keyword.arg == "method":
                method_node = keyword.value
        if method_node is None:
            return True
        literal, http_method = _literal(method_node)
        if literal and isinstance(http_method, str):
            return http_method.strip().upper() not in _HTTP_READ_ONLY_METHODS
        if self._call_name(method_node) in _HTTP_READ_ONLY_CONSTANTS:
            return False
        return True

    def _http_request_is_mutating(
        self,
        node: ast.Call,
        name: str,
    ) -> bool | None:
        if name.rsplit(".", 1)[-1] != "request":
            return None
        positional_index = 1 if name in _HTTP_UNBOUND_REQUEST_METHODS else 0
        method_node: ast.AST | None = node.args[positional_index] if len(node.args) > positional_index else None
        for keyword in node.keywords:
            if keyword.arg == "method":
                method_node = keyword.value
        is_request_method = isinstance(node.func, ast.Attribute) or "." in name
        if not is_request_method:
            return None
        if method_node is None:
            return _root_module(name) in _NETWORK_MODULES
        literal, method = _literal(method_node)
        if literal and isinstance(method, str):
            return method.strip().upper() not in _HTTP_READ_ONLY_METHODS
        if self._call_name(method_node) in _HTTP_READ_ONLY_CONSTANTS:
            return False
        return True

    def _safe_http_is_mutating(
        self,
        node: ast.Call,
        name: str,
    ) -> bool | None:
        if name not in _SAFE_HTTP_CALLS:
            return None
        method_node: ast.AST | None = None
        for keyword in node.keywords:
            if keyword.arg == "method":
                method_node = keyword.value
        if method_node is None:
            return False
        literal, method = _literal(method_node)
        if literal and isinstance(method, str):
            return method.strip().upper() not in _HTTP_READ_ONLY_METHODS
        if self._call_name(method_node) in _HTTP_READ_ONLY_CONSTANTS:
            return False
        return True

    def _inspect_named_call(self, node: ast.Call, name: str) -> None:
        normalized = name.removeprefix("builtins.")
        root = _root_module(name)
        method = name.rsplit(".", 1)[-1]

        if name in self.known_functions:
            self.direct_calls.add(name)

        if self.generated and normalized in _GENERATED_DENIED_BUILTINS:
            self._add(
                PolicyDecision.DENY,
                "call.dynamic",
                f"Generated Tool 禁止调用 {normalized}",
                node,
            )
        if self.generated and method in _GENERATED_DENIED_ATTRIBUTES:
            self._add(
                PolicyDecision.DENY,
                "call.dynamic_attribute",
                f"Generated Tool 禁止动态属性调用 {name}",
                node,
            )
        if self.generated and (
            name in _DYNAMIC_IMPORT_CALLS
            or name.startswith("__builtins__.")
            or name.startswith("sys.modules.")
        ):
            self._add(
                PolicyDecision.DENY,
                "import.dynamic",
                "Generated Tool 禁止动态导入",
                node,
            )

        self._inspect_dynamic_process_lookup(node, name)
        self._inspect_generated_dynamic_lookup(node, name)
        if _is_process_name(name):
            self._capability(
                "process",
                "process.call",
                f"调用进程执行原语 {name}",
                node,
            )
            # A process primitive can change host or external state even when
            # the tool manifest claims read_only.  Custom tools may receive an
            # explicit process capability, but they must still pass through
            # the mutating-tool confirmation path.
            self._mark_mutating("检测到系统命令或进程执行调用", node)
        elif _is_process_namespace_access(name):
            self._capability(
                "process",
                "process.dynamic_namespace",
                f"动态调用可暴露进程执行原语的模块命名空间 {name}",
                node,
            )
        if name in _SAFE_HTTP_CALLS:
            self._capability(
                "network",
                "capability.network",
                "调用 safe_request 需要 network capability",
                node,
            )
        elif root in _NETWORK_MODULES:
            if self.custom_file:
                self._add(
                    PolicyDecision.DENY,
                    "network.raw_client",
                    f"Custom File 禁止直接调用网络客户端 {name}；请使用 safe_request",
                    node,
                    capability="network",
                )
            else:
                self._capability(
                    "network",
                    "capability.network",
                    f"调用 {name} 需要 network capability",
                    node,
                )

        if self.custom_file and normalized in {
            "__import__",
            "importlib.__import__",
            "importlib.import_module",
        }:
            module_node = node.args[0] if node.args else None
            literal, module_name = _literal(module_node)
            if (
                not literal
                or not isinstance(module_name, str)
                or _root_module(module_name) in _NETWORK_MODULES
            ):
                self._add(
                    PolicyDecision.DENY,
                    "network.raw_client",
                    "Custom File 禁止动态加载网络客户端；请使用 safe_request",
                    node,
                    capability="network",
                )

        if (isinstance(node.func, ast.Name) and normalized == "open") or name in {"builtins.open", "io.open"}:
            if self._open_is_mutating(node, 1):
                self._mark_mutating("检测到写模式文件打开", node)
        elif name == "os.open":
            if self._os_open_is_mutating(node):
                self._mark_mutating("检测到可能写入的 os.open", node)
        elif method == "open":
            positional_index = 1 if name == "pathlib.Path.open" else 0
            if self._open_is_mutating(node, positional_index):
                self._mark_mutating("检测到写模式 Path.open 或对象 open", node)
        elif name in _MUTATING_CALLS:
            self._mark_mutating(f"检测到状态变更调用 {name}", node)
        elif method in _MUTATING_METHOD_NAMES:
            self._mark_mutating(f"检测到可能的状态变更方法 {method}", node)

        if self._http_request_is_mutating(node, name):
            self._mark_mutating("检测到可能写入远端状态的 HTTP request", node)
        if self._http_url_open_is_mutating(node, name):
            self._mark_mutating("检测到可能写入远端状态的 HTTP 调用", node)
        if self._safe_http_is_mutating(node, name):
            self._mark_mutating("检测到可能写入远端状态的 safe_request", node)

        if normalized == "print" and any(keyword.arg == "file" for keyword in node.keywords):
            self._mark_mutating("检测到 print(file=...) 文件输出", node)

    def visit_Call(self, node: ast.Call) -> None:
        self._inspect_named_call(node, self._call_name(node.func))
        # Chained calls such as ``importlib.__import__(...).run()`` hide the
        # dangerous inner callable below an Attribute node. Always descend into
        # the complete callable expression; finding de-duplication keeps direct
        # Name/Attribute calls stable.
        self.visit(node.func)
        for argument in node.args:
            self.visit(argument)
        for keyword in node.keywords:
            self.visit(keyword.value)

    def visit_Name(self, node: ast.Name) -> None:
        if self.generated and node.id == "__builtins__":
            self._add(
                PolicyDecision.DENY,
                "call.dynamic_reference",
                "Generated Tool 禁止访问动态执行入口 __builtins__",
                node,
            )
        if isinstance(node.ctx, ast.Load):
            normalized = self._call_name(node).removeprefix("builtins.")
            if self.generated and normalized in _GENERATED_DENIED_BUILTINS:
                self._add(
                    PolicyDecision.DENY,
                    "call.dynamic_reference",
                    f"Generated Tool 禁止引用动态执行入口 {node.id}",
                    node,
                )
            self._inspect_process_reference(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, ast.Load):
            name = self._call_name(node)
            if self.generated and (
                node.attr in _GENERATED_DENIED_ATTRIBUTES
                or name in _DYNAMIC_IMPORT_CALLS
                or name.startswith("sys.modules.")
            ):
                self._add(
                    PolicyDecision.DENY,
                    "call.dynamic_attribute",
                    f"Generated Tool 禁止动态属性访问 {name or node.attr}",
                    node,
                )
            self._inspect_process_reference(node)
        self.visit(node.value)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        container = self._call_name(node.value)
        literal, key = _literal(node.slice)
        if self.generated and (
            container in {"__builtins__", "builtins", "sys.modules"}
            or (
                literal
                and isinstance(key, str)
                and key in _GENERATED_DENIED_SUBSCRIPT_KEYS
            )
        ):
            self._add(
                PolicyDecision.DENY,
                "call.dynamic_subscript",
                "Generated Tool 禁止通过映射访问动态执行入口",
                node,
            )
        self.visit(node.value)
        self.visit(node.slice)


def _analyze_function(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    source_type: str,
    known_functions: set[str],
    module_aliases: Mapping[str, str],
) -> _ScopeAnalysis:
    visitor = _ScopeVisitor(
        source_type=source_type,
        scope=node.name,
        known_functions=known_functions,
        aliases=module_aliases,
        alias_calls=_collect_alias_calls(node.body),
        scan_nested_function_bodies=True,
    )
    for argument in [
        *node.args.posonlyargs,
        *node.args.args,
        *node.args.kwonlyargs,
    ]:
        visitor.aliases.pop(argument.arg, None)
    for statement in node.body:
        visitor.visit(statement)
    return visitor.result()


def _collect_reachable(
    initial: Iterable[str],
    scopes: Mapping[str, _ScopeAnalysis],
    *,
    origin: str,
) -> tuple[set[str], _FindingTemplate | None]:
    pending = [(name, 1) for name in sorted(set(initial), reverse=True)]
    reached: set[str] = set()
    while pending:
        name, depth = pending.pop()
        if name in reached or name not in scopes:
            continue
        if depth > _MAX_CALL_GRAPH_DEPTH or len(reached) >= _MAX_CALL_GRAPH_FUNCTIONS:
            return reached, _FindingTemplate(
                decision=PolicyDecision.DENY,
                code="analysis.call_graph_limit",
                message=("本文件 helper 调用图超过静态分析上限，候选代码按 fail closed 拒绝"),
                line=None,
                capability=None,
                scope=origin,
            )
        reached.add(name)
        pending.extend((child, depth + 1) for child in sorted(scopes[name].direct_calls, reverse=True) if child not in reached)
    return reached, None


def analyze_ast_policy(
    tree: ast.Module,
    *,
    source_type: str,
    policy: ToolPolicy,
    handler_names: Iterable[str] | None = None,
    handler_policies: Mapping[str, ToolPolicy] | None = None,
) -> AstPolicyReport:
    """Analyze one source file without claiming to replace runtime isolation."""

    if source_type not in {"custom_file", "generated"}:
        raise ValueError("AST policy source_type 仅支持 custom_file 或 generated")
    if not isinstance(policy, ToolPolicy):
        raise ValueError("AST policy 必须提供 ToolPolicy")

    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    known_functions = set(functions)
    module_visitor = _ScopeVisitor(
        source_type=source_type,
        scope="<module>",
        known_functions=known_functions,
        alias_calls=_collect_alias_calls(tree.body),
        scan_nested_function_bodies=False,
    )
    for statement in tree.body:
        module_visitor.visit(statement)
    module_scope = module_visitor.result()

    function_scopes = {
        name: _analyze_function(
            node,
            source_type=source_type,
            known_functions=known_functions,
            module_aliases=module_visitor.aliases,
        )
        for name, node in functions.items()
    }
    call_graph_values = {
        "<module>": tuple(sorted(module_scope.direct_calls)),
        **{name: tuple(sorted(scope.direct_calls)) for name, scope in function_scopes.items()},
    }

    module_templates = list(module_scope.findings)
    module_effect = module_scope.detected_effect
    module_reachable, module_limit = _collect_reachable(
        module_scope.direct_calls,
        function_scopes,
        origin="<module>",
    )
    for name in sorted(module_reachable):
        scope = function_scopes[name]
        module_templates.extend(scope.findings)
        if scope.detected_effect is ToolEffect.MUTATING:
            module_effect = ToolEffect.MUTATING
    if module_limit is not None:
        module_templates.append(module_limit)
    module_findings = _materialize(module_templates, policy)

    configured_policies = dict(handler_policies or {})
    if handler_names is None:
        selected_handlers = (
            set(configured_policies) if configured_policies else {name for name in functions if not name.startswith("_")}
        )
    else:
        selected_handlers = set(handler_names)

    reports: dict[str, HandlerPolicyReport] = {}
    for name in sorted(selected_handlers):
        selected_policy = configured_policies.get(name, policy)
        templates = list(module_templates)
        effect = module_effect
        reachable: set[str] = set()
        direct_calls: tuple[str, ...] = ()
        node = functions.get(name)
        if node is None:
            templates.append(
                _FindingTemplate(
                    decision=PolicyDecision.DENY,
                    code="handler.missing",
                    message=f"handler 不存在: {name}",
                    line=None,
                    capability=None,
                    scope=name,
                )
            )
        else:
            own_scope = function_scopes[name]
            direct_calls = tuple(sorted(own_scope.direct_calls))
            reachable, limit = _collect_reachable(
                {name},
                function_scopes,
                origin=name,
            )
            for reachable_name in sorted(reachable):
                reachable_scope = function_scopes[reachable_name]
                templates.extend(reachable_scope.findings)
                if reachable_scope.detected_effect is ToolEffect.MUTATING:
                    effect = ToolEffect.MUTATING
            if limit is not None:
                templates.append(limit)
        reports[name] = HandlerPolicyReport(
            handler=name,
            findings=_materialize(templates, selected_policy),
            detected_effect=effect,
            direct_calls=direct_calls,
            reachable_functions=tuple(sorted(reachable)),
        )

    return AstPolicyReport(
        module_findings=module_findings,
        handler_reports=MappingProxyType(reports),
        call_graph=MappingProxyType(call_graph_values),
        module_effect=module_effect,
    )

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ToolEffect(str, Enum):
    READ_ONLY = "read_only"
    MUTATING = "mutating"


@dataclass(frozen=True)
class ToolContext:
    bot: Any
    event: Any
    request_id: int | None = None
    confirmed: bool = False


@dataclass(frozen=True)
class ToolResult:
    text: str = ""
    images: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


ToolHandler = Callable[..., Awaitable[Any] | Any]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: Mapping[str, Any]
    handler: ToolHandler
    effect: ToolEffect = ToolEffect.READ_ONLY
    permission: str = "user"
    timeout_seconds: float | None = None
    result_limit: int | None = None
    dependencies: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.name or len(self.name) > 64:
            raise ValueError("工具名必须是 1 到 64 个字符")
        if not self.description.strip():
            raise ValueError("工具描述不能为空")
        if not isinstance(self.parameters, Mapping):
            raise ValueError("工具 parameters 必须是 JSON Schema 映射")
        if self.permission not in {"user", "superuser"}:
            raise ValueError("工具 permission 仅支持 user 或 superuser")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("工具 timeout_seconds 必须大于 0")
        if self.result_limit is not None and self.result_limit <= 0:
            raise ValueError("工具 result_limit 必须大于 0")

    def as_legacy_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": dict(self.parameters),
            "func": self.handler,
            "tool_spec": self,
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec, *, replace: bool = False) -> ToolSpec:
        if spec.name in self._tools and not replace:
            raise ValueError(f"工具已注册: {spec.name}")
        self._tools[spec.name] = spec
        return spec

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def snapshot(self) -> dict[str, ToolSpec]:
        return dict(self._tools)


tool_registry = ToolRegistry()


def register_tool(spec: ToolSpec, *, replace: bool = False) -> ToolSpec:
    """Register a first-class MoEllm tool without synthesizing a NoneBot event."""
    return tool_registry.register(spec, replace=replace)

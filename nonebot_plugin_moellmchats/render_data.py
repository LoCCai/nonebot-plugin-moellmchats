"""Session-scoped text and field locations from host-rendered cards, without OCR."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from html.parser import HTMLParser
import math
import re
import time
import uuid

from nonebot import get_driver

from .tool_contracts import ToolContext, ToolEffect, ToolResult, ToolSpec, register_tool

_TTL = 600
_MAX_VIEWS = 64
_views: OrderedDict[str, tuple[float, tuple[str, str], dict]] = OrderedDict()
_OMIT = re.compile(r"css|style|(?:^|_)(?:icon|image|avatar|portrait|font|url|uri|token|password|secret|cookie)(?:_|$)", re.I)


def render_scope(bot, event) -> tuple[str, str] | None:
    bot_id = getattr(bot, "self_id", None)
    if not isinstance(bot_id, (str, int)) or not str(bot_id):
        return None
    try:
        return str(bot_id), event.get_session_id()
    except (AttributeError, ValueError):
        return None


def _capture():
    from .event_simulator import _capture_key, _captures

    capture = _captures.get(_capture_key.get())
    return capture if capture and capture.get("render_scope") else None


def is_render_capture_active() -> bool:
    return _capture() is not None


class _TextNodes(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.counts = [{}]
        self.nodes = []
        self.text = []
        self.remaining = 24_000
        self.truncated = False
        self.nodes_truncated = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        counts = self.counts[-1]
        counts[tag] = counts.get(tag, 0) + 1
        selector = f"{tag}:nth-of-type({counts[tag]})"
        hidden = (
            bool(self.stack and self.stack[-1][2])
            or tag in {"head", "script", "style", "svg"}
            or "hidden" in attrs
            or attrs.get("aria-hidden") == "true"
            or bool(re.search(r"display\s*:\s*none", attrs.get("style", ""), re.I))
        )
        if tag not in {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }:
            self.stack.append((tag, selector, hidden))
            self.counts.append({})

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                del self.counts[index + 1 :]
                break

    def handle_data(self, text):
        text = " ".join(text.split())
        if not text or (self.stack and self.stack[-1][2]):
            return
        if self.remaining <= 0:
            self.truncated = True
            return
        chunk = text[: min(self.remaining, 2048)]
        self.truncated |= len(chunk) < len(text)
        self.remaining -= len(chunk)
        self.text.append(chunk)
        if len(self.nodes) >= 256:
            self.nodes_truncated = True
            return
        selector = " > ".join(row[1] for row in self.stack)
        if len(selector) > 2048:
            self.nodes_truncated = True
            return
        self.nodes.append({"selector": selector, "text": chunk})


def _clean(value, budget, depth=0):
    if budget[0] <= 0 or depth > 16:
        return "[已截断]"
    if value is None or isinstance(value, (bool, int)):
        budget[0] -= 16
        return value
    if isinstance(value, float):
        budget[0] -= 24
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        if value.startswith(("data:", "base64://", "file://")):
            return "[素材引用已省略]"
        result = value[: min(2048, max(0, budget[0]))]
        budget[0] -= len(result)
        return result + ("[已截断]" if len(result) < len(value) else "")
    if isinstance(value, Mapping):
        result = {}
        for key, child in value.items():
            if not isinstance(key, str) or key.startswith("_") or _OMIT.search(key):
                continue
            if budget[0] <= 0:
                result["__truncated__"] = True
                break
            budget[0] -= len(key)
            result[key[:160]] = _clean(child, budget, depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        result = []
        for child in value[:1024]:
            if budget[0] <= 0:
                break
            result.append(_clean(child, budget, depth + 1))
        if len(result) < len(value):
            result.append({"__truncated__": True})
        return result
    return "[非文本字段]"


def _expire():
    now = time.monotonic()
    for key, (expires, _, _) in list(_views.items()):
        if expires <= now:
            _views.pop(key, None)


def record_rendered_view(template: str, html: str, data: Mapping) -> str | None:
    """Hosts call this after rendering; only an active tool dispatch records it."""
    capture = _capture()
    if capture is None:
        return None
    parser = _TextNodes()
    parser.feed(html[:2_000_000])
    payload = {
        "template": template,
        "text": "\n".join(parser.text),
        "nodes": parser.nodes,
        "text_truncated": parser.truncated or len(html) > 2_000_000,
        "nodes_truncated": parser.nodes_truncated or len(html) > 2_000_000,
        "data": _clean(data, [64_000]),
    }
    _expire()
    render_id = uuid.uuid4().hex
    _views[render_id] = (time.monotonic() + _TTL, capture["render_scope"], payload)
    while len(_views) > _MAX_VIEWS:
        _views.popitem(last=False)
    refs = capture.setdefault("rendered_views", [])
    refs.append(
        {
            "render_id": render_id,
            "template": template,
            "text": payload["text"][:1200],
            "text_truncated": payload["text_truncated"] or len(payload["text"]) > 1200,
            "locations": {"text": "/text", "text_nodes_with_selectors": "/nodes", "fields": "/data"},
            "read_tool": "read_rendered_view",
        }
    )
    del refs[:-2]
    return render_id


def _pointer_part(value):
    return str(value).replace("~", "~0").replace("/", "~1")


async def read_rendered_view(
    render_id: str, pointer: str = "", offset: int = 0, limit: int = 10, *, _tool_context: ToolContext
) -> ToolResult:
    """Read only a card belonging to this exact bot and conversation session."""
    _expire()
    record = _views.get(render_id)
    if record is None or record[1] != render_scope(_tool_context.bot, _tool_context.event):
        return ToolResult(text="卡片数据不存在、已过期或不属于当前会话，请重新查询原功能。")
    if not isinstance(pointer, str) or (pointer and not pointer.startswith("/")):
        return ToolResult(text="pointer 需使用返回的 JSON Pointer 字段路径。")
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 20:
        return ToolResult(text="offset 必须非负，limit 为1～20。")
    value = record[2]
    try:
        for part in pointer.split("/")[1:]:
            part = part.replace("~1", "/").replace("~0", "~")
            if isinstance(value, list):
                if not part.isdigit():
                    raise KeyError(part)
                value = value[int(part)]
            elif isinstance(value, dict):
                value = value[part]
            else:
                raise KeyError(part)
    except (KeyError, IndexError, ValueError):
        return ToolResult(text="未找到该字段路径，请读取上层路径确认字段名。")
    result = {"render_id": render_id, "pointer": pointer, "offset": offset}
    if isinstance(value, (dict, list)):
        items = list(value.items()) if isinstance(value, dict) else list(enumerate(value))
        rows = []
        for key, child in items[offset : offset + limit]:
            location = pointer + "/" + _pointer_part(key)
            row = {"key": str(key), "pointer": location}
            if isinstance(child, (dict, list)):
                row.update(type="object" if isinstance(child, dict) else "array", count=len(child))
            else:
                row["value"] = child[:800] if isinstance(child, str) else child
                if isinstance(child, str) and len(child) > 800:
                    row["truncated"] = True
            rows.append(row)
        end = offset + len(rows)
        result.update(items=rows, total=len(items), next_offset=end if end < len(items) else None)
    elif isinstance(value, str):
        end = min(len(value), offset + 4096)
        result.update(value=value[offset:end], total=len(value), next_offset=end if end < len(value) else None)
    else:
        result.update(value=value, next_offset=None)
    return ToolResult(text="原始渲染文字和字段；按 pointer/next_offset 继续读取，无需识图。", structured=result)


READ_RENDERED_VIEW_TOOL = ToolSpec(
    name="read_rendered_view",
    handler=read_rendered_view,
    description="直接读取刚才插件卡片的渲染文字、DOM位置和结构化字段，无需图片识别。"
    "使用原结果的render_id，根路径列目录，/text读文字，/nodes读文本位置，/data读字段。"
    "按返回pointer深入、next_offset分页；数据仅当前用户会话可读，10分钟过期。",
    parameters={
        "type": "object",
        "properties": {
            "render_id": {"type": "string", "pattern": "^[a-f0-9]{32}$"},
            "pointer": {"type": "string", "maxLength": 1024},
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        },
        "required": ["render_id"],
        "additionalProperties": False,
    },
    effect=ToolEffect.READ_ONLY,
    permission="user",
    timeout_seconds=5,
    result_limit=12000,
)


@get_driver().on_startup
async def _register_render_data_tool():
    register_tool(READ_RENDERED_VIEW_TOOL)

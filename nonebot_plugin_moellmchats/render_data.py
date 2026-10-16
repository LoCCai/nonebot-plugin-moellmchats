"""Session-scoped text and field locations from host-rendered cards, without OCR."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
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
_local_capture: ContextVar[dict | None] = ContextVar("rendered_card_capture", default=None)
_pending: OrderedDict[tuple, tuple[float, tuple[dict, ...]]] = OrderedDict()
_published: OrderedDict[tuple, tuple[float, tuple[dict, ...]]] = OrderedDict()
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
    return capture if capture and capture.get("render_scope") else _local_capture.get()


@contextmanager
def capture_rendered_views(bot, event):
    """Allow native host queries to retain the same evidence as AI queries."""
    existing = _capture()
    if existing is not None:
        yield existing
        return
    scope = render_scope(bot, event)
    capture = {"render_scope": scope} if scope else None
    token = _local_capture.set(capture)
    try:
        yield capture
    finally:
        _local_capture.reset(token)


def _message_scope(bot, event):
    if render_scope(bot, event) is None:
        return None
    group = getattr(event, "group_id", None)
    channel = getattr(event, "channel_id", None)
    scene = ("group", str(group)) if group is not None else (
        ("channel", str(channel)) if channel else ("private", event.get_user_id())
    )
    return str(bot.self_id), *scene


def _picture_key(bot, event, pictures):
    scope = render_scope(bot, event)
    if scope is None:
        return None
    digest = hashlib.sha256()
    for picture in pictures if isinstance(pictures, list) else [pictures]:
        digest.update(len(picture).to_bytes(8, "big"))
        digest.update(picture)
    return (*scope, digest.hexdigest())


async def remember_rendered_result(bot, event, pictures, capture):
    """Keep only a digest, never image bytes; publication needs a send receipt."""
    refs = tuple((capture or {}).get("rendered_views", ()))
    key = await asyncio.to_thread(_picture_key, bot, event, pictures)
    if key is not None and refs:
        _expire()
        _pending[key] = (time.monotonic() + _TTL, refs)
        while len(_pending) > 128:
            _pending.popitem(last=False)


async def bind_rendered_message(bot, event, pictures, receipt):
    message_id = receipt.get("message_id") if isinstance(receipt, Mapping) else receipt
    if isinstance(message_id, bool) or not isinstance(message_id, (int, str)) or not str(message_id):
        return
    key = await asyncio.to_thread(_picture_key, bot, event, pictures)
    record = _pending.pop(key, None)
    if record is not None and record[0] > time.monotonic():
        _published[(*_message_scope(bot, event), str(message_id))] = (time.monotonic() + 3600, record[1])
        while len(_published) > 256:
            _published.popitem(last=False)


def rendered_views_for_message(bot, event, message_id):
    """A confirmed public card can be read by its chat's quoting user only.

    Rebind fresh read handles to that user's existing strict render scope.
    Knowing a render ID never grants access to another user or conversation.
    """
    _expire()
    scope = _message_scope(bot, event)
    record = _published.get((*scope, str(message_id))) if scope else None
    if record is None:
        return []
    refs = []
    for original in record[1]:
        view = _views.get(original["render_id"])
        if view is None:
            refs.append({"template": original["template"], "expired": True,
                         "text": "原始卡片数据已过期，请重新查询原功能；不要从图片猜测数值。"})
            continue
        ref = dict(original)
        if view[1] != render_scope(bot, event):
            ref["render_id"] = uuid.uuid4().hex
            _views[ref["render_id"]] = (view[0], render_scope(bot, event), view[2])
        refs.append(ref)
    while len(_views) > _MAX_VIEWS:
        _views.popitem(last=False)
    return refs


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
        self.node_remaining = 16_000
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
        chunk = text[: self.remaining]
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
        node_size = len(selector) + min(len(chunk), 2048)
        if node_size > self.node_remaining:
            self.nodes_truncated = True
            return
        self.node_remaining -= node_size
        self.nodes_truncated |= len(chunk) > 2048
        self.nodes.append({"selector": selector, "text": chunk[:2048]})


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
    for store in (_pending, _published):
        for key, (expires, _) in list(store.items()):
            if expires <= now:
                store.pop(key, None)


def record_rendered_view(template: str, html: str, data: Mapping) -> str | None:
    """Record text during a scoped host render or active tool dispatch."""
    capture = _capture()
    if capture is None:
        return None
    parser = _TextNodes()
    parser.feed(html[:2_000_000])
    text = "\n".join(parser.text)
    # Share a bounded text budget with location summaries; ordinary cards
    # retain the existing field budget, including when one text node is long.
    field_budget = min(64_000, 80_000 - len(text) - (16_000 - parser.node_remaining))
    payload = {
        "template": template,
        "text": text,
        "nodes": parser.nodes,
        "text_truncated": parser.truncated or len(html) > 2_000_000,
        "nodes_truncated": parser.nodes_truncated or len(html) > 2_000_000,
        "data": _clean(data, [max(0, field_budget)]),
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
            "text": payload["text"][:4000],
            "text_truncated": payload["text_truncated"] or len(payload["text"]) > 4000,
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

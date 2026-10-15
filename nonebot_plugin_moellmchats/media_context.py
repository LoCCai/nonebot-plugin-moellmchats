"""Read event-owned messages and attachments without exposing local paths or signed URLs."""
from __future__ import annotations

import base64
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
import ipaddress
import json
import mimetypes
import secrets
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlsplit

import aiohttp

from .compat import timeout as timeout_scope
from .network_safety import validate_public_url
from .onebot_facade import event_message, event_reply_message_id

if TYPE_CHECKING:
    from .tool_contracts import ToolContext

MAX_MEDIA_BYTES = 20 * 1024 * 1024
MAX_DOCUMENT_CHARS = 120_000
_TTL = 600
_MAX_STORE_BYTES = 64 * 1024 * 1024


class MediaError(ValueError):
    """A safe, user-readable media processing error."""


def context_scope(context: ToolContext) -> tuple[str, str, str]:
    event = context.event
    user_id = event.get_user_id()
    group_id = getattr(event, "group_id", None)
    channel_id = getattr(event, "channel_id", None)
    scene = f"group:{group_id}" if group_id is not None else f"channel:{channel_id}" if channel_id else "private"
    return str(context.bot.self_id), scene, str(user_id)


@dataclass
class MediaAsset:
    kind: str
    name: str
    data: dict[str, Any] = field(default_factory=dict)
    payload: bytes | None = None
    mime: str = ""


@dataclass
class MessageDocument:
    lines: list[str] = field(default_factory=list)
    assets: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    nodes: int = 0
    chars: int = 0
    partial: bool = False

    def append(self, text: str) -> None:
        remaining = MAX_DOCUMENT_CHARS - self.chars
        if len(text) > remaining:
            self.partial = True
        text = text[:remaining]
        if text:
            self.lines.append(text)
            self.chars += len(text)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


class MediaStore:
    def __init__(self) -> None:
        self.entries: OrderedDict[str, tuple[tuple[str, str, str], float, Any]] = OrderedDict()

    def _prune(self) -> None:
        now = time.monotonic()
        for key, (_, expiry, _) in list(self.entries.items()):
            if expiry < now:
                del self.entries[key]
        total = sum(len(value.payload or b"") for _, _, value in self.entries.values() if isinstance(value, MediaAsset))
        while self.entries and (len(self.entries) > 256 or total > _MAX_STORE_BYTES):
            _, (_, _, value) = self.entries.popitem(last=False)
            if isinstance(value, MediaAsset):
                total -= len(value.payload or b"")

    def put(self, context: ToolContext, value: Any) -> str:
        reference = "media_" + secrets.token_urlsafe(16)
        self.entries[reference] = (context_scope(context), time.monotonic() + _TTL, value)
        self._prune()
        return reference

    def get(self, context: ToolContext, reference: str, expected: type) -> Any:
        self._prune()
        row = self.entries.get(reference)
        if row is None or row[0] != context_scope(context) or not isinstance(row[2], expected):
            raise MediaError("素材引用已过期或不属于当前用户会话，请重新读取原消息。")
        return row[2]


media_store = MediaStore()


async def _call(context: ToolContext, api: str, **kwargs: Any) -> Any:
    async with timeout_scope(8):
        return await context.bot.call_api(api, **kwargs, _timeout=8)


def _segments(raw: Any) -> list:
    if isinstance(raw, str):
        try:
            from nonebot.adapters.onebot.v11 import Message
            return list(Message(raw))
        except Exception:
            return [{"type": "text", "data": {"text": raw}}]
    if isinstance(raw, Mapping):
        return [raw]
    return list(raw) if raw is not None else []


def _segment(segment: Any) -> tuple[str, dict]:
    if isinstance(segment, Mapping):
        return str(segment.get("type", "")), dict(segment.get("data") or {})
    return str(getattr(segment, "type", "")), dict(getattr(segment, "data", {}) or {})


def _forward_id(data: Mapping) -> str:
    try:
        parsed = json.loads(data.get("data", "{}"))
        meta = parsed.get("meta", {})
        for key in ("detail", "news", "forward"):
            value = meta.get(key, {})
            if isinstance(value, Mapping) and value.get("resid"):
                return str(value["resid"])
    except (ValueError, TypeError, AttributeError):
        pass
    return ""


async def collect_message_context(context: ToolContext, *, source: str = "message", max_messages: int = 100) -> MessageDocument:
    """Walk current/replied/forwarded content; history is confined to the triggering group."""
    if source not in {"message", "group_history"} or type(max_messages) is not int or not 1 <= max_messages <= 200:
        raise MediaError("source 须为 message/group_history，max_messages 须为 1 至 200。")
    document = MessageDocument()
    seen: set[tuple[str, str]] = set()
    calls = 0

    async def walk(raw: Any, depth: int = 0, label: str = "") -> None:
        nonlocal calls
        if depth > 8 or document.nodes >= 1000 or document.chars >= MAX_DOCUMENT_CHARS:
            document.partial = True
            return
        if label:
            document.append(label)
        for segment in _segments(raw):
            document.nodes += 1
            if document.nodes > 1000 or document.chars >= MAX_DOCUMENT_CHARS:
                document.partial = True
                break
            kind, data = _segment(segment)
            if kind == "text":
                document.append(str(data.get("text", "")))
            elif kind in {"at", "mention"}:
                document.append(f"[CQ:at,qq={data.get('qq', data.get('user_id', ''))}]")
            elif kind in {"image", "video", "record", "audio", "file"}:
                if len(document.assets) >= 16:
                    document.partial = True
                    continue
                name = str(data.get("name") or data.get("file_name") or kind)
                if name == "file" and isinstance(data.get("file"), str):
                    candidate = data["file"]
                    if "/" not in candidate and "\\" not in candidate and "." in candidate:
                        name = candidate
                # A gateway file value may itself be a URL or path. Never use
                # it as a model-facing display name.
                name = name.replace("\\", "/").rsplit("/", 1)[-1].split("?", 1)[0][:120]
                asset = MediaAsset("audio" if kind == "record" else kind, name, data)
                reference = media_store.put(context, asset)
                document.assets.append(reference)
                document.append(f"[{asset.kind} 素材 {reference} · {name}]")
            elif kind == "node" or (kind == "forward" and data.get("content")):
                content = data.get("content") or data.get("message") or []
                if kind == "node":
                    await walk(content, depth + 1, f"[转发者 {data.get('name', data.get('uin', '未知'))}]")
                else:
                    await containers(content, depth + 1)
            elif kind in {"forward", "json"}:
                forward_id = str(data.get("id") or data.get("resid") or "") if kind == "forward" else _forward_id(data)
                if not forward_id or ("forward", forward_id) in seen:
                    continue
                seen.add(("forward", forward_id))
                if calls >= 12:
                    document.partial = True
                    continue
                calls += 1
                try:
                    response = await _call(context, "get_forward_msg", id=forward_id)
                    await containers(response.get("messages", response.get("message", [])), depth + 1)
                except Exception:
                    document.warnings.append("一部分合并转发未能读取。")
                    document.partial = True

    async def containers(rows: Any, depth: int) -> None:
        if depth > 8:
            document.partial = True
            return
        for row in list(rows or [])[:1000]:
            if not isinstance(row, Mapping):
                continue
            sender = row.get("sender") or {}
            label = f"[{row.get('time', '')} {sender.get('card') or sender.get('nickname') or sender.get('user_id') or '未知'}]"
            await walk(row.get("message", row.get("content", [row])), depth, label)

    if source == "group_history":
        group_id = getattr(context.event, "group_id", None)
        if group_id is None:
            raise MediaError("读取群聊历史需要在对应群内发起。")
        history: dict[str, Mapping] = {}
        seq = None
        for _ in range(10):
            params = {"group_id": group_id, "count": min(100, max_messages - len(history))}
            if seq is not None:
                params["message_seq"] = seq
            response = await _call(context, "get_group_msg_history", **params)
            rows = response.get("messages", []) if isinstance(response, Mapping) else []
            added = 0
            for row in rows:
                if row.get("group_id", group_id) != group_id:
                    continue
                identity = str(row.get("message_id") or row.get("message_seq") or "")
                if identity and identity not in history:
                    history[identity] = row
                    added += 1
            if len(history) >= max_messages or not rows or not added:
                break
            sequences = [int(row["message_seq"]) for row in rows if str(row.get("message_seq", "")).isdigit()]
            next_seq = min(sequences) - 1 if sequences else None
            if next_seq is None or next_seq <= 0 or next_seq == seq:
                break
            seq = next_seq
        ordered = sorted(history.values(), key=lambda row: (int(row.get("time", 0)), int(row.get("message_seq", 0))))
        await containers(ordered[-max_messages:], 0)
        if len(history) < max_messages:
            document.warnings.append(f"本次实际取得 {len(history)} 条群消息，不能据此声称覆盖整天。")
    else:
        await walk(event_message(context.event), label="[当前消息]")
        reply = getattr(context.event, "reply", None)
        raw = getattr(reply, "message", None)
        if raw is None and (reply_id := event_reply_message_id(context.event)):
            try:
                response = await _call(context, "get_msg", message_id=int(reply_id))
                if response.get("group_id", getattr(context.event, "group_id", None)) != getattr(context.event, "group_id", None):
                    raise MediaError("引用消息与当前会话不符。")
                raw = response.get("message")
            except Exception:
                document.warnings.append("引用消息未能读取。")
                document.partial = True
        if raw is not None:
            await walk(raw, label="[引用消息]")
    return document


class _PublicResolver(aiohttp.resolver.DefaultResolver):
    async def resolve(self, host: str, port: int = 0, family: int = 0) -> list:
        rows = await super().resolve(host, port, family)
        if not rows or any(not ipaddress.ip_address(row["host"]).is_global for row in rows):
            raise MediaError("附件地址解析到了非公网地址。")
        return rows


async def download_media(url: str) -> tuple[bytes, str]:
    """Validate every redirect and the addresses actually used by the connector."""
    current = url
    try:
        connector = aiohttp.TCPConnector(resolver=_PublicResolver())
        async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=40)) as session:
            for _ in range(5):
                await validate_public_url(current)
                async with session.get(current, allow_redirects=False) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        current = urljoin(current, response.headers.get("location", ""))
                        continue
                    if response.status != 200:
                        raise MediaError(f"附件下载失败（HTTP {response.status}）。")
                    if response.content_length and response.content_length > MAX_MEDIA_BYTES:
                        raise MediaError("附件超过 20MiB，请缩小或分段后重试。")
                    data = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        data.extend(chunk)
                        if len(data) > MAX_MEDIA_BYTES:
                            raise MediaError("附件超过 20MiB，请缩小或分段后重试。")
                    return bytes(data), response.headers.get("content-type", "").split(";", 1)[0]
        raise MediaError("附件重定向次数过多。")
    except MediaError:
        raise
    except Exception as error:
        raise MediaError("附件暂时无法下载，请稍后重试。") from error


async def asset_bytes(context: ToolContext, reference: str) -> MediaAsset:
    asset = media_store.get(context, reference, MediaAsset)
    if asset.payload is not None:
        return asset
    data = asset.data
    url = data.get("url") or data.get("file_url")
    file_id = data.get("file_id") or data.get("id") or data.get("file")
    if asset.kind == "audio" and file_id:
        try:
            result = await _call(context, "get_record", file=str(file_id), out_format="wav")
            encoded = result.get("base64")
            if isinstance(encoded, str) and len(encoded) <= MAX_MEDIA_BYTES * 4 // 3 + 128:
                payload = base64.b64decode(encoded.removeprefix("base64://"), validate=True)
                if payload[:4] == b"RIFF" and payload[8:12] == b"WAVE" and len(payload) <= MAX_MEDIA_BYTES:
                    asset.payload, asset.mime = payload, "audio/wav"
                    media_store._prune()
                    return asset
        except Exception:
            pass
    if asset.kind == "image":
        from types import SimpleNamespace

        from .utils import _image_url
        url = await _image_url(SimpleNamespace(data=data), context.bot, context.event) or url
    if not url or urlsplit(str(url)).scheme not in {"http", "https"}:
        if not file_id:
            raise MediaError("该附件缺少可读取的文件标识。")
        attempts = [("get_file", {"file_id": file_id})]
        if (group_id := getattr(context.event, "group_id", None)) is not None:
            attempts.append(("get_group_file_url", {"group_id": group_id, "file_id": file_id,
                **({"busid": data["busid"]} if "busid" in data else {})}))
        else:
            attempts.append(("get_private_file_url", {"user_id": int(context.event.get_user_id()), "file_id": file_id,
                **({"file_hash": data["file_hash"]} if data.get("file_hash") else {})}))
        for api, params in attempts:
            try:
                result = await _call(context, api, **params)
                candidate = result.get("url") or result.get("file_url")
                if candidate and urlsplit(str(candidate)).scheme in {"http", "https"}:
                    url = candidate
                    break
            except Exception:
                continue
    if not url:
        raise MediaError("QQ 框架未返回可下载地址；该附件目前无法读取。")
    payload, mime = await download_media(str(url))
    if asset.kind == "audio" and (payload.startswith(b"#!SILK") or payload.startswith(b"\x02#!SILK")):
        raise MediaError("这条QQ语音仍为SILK格式，框架转码未成功；本次没有按MP3提交模型。")
    asset.payload = payload
    asset.mime = ((mime if mime != "application/octet-stream" else "")
                  or mimetypes.guess_type(asset.name)[0] or "application/octet-stream")
    media_store._prune()
    return asset

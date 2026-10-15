import asyncio
from collections.abc import Mapping
import inspect
from os import listdir
from pathlib import Path
from random import choice
import re
from traceback import format_exc
from types import SimpleNamespace
from typing import Annotated, get_args, get_origin, get_type_hints
from urllib.parse import urlsplit

import aiohttp
import nonebot
from nonebot.log import logger

from .compat import timeout as timeout_scope
from .config import config_parser, config_path
from .member_cache import member_name_cache
from .onebot_facade import (
    bot_self_id,
    event_group_id,
    event_message,
    event_reply_message_id,
    event_sender_name,
    event_user_id,
    message_segments,
    onebot_protocol,
)

try:
    import tomllib
except ImportError:
    import tomli as tomllib

Bot_NICKNAME: str = next(iter(nonebot.get_driver().config.nickname))  # bot的nickname
# 表情包名字缓存
_emotions_cache = None

_DEFAULT_REPLIES = {
    "hello": [
        "你好喵~",
        "呜喵..？！",
        "你好OvO",
        "喵呜 ~ ，叫{bot_name}做什么呢☆",
        "怎么啦qwq",
        "呜喵 ~ ，干嘛喵？",
        "呼喵 ~ 叫可爱的咱有什么事嘛OvO",
    ],
    "poke": [
        "嗯？",
        "戳我干嘛qwq",
        "呜喵？",
        "喵！",
        "请不要戳{bot_name} >_<",
    ],
}


def invalidate_resource_caches() -> None:
    global _emotions_cache
    _emotions_cache = None


# 戳和hello消息
def get_reply_messages(reply_type: str) -> list:
    """获取回复消息，reply_type 可选 'hello' 或 'poke'"""
    from .runtime_snapshot import runtime_snapshots

    snapshot = runtime_snapshots.active()
    if snapshot is not None:
        replies = snapshot.replies.get(reply_type, ("喵？",))
        return [reply.replace("{bot_name}", Bot_NICKNAME) for reply in replies]

    reply_file_path = config_path / "replies.toml"

    # 如果文件不存在，则自动创建并写入默认文案
    if not reply_file_path.exists():
        default_toml_content = """# 机器人回复文案配置
# 可在文案中使用 {bot_name} 作为机器人昵称的占位符

hello = [
    "你好喵~",
    "呜喵..？！",
    "你好OvO",
    "喵呜 ~ ，叫{bot_name}做什么呢☆",
    "怎么啦qwq",
    "呜喵 ~ ，干嘛喵？",
    "呼喵 ~ 叫可爱的咱有什么事嘛OvO"
]

poke = [
    "嗯？",
    "戳我干嘛qwq",
    "呜喵？",
    "喵！",
    "呜...不要用力戳咱...好疼>_<",
    "请不要戳{bot_name} >_<",
    "放手啦，不给戳QAQ",
    "喵 ~ ！ 戳{bot_name}干嘛喵！",
    "戳坏了，你赔！",
    "呜......戳坏了",
    "呜呜......不要乱戳",
    "喵喵喵？OvO",
    "(。´・ω・)ん?",
    "怎么了喵？",
    "呜喵！......不许戳 (,,• ₃ •,,)",
    "有什么吩咐喵？",
    "啊呜 ~ ",
    "呼喵 ~ 叫可爱的咱有什么事嘛OvO"
]
"""
        try:
            with open(reply_file_path, "w", encoding="utf-8") as f:
                f.write(default_toml_content)
        except Exception as e:
            logger.warning(f"创建 replies.toml 失败: {e}")

        # 写入失败或创建默认值后，直接使用兜底字典
        replies_dict = _DEFAULT_REPLIES
    else:
        # 文件存在则读取，tomllib 需要用 "rb" 模式
        try:
            with open(reply_file_path, "rb") as f:
                replies_dict = tomllib.load(f)
        except Exception as e:
            logger.warning(f"读取 replies.toml 失败: {e}")
            replies_dict = {"hello": ["你好喵~"], "poke": ["嗯？"]}  # 兜底回复

    # 获取对应类型的列表
    replies = replies_dict.get(reply_type, ["喵？"])

    # 动态将 {bot_name} 占位符替换为真实的机器人昵称
    return [reply.replace("{bot_name}", Bot_NICKNAME) for reply in replies]


def load_replies_candidate() -> dict[str, tuple[str, ...]]:
    path = config_path / "replies.toml"
    if not path.exists():
        data = _DEFAULT_REPLIES
    else:
        with path.open("rb") as file:
            data = tomllib.load(file)
    result: dict[str, tuple[str, ...]] = {}
    for key in ("hello", "poke"):
        values = data.get(key)
        if not isinstance(values, list) or not values or not all(isinstance(value, str) and value for value in values):
            raise ValueError(f"replies.toml: {key} 必须是非空字符串数组")
        result[key] = tuple(values)
    return result


def load_emotions_candidate(config: dict) -> tuple[str, ...]:
    if not config.get("emotions_enabled"):
        return ()
    directory = Path(str(config.get("emotions_dir") or ""))
    if not directory.is_dir():
        raise ValueError(f"表情目录不可用: {directory}")
    return tuple(sorted(item.name for item in directory.iterdir()))


def parse_emotion(text: str) -> tuple:
    """
    解析并剥离文本中的有效表情包，保留非表情包的中括号内容。
    """
    # 1. 获取当前系统真实存在的表情包名称列表
    valid_emotions = get_emotions_names()
    extracted_emotions = []

    # 2. 定义替换回调函数
    def replacer(match):
        name = match.group(1)
        # 校验：如果括号内的名字在图库中存在
        if name in valid_emotions:
            extracted_emotions.append(name)
            return ""  # 确认为表情包，从原文本中剥离（替换为空）

        # 校验失败：说明是普通中括号文本（如 [图片]），原样保留返回
        return match.group(0)

    # 3. 执行正则替换
    pattern = r"\[(.*?)\]"
    replaced_text = re.sub(pattern, replacer, text)

    return replaced_text, extracted_emotions


# 获取表情包名字列表
def get_emotions_names() -> list:
    from .runtime_snapshot import runtime_snapshots

    snapshot = runtime_snapshots.active()
    if snapshot is not None:
        return list(snapshot.emotions)
    global _emotions_cache
    if _emotions_cache is None:
        try:
            # 初次调用时读取磁盘并缓存
            _emotions_cache = listdir(config_parser.get_config("emotions_dir"))
        except OSError:
            logger.warning(f"读取表情包目录失败:\n{format_exc()}")
            _emotions_cache = []

    return _emotions_cache


# 获取具体表情包
def get_emotion(
    emoji_name: str,
    *,
    protocol: str = "onebot_v11",
):
    if protocol == "onebot_v12":
        # Optional local emotions are paths/bytes, not v12 implementation-owned
        # file_id values.  Skip the attachment without failing the main reply.
        return None
    path = Path(config_parser.get_config("emotions_dir")) / emoji_name
    emotion_image_list = list(path.glob("*"))
    if not emotion_image_list:
        return None
    image = path / choice(emotion_image_list)
    try:
        with open(image, "rb") as f:
            img = f.read()
            from nonebot.adapters.onebot.v11 import MessageSegment

            return MessageSegment.image(img)
    except OSError:
        logger.warning(format_exc())
        return None


# 消息格式转换
def format_context_message(event) -> dict:
    """Extract ordinary group context without OneBot API or reply lookups."""
    text_message = []
    nested_self = getattr(event, "self", None)
    self_id = getattr(event, "self_id", None) or getattr(
        nested_self,
        "user_id",
        None,
    )
    for segment in message_segments(event):
        if segment.type == "text":
            text_message.append(segment.data.get("text", ""))
        elif segment.type == "image":
            text_message.append("[图片]")
        elif segment.type in {"at", "mention"}:
            qq = str(segment.data.get("qq") if segment.type == "at" else segment.data.get("user_id", ""))
            if qq and qq != str(self_id):
                text_message.append(f"@{qq}")
    return {"text": text_message}


def _image_http_url(value) -> str | None:
    if not isinstance(value, str) or len(value) > 2048:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password:
            return value
    except ValueError:
        pass
    return None


async def _image_url(segment, bot, event) -> str | None:
    data = segment.data
    url = _image_http_url(data.get("url")) or _image_http_url(data.get("file"))
    # NT QQ URLs carry expiring download credentials. get_image refreshes
    # them in SnowLuma/NapCat, even when the quoted message already has a URL.
    refresh = url is not None and urlsplit(url).hostname in {"multimedia.nt.qq.com.cn", "multimedia.nt.qq.com"}
    if url and not refresh:
        return url
    reference = data.get("file_id") or data.get("file") or url
    if not isinstance(reference, str) or not reference or len(reference) > 512 or bot is None:
        return url
    try:
        async with timeout_scope(3):
            if onebot_protocol(bot, event) == "onebot_v12":
                result = await bot.call_api("get_file", file_id=reference, type="url")
            else:
                result = await bot.call_api("get_image", file=reference)
        if isinstance(result, Mapping):
            return _image_http_url(result.get("url")) or url
    except Exception:
        # An optional image lookup must not suppress the user's text request.
        logger.debug("消息图片地址解析失败，保留未解析标记")
    return url


async def _message_reply(event, bot):
    reply = getattr(event, "reply", None)
    if reply is not None and getattr(reply, "message", None) is not None:
        return reply
    reply_id = event_reply_message_id(event)
    if reply_id and bot is not None and onebot_protocol(bot, event) != "onebot_v12":
        try:
            async with timeout_scope(3):
                data = await bot.call_api("get_msg", message_id=int(reply_id))
            if isinstance(data, Mapping) and data.get("message") is not None:
                from nonebot.adapters.onebot.v11 import Message, MessageSegment

                sender = data.get("sender") or {}
                raw = data["message"]
                message = Message(MessageSegment(item["type"], item["data"]) if isinstance(item, Mapping) else item
                                  for item in raw) if isinstance(raw, list) else Message(raw)
                return SimpleNamespace(message=message, message_id=reply_id,
                                       sender=SimpleNamespace(**sender))
        except Exception:
            logger.debug("引用消息读取失败，继续处理当前消息")
    return reply


async def format_message(event, bot) -> dict:
    text_message = []
    reply_text = ""
    image_urls = []
    image_file_ids = []
    image_segments = []
    mentions = []
    reply_user = None
    current_user_id = event_user_id(event)
    current_user = {
        "qq": current_user_id,
        "name": event_sender_name(event),
    }

    # 1. 处理回复消息
    if reply := await _message_reply(event, bot):
        reply_segments = []
        reply_message = getattr(reply, "message", None)
        if reply_message is not None:
            for seg in reply_message:
                if seg.type == "text":
                    reply_segments.append(seg.data.get("text", ""))
                elif seg.type == "image":
                    reply_segments.append("[图片]")
                elif seg.type in {"at", "mention"}:
                    reply_segments.append("[提及]")
                elif seg.type in {"forward", "node", "json", "file", "video", "record", "audio"}:
                    reply_segments.append(f"[引用{seg.type}：可用read_message_content读取]")
            for seg in reply_message:
                if seg.type != "image":
                    continue
                image_segments.append(("reply", seg))
        else:
            reply_id = getattr(reply, "message_id", None)
            if reply_id is not None:
                reply_segments.append(f"[回复消息 {reply_id}]")
        reply_text = "".join(reply_segments).strip()
        reply_sender = getattr(reply, "sender", None)
        reply_user_id = str(getattr(reply_sender, "user_id", None) or getattr(reply, "user_id", ""))
        reply_name = getattr(reply_sender, "card", None) or getattr(reply_sender, "nickname", None)
        if not reply_name and reply_user_id and event_group_id(event) is not None:
            reply_name = await get_member_name(
                event_group_id(event),
                reply_user_id,
                bot,
            )
        reply_user = {
            "qq": reply_user_id,
            "name": reply_name or reply_user_id or "被引用者",
        }

    # 2. 处理当前消息
    group_id = event_group_id(event)
    for msgseg in event_message(event):
        if msgseg.type in {"at", "mention"}:
            qq = str(msgseg.data.get("qq") if msgseg.type == "at" else msgseg.data.get("user_id"))
            if qq != bot_self_id(bot, event):
                name = await get_member_name(group_id, qq, bot) if group_id is not None else qq
                mentions.append({"qq": qq, "name": name})
                # Keep the identity token at the original position. A name
                # alone makes a mentioned account look like literal text.
                text_message.append(f"[CQ:at,qq={qq}]")
        elif msgseg.type == "image":
            text_message.append("[图片]")
            image_segments.append(("current", msgseg))
        elif msgseg.type == "face":
            pass
        elif msgseg.type in {"forward", "node", "json", "file", "video", "record", "audio"}:
            text_message.append(f"[{msgseg.type}素材：可用read_message_content读取]")
        elif msgseg.type == "text":
            if plain := msgseg.data.get("text", ""):
                # Strip only the standalone, case-insensitive wake word.  Text
                # such as ``airpods`` or ``ai助手`` is ordinary user content.
                wake_word = re.match(r"(?i)^ai(?=$|[\s\u3000])", plain)
                if wake_word is not None:
                    plain = plain[wake_word.end() :].lstrip()
                text_message.append(plain)

    image_sources = []
    resolved = await asyncio.gather(*(_image_url(seg, bot, event) for _, seg in image_segments[:8]))
    for (origin, segment), url in zip(image_segments[:8], resolved):
        if url:
            image_urls.append(url)
            image_sources.append({"source": origin, "url": url})
        elif file_id := segment.data.get("file_id"):
            image_file_ids.append(str(file_id))
    return {
        "text": text_message,
        "reply": reply_text,
        "images": image_urls,
        "image_file_ids": image_file_ids,
        "image_sources": image_sources,
        "unresolved_images": len(image_segments) - len(image_sources),
        "mentions": mentions,
        "reply_user": reply_user,
        "current_user": current_user,
    }


async def get_member_name(
    group: int | str,
    sender_id: int | str,
    bot,
) -> str:  # 将QQ号转换成昵称
    return await member_name_cache.get(bot, group, sender_id)


def build_schema_from_func(func) -> dict:
    """
    动态解析异步函数，自动生成 LLM 工具所需的 Schema 字典。
    通过 typing.Annotated 提取参数描述，通过 docstring 提取工具描述。
    """
    tool_name = func.__name__
    # 直接使用整段 docstring 作为工具描述，无需正则解析
    tool_desc = inspect.getdoc(func) or "未提供功能描述"

    sig = inspect.signature(func)
    try:
        # 必须加 include_extras=True 才能保留 Annotated 里的元数据
        type_hints = get_type_hints(func, include_extras=True)
    except Exception:
        type_hints = {}

    properties = {}
    required = []

    # Python 类型映射到 JSON Schema 类型
    type_map = {
        str: "string",
        int: "integer",
        float: "number",
        bool: "boolean",
        list: "array",
        dict: "object",
    }

    for param_name, param in sig.parameters.items():
        if param_name in ("self", "cls") or param_name.startswith("_"):
            continue

        hint = type_hints.get(param_name, str)
        param_desc = f"参数 {param_name}"
        param_type = str

        # 解析 Annotated[Type, "描述"]
        if get_origin(hint) is Annotated:
            args = get_args(hint)
            param_type = args[0]  # 真实类型 (如 str)
            # 遍历元数据，寻找字符串作为描述
            for metadata in args[1:]:
                if isinstance(metadata, str):
                    param_desc = metadata
                    break
        else:
            param_type = hint

        # 映射类型，若不在 type_map 中则默认 fallback 到 "string"
        json_type = type_map.get(param_type, "string")

        properties[param_name] = {"type": json_type, "description": param_desc}

        # 没有默认值即为必填参数
        if param.default == inspect.Parameter.empty:
            required.append(param_name)

    return {
        "name": tool_name,
        "description": tool_desc,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
        },
        "func": func,
    }


# ── 全局 aiohttp session ──────────────────────────────────────────────────────

_session: aiohttp.ClientSession | None = None


def get_session() -> aiohttp.ClientSession:
    if _session is None or _session.closed:
        raise RuntimeError("HTTP session not initialized")
    return _session


async def init_session() -> None:
    global _session
    _session = aiohttp.ClientSession()


async def close_session() -> None:
    global _session
    if _session and not _session.closed:
        await _session.close()
        _session = None

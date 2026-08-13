import inspect
from os import listdir
from pathlib import Path
from random import choice
import re
from traceback import format_exc
from typing import Annotated, get_args, get_origin, get_type_hints

import aiohttp
import nonebot
from nonebot.adapters.onebot.v11 import MessageSegment
from nonebot.log import logger

from .config import config_parser, config_path
from .member_cache import member_name_cache

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
        if not isinstance(values, list) or not values or not all(
            isinstance(value, str) and value for value in values
        ):
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
def get_emotion(emoji_name: str) -> MessageSegment:
    path = Path(config_parser.get_config("emotions_dir")) / emoji_name
    emotion_image_list = list(path.glob("*"))
    if not emotion_image_list:
        return None
    image = path / choice(emotion_image_list)
    try:
        with open(image, "rb") as f:
            img = f.read()
            return MessageSegment.image(img)
    except OSError:
        logger.warning(format_exc())
        return None


# 消息格式转换
def format_context_message(event) -> dict:
    """Extract ordinary group context without OneBot API or reply lookups."""
    text_message = []
    for segment in event.get_message():
        if segment.type == "text":
            text_message.append(segment.data.get("text", ""))
        elif segment.type == "image":
            text_message.append("[图片]")
        elif segment.type == "at":
            qq = str(segment.data.get("qq", ""))
            if qq and qq != str(event.self_id):
                text_message.append(f"@{qq}")
    return {"text": text_message}


async def format_message(event, bot) -> dict:
    text_message = []
    reply_text = ""
    image_urls = []
    mentions = []
    reply_user = None
    sender = event.sender
    current_user = {
        "qq": str(getattr(sender, "user_id", getattr(event, "user_id", ""))),
        "name": getattr(sender, "card", None)
        or getattr(sender, "nickname", None)
        or str(getattr(sender, "user_id", getattr(event, "user_id", ""))),
    }

    # 1. 处理回复消息
    if reply := getattr(event, "reply", None):
        reply_segments = []
        for seg in event.reply.message:
            if seg.type == "text":
                reply_segments.append(seg.data.get("text", ""))
            elif seg.type == "image":
                reply_segments.append("[图片]")
            elif seg.type == "at":
                reply_segments.append("[提及]")
        reply_text = "".join(reply_segments).strip()
        reply_user = {
            "qq": str(getattr(reply.sender, "user_id", "")),
            "name": reply.sender.card or reply.sender.nickname,
        }

        for seg in reply.message:
            if seg.type == "image" and (url := seg.data.get("url")):
                image_urls.append(url)

    # 2. 处理当前消息
    for msgseg in event.get_message():
        if msgseg.type == "at":
            qq = str(msgseg.data.get("qq"))
            if qq != str(bot.self_id):
                name = await get_member_name(event.group_id, qq, bot)
                mentions.append({"qq": qq, "name": name})
                text_message.append(name)
        elif msgseg.type == "image":
            text_message.append("[图片]")
            if url := msgseg.data.get("url"):
                image_urls.append(url)
        elif msgseg.type == "face":
            pass
        elif msgseg.type == "text":
            if plain := msgseg.data.get("text", ""):
                if plain.startswith("ai"):
                    text_message.append(plain[2:])
                else:
                    text_message.append(plain)

    return {
        "text": text_message,
        "reply": reply_text,
        "images": image_urls,
        "mentions": mentions,
        "reply_user": reply_user,
        "current_user": current_user,
    }


async def get_member_name(group: int, sender_id: int, bot) -> str:  # 将QQ号转换成昵称
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

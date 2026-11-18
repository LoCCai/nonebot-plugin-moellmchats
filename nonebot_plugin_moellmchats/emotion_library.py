"""Optional local meme-manager integration; no extra model or send calls."""
from __future__ import annotations

import sys

from nonebot.log import logger

from .config import config_parser


def enabled() -> bool:
    return config_parser.get_config("emotion_source", "directory") == "meme_manager"


def _library():
    # Do not import a NoneBot plugin behind the loader's back. Missing manager
    # means skip this optional attachment, never fall back to an unrelated pack.
    if "nonebot_plugin_meme_manager" not in sys.modules:
        return None
    from nonebot_plugin_meme_manager.nb2 import library

    return library


def catalog(bot=None, event=None) -> dict:
    try:
        library = _library()
        return library.category_catalog(bot=bot, event=event) if library else {"categories": {}, "hints": ""}
    except Exception as exc:
        logger.warning("表情图库暂不可用，跳过随文表情：{}", type(exc).__name__)
        return {"categories": {}, "hints": ""}


def image(category: str, *, bot=None, event=None, protocol="onebot_v11"):
    if protocol != "onebot_v11":
        return None
    try:
        library = _library()
        path = library.pick_category_image(category, bot=bot, event=event) if library else None
        if path is None:
            return None
        with path.open("rb") as stream:
            payload = stream.read(library.MAX_IMAGE_BYTES + 1)
        if not payload or len(payload) > library.MAX_IMAGE_BYTES:
            return None
        from nonebot.adapters.onebot.v11 import MessageSegment

        return MessageSegment.image(payload)
    except Exception as exc:
        logger.warning("表情图片不可用，跳过附件：{}", type(exc).__name__)
        return None

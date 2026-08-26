from __future__ import annotations

from typing import TYPE_CHECKING

from nonebot.log import logger

if TYPE_CHECKING:
    from .runtime_reload import ReloadResult


async def reload_tools_for_commands() -> ReloadResult:
    """Reload the runtime snapshot for an administrative command.

    Command handlers need the complete result to report the generation that was
    actually published.  A failed atomic reload must remain a failure: callers
    decide how to explain that the previous generation is still active.
    """

    from .runtime_reload import runtime_reloader

    try:
        return await runtime_reloader.reload("tool-command")
    except Exception:
        logger.exception("工具原子重载失败")
        raise

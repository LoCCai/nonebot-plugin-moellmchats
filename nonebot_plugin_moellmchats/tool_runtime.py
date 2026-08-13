from nonebot.log import logger


async def reload_tools_for_commands() -> tuple[int, int]:
    from .runtime_reload import runtime_reloader

    try:
        result = await runtime_reloader.reload("tool-command")
    except Exception:
        logger.exception("工具原子重载失败")
        return 1, 0
    return 0, result.mcp_tools

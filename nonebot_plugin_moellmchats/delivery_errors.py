"""Describe adapter delivery errors without replaying an uncertain send."""

from nonebot.exception import ActionFailed, ApiNotAvailable, NetworkError
from nonebot.log import logger


async def finish_local_reply(matcher, message) -> None:
    """Finish a greeting without replaying an adapter failure or starting the model."""
    try:
        await matcher.send(message)
    except (ActionFailed, ApiNotAvailable, NetworkError) as error:
        logger.warning("OneBot 本地回复投递结束，未自动重发：{}", reply_delivery_failure(error))
    await matcher.finish()


def reply_delivery_failure(error: BaseException) -> dict[str, object]:
    info = getattr(error, "info", {})
    info = info if isinstance(info, dict) else {}
    wording = getattr(error, "wording", None) or info.get("wording") or str(error)
    unknown = isinstance(error, NetworkError) or any(
        marker in str(wording).lower()
        for marker in (
            "timed out",
            "timeout",
            "stream client disconnected",
            "connection closed",
            "client closed",
            "client disconnected",
            "socket closed",
            "stale socket",
            "epipe",
            "econnreset",
            # Some implementations persist metadata after QQ accepts the send.
            # A storage error alone cannot prove that nothing was delivered.
            "sqlite_busy",
            "sqlite_locked",
            "database is locked",
        )
    )
    return {
        "error_type": type(error).__name__,
        "retcode": getattr(error, "retcode", None) or info.get("retcode"),
        "result_unknown": unknown,
        "retry": False,
    }

"""Describe adapter delivery errors without replaying an uncertain send."""

from nonebot.exception import NetworkError


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
        )
    )
    return {
        "error_type": type(error).__name__,
        "retcode": getattr(error, "retcode", None) or info.get("retcode"),
        "result_unknown": unknown,
        "retry": False,
    }

from __future__ import annotations

from collections.abc import Mapping
import json
import re

from aiohttp import ClientError

from .compat import TimeoutError, timeout

_CONTENT_POLICY_IDENTIFIERS = frozenset(
    {
        "content_blocked",
        "content_filter",
        "content_filter_error",
        "content_policy_violation",
        "data_inspection_failed",
        "datainspectionfailed",
        "moderation_blocked",
        "prohibited_content",
        "safety_violation",
        "sensitive_content",
    }
)


def normalize_error_identifier(value: object) -> str:
    """Normalize one provider code/type without inspecting free-form text."""

    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return ""
    return re.sub(r"[^a-z0-9]+", "_", str(value).casefold()).strip("_")


def is_content_policy_error_info(info: Mapping[str, object]) -> bool:
    """Match only exact normalized structured ``code`` or ``type`` values."""

    if not isinstance(info, Mapping):
        return False
    return any(
        normalize_error_identifier(info.get(field)) in _CONTENT_POLICY_IDENTIFIERS
        for field in ("code", "type")
    )


def is_quota_exhaustion_error_info(status: int, payload: Mapping) -> bool:
    """Recognize explicit billing/quota failures, never a generic forbidden error."""
    if status not in {402, 403}:
        return False
    info = payload.get("error", payload)
    if not isinstance(info, Mapping) or is_content_policy_error_info(info):
        return False
    identifiers = {normalize_error_identifier(info.get(field)) for field in ("code", "type")}
    quota_codes = {
        "allocationquota_freetieronly", "allocationquota_quotaexceeded", "allocation_quota_free_tier_only",
        "freetierexhausted", "free_tier_exhausted", "quotaexhausted", "free_quota_exhausted",
        "quota_exhausted", "insufficient_quota", "insufficient_balance", "insufficientbalance",
        "arrearage", "billing_hard_limit_reached",
    }
    if identifiers & quota_codes:
        return True
    if identifiers - {"", "402", "403", "forbidden", "quota_error", "billing_error"}:
        return False
    message = info.get("message")
    return isinstance(message, str) and message.strip().casefold().startswith((
        "free quota exhausted", "free tier quota exhausted", "insufficient quota", "insufficient balance",
        "免费额度已用尽", "免费额度已耗尽", "余额不足",
    ))


def is_quota_exhaustion_error(status: int, body: str) -> bool:
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return False
    return isinstance(payload, Mapping) and is_quota_exhaustion_error_info(status, payload)


async def read_bounded_error(response) -> str:
    """Only quota/forbidden responses need this bounded body inspection."""
    try:
        async with timeout(1.5):
            reader = getattr(getattr(response, "content", None), "read", None)
            if callable(reader):
                raw = bytearray()
                while len(raw) <= 8192:
                    chunk = await reader(8193 - len(raw))
                    if not chunk:
                        return raw.decode("utf-8", errors="replace")
                    raw.extend(chunk)
                return ""
            # Lightweight adapter fakes may expose only text().
            return (await response.text())[:8192]
    except (TimeoutError, OSError, ClientError):
        return ""


__all__ = ["is_content_policy_error_info", "is_quota_exhaustion_error", "is_quota_exhaustion_error_info",
           "normalize_error_identifier", "read_bounded_error"]

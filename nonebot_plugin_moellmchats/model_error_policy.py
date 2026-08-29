from __future__ import annotations

from collections.abc import Mapping
import re

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


__all__ = ["is_content_policy_error_info", "normalize_error_identifier"]

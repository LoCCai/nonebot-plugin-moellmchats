from __future__ import annotations

import pytest

from nonebot_plugin_moellmchats import _parse_llm_cooldown_seconds
from nonebot_plugin_moellmchats.config import MAX_CD_SECONDS


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0", 0),
        ("00000", 0),
        (" 30 ", 30),
        ("00030", 30),
        (str(MAX_CD_SECONDS), MAX_CD_SECONDS),
    ],
)
def test_parse_llm_cooldown_seconds(raw: str, expected: int) -> None:
    assert _parse_llm_cooldown_seconds(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "-1",
        "+1",
        "1.5",
        "０",
        "1 秒",
        str(MAX_CD_SECONDS + 1),
        "9" * 5000,
    ],
)
def test_parse_llm_cooldown_seconds_rejects_invalid_input(raw: str) -> None:
    with pytest.raises(ValueError, match="0 到"):
        _parse_llm_cooldown_seconds(raw)

"""Parse explicit confirmation messages independently of command prefixes."""

import re

from nonebot.adapters import Event

_START = re.compile(r"^[/!！]?(确认执行|取消执行)(?:\s|$)")
_PART = re.compile(r"(?:[/!！]?(确认执行|取消执行)\s+)?([0-9A-Fa-f]{6})(?=\s|$)")


async def confirmation_requested(event: Event) -> bool:
    return bool(_START.match(event.get_plaintext().strip()))


def parse_confirmation(text: str) -> tuple[str, list[str]]:
    text = text.strip()
    start = _START.match(text)
    if not start:
        raise ValueError("格式：确认执行 <6位确认码>，或取消执行 <6位确认码>")
    action = start[1]
    tail = text[start.end():].strip()
    codes = []
    while tail:
        part = _PART.match(tail)
        if not part or (part[1] and part[1] != action):
            raise ValueError(f"格式：{action} <6位确认码>；多个确认码用空格或换行分开")
        codes.append(part[2].upper())
        tail = tail[part.end():].strip()
        if len(codes) > 10:
            raise ValueError("每次最多处理 10 个确认码")
    if not codes:
        raise ValueError(f"格式：{action} <6位确认码>")
    return action, list(dict.fromkeys(codes))

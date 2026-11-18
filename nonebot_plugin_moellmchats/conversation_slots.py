"""One in-flight conversation per account across scenes and runtime generations."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
import re
import time


class ConversationSlots:
    """Process-wide ownership, independent of cooldowns and clear-memory commands.

    New input is rejected while its account is busy. Already durable recovery
    work waits for the current owner instead of being discarded as a duplicate.
    All mutations run synchronously on the bot event loop, including cleanup.
    """

    def __init__(self) -> None:
        self._owners: dict[str, asyncio.Event] = {}
        self._activity: dict[str, tuple[float, str]] = {}
        self._current = ContextVar("moellm_conversation_owner", default=None)

    def busy_message(self, user_id: int | str) -> str:
        key = str(user_id).strip()
        if key.isascii() and key.isdecimal():
            key = key.lstrip("0") or "0"
        started, description = self._activity.get(key, (time.monotonic(), "正在生成回复"))
        elapsed = max(0, int(time.monotonic() - started))
        return f"七七{description}，已用时 {elapsed} 秒。请等待本次结果，无需重复发送。"

    @asynccontextmanager
    async def activity(self, command: str):
        lease = self._current.get()
        if lease is None:
            yield
            return
        key, owner = lease
        if self._owners.get(key) is not owner:
            yield
            return
        head = command.split(maxsplit=1)[0] if command.strip() else ""
        if re.fullmatch(r"[/!！]?[\w\u3400-\u9fff]{1,30}", head) is None:
            head = "插件指令"
        previous = self._activity[key]
        current = (previous[0], f"正在执行 {head}（查询、渲染或发送）")
        self._activity[key] = current
        try:
            yield
        finally:
            if self._owners.get(key) is owner and self._activity.get(key) == current:
                self._activity[key] = previous

    @asynccontextmanager
    async def slot(self, user_id: int | str, *, wait: bool = False) -> AsyncIterator[bool]:
        if isinstance(user_id, bool) or not isinstance(user_id, (int, str)):
            raise ValueError("LLM 对话缺少有效的用户 ID")
        key = str(user_id).strip()
        if not key or len(key) > 512 or any(ord(char) < 32 or ord(char) == 127 for char in key):
            raise ValueError("LLM 对话缺少有效的用户 ID")
        if key.isascii() and key.isdecimal():
            key = key.lstrip("0") or "0"
        while (owner := self._owners.get(key)) is not None:
            if not wait:
                yield False
                return
            await owner.wait()

        owner = asyncio.Event()
        self._owners[key] = owner
        self._activity[key] = (time.monotonic(), "正在生成回复")
        token = self._current.set((key, owner))
        try:
            yield True
        finally:
            # No await: repeated cancellation cannot strand the account's slot.
            if self._owners.get(key) is owner:
                self._owners.pop(key)
                self._activity.pop(key, None)
                owner.set()
            self._current.reset(token)


conversation_slots = ConversationSlots()

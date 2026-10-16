"""One in-flight conversation per account across scenes and runtime generations."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class ConversationSlots:
    """Process-wide ownership, independent of cooldowns and clear-memory commands.

    New input is rejected while its account is busy. Already durable recovery
    work waits for the current owner instead of being discarded as a duplicate.
    All mutations run synchronously on the bot event loop, including cleanup.
    """

    def __init__(self) -> None:
        self._owners: dict[str, asyncio.Event] = {}

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
        try:
            yield True
        finally:
            # No await: repeated cancellation cannot strand the account's slot.
            if self._owners.get(key) is owner:
                self._owners.pop(key)
                owner.set()


conversation_slots = ConversationSlots()

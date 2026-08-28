from __future__ import annotations

import asyncio
import builtins

import pytest

from nonebot_plugin_moellmchats.compat import TimeoutError, timeout


def test_compat_timeout_error_is_builtin_identity() -> None:
    # compat.timeout 在所有支持版本上都抛内建 TimeoutError；
    # 3.10 上 asyncio.TimeoutError 是另一个类，聊天链路必须统一用本别名
    assert TimeoutError is builtins.TimeoutError
    assert TimeoutError is not asyncio.CancelledError


@pytest.mark.asyncio
async def test_timeout_raises_compat_timeout_error() -> None:
    with pytest.raises(TimeoutError):
        async with timeout(0.01):
            await asyncio.sleep(1)


@pytest.mark.asyncio
async def test_timeout_none_never_expires() -> None:
    async with timeout(None):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_timeout_passes_through_external_cancellation() -> None:
    task = asyncio.create_task(_sleep_in_timeout())
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def _sleep_in_timeout() -> None:
    async with timeout(5):
        await asyncio.sleep(5)

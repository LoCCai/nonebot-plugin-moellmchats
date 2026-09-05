from __future__ import annotations

import asyncio
import builtins

import pytest

from nonebot_plugin_moellmchats.compat import TimeoutError, timeout


def test_compat_timeout_error_is_builtin_identity() -> None:
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
    async def wait() -> None:
        async with timeout(5):
            await asyncio.sleep(5)

    task = asyncio.create_task(wait())
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

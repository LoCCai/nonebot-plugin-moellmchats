from __future__ import annotations

import asyncio
import builtins
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

# ``timeout`` below consistently raises the built-in exception.  On Python
# 3.10 ``asyncio.TimeoutError`` is still a distinct class, so chat callers must
# import this alias instead of guessing which timeout primitive raised.
TimeoutError = builtins.TimeoutError


@dataclass(frozen=True)
class SettledAwaitable:
    """Outcome of a cleanup awaitable protected from repeated caller cancellation."""

    error: BaseException | None
    interrupted: bool


async def settle_awaitable(awaitable: Awaitable[Any]) -> SettledAwaitable:
    """Let one cleanup operation finish even if its caller is cancelled again.

    ``asyncio.shield`` alone returns immediately on caller cancellation.  The
    loop below keeps settling the same child task, so a second or later cancel
    cannot skip the next cleanup step (for example ``session.close()``).
    Cancellation is reported to the caller instead of being silently lost.
    """

    if not hasattr(awaitable, "__await__"):
        raise TypeError("settle_awaitable 需要 awaitable")
    task = asyncio.ensure_future(awaitable)
    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # A cancelled child is its own cleanup error.  A still-running
            # child, or a completed non-cancelled child racing with caller
            # cancellation, means the caller was interrupted.
            if not task.done() or not task.cancelled():
                interrupted = True
            continue
        except BaseException:
            break
    try:
        task.result()
    except BaseException as error:
        return SettledAwaitable(error=error, interrupted=interrupted)
    return SettledAwaitable(error=None, interrupted=interrupted)

if hasattr(asyncio, "timeout"):
    timeout = getattr(asyncio, "timeout")
else:  # pragma: no cover - exercised by the Python 3.10 CI job

    @asynccontextmanager
    async def timeout(delay: float | None) -> AsyncIterator[None]:
        """Small asyncio.timeout compatibility layer for Python 3.10."""
        if delay is None:
            yield
            return
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("timeout context requires an asyncio task")
        loop = asyncio.get_running_loop()
        expired = False

        def cancel() -> None:
            nonlocal expired
            expired = True
            task.cancel()

        handle = loop.call_later(delay, cancel)
        try:
            yield
        except asyncio.CancelledError as error:
            if expired:
                raise TimeoutError from error
            raise
        finally:
            handle.cancel()

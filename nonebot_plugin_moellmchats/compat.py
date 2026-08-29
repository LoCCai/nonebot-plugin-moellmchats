from __future__ import annotations

import asyncio
import builtins
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

# ``timeout`` below consistently raises the built-in exception.  On Python
# 3.10 ``asyncio.TimeoutError`` is still a distinct class, so chat callers must
# import this alias instead of guessing which timeout primitive raised.
TimeoutError = builtins.TimeoutError

if hasattr(asyncio, "timeout"):
    timeout = asyncio.timeout
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

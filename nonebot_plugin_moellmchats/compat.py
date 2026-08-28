from __future__ import annotations

import asyncio
import builtins
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

# 本模块的 timeout() 在所有支持的 Python 版本上都抛出内建 TimeoutError
# （asyncio.TimeoutError 直到 3.11 才与内建类合一）。聊天链路必须捕获本别名，
# 否则 3.10 上超时会落进 except Exception 分支，走错文案/遥测/重试路径。
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

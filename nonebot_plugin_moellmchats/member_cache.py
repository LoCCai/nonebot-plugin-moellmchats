from __future__ import annotations

import asyncio
from collections import OrderedDict
import time
from typing import Any

from .compat import timeout as timeout_scope
from .config import config_parser
from .runtime_metrics import runtime_metrics


class MemberNameCache:
    def __init__(self) -> None:
        self._cache: OrderedDict[tuple[str, int, int], tuple[float, str]] = OrderedDict()
        self._inflight: dict[tuple[str, int, int], asyncio.Task[str]] = {}
        self._lock = asyncio.Lock()

    async def get(self, bot: Any, group_id: int, user_id: int | str) -> str:
        user_id_int = int(user_id)
        key = (str(bot.self_id), int(group_id), user_id_int)
        now = time.monotonic()
        ttl = config_parser.get_config("member_cache_ttl_seconds", 600)

        async with self._lock:
            cached = self._cache.get(key)
            if cached and now - cached[0] <= ttl:
                self._cache.move_to_end(key)
                runtime_metrics.member_cache_hits += 1
                return cached[1]
            if cached:
                self._cache.pop(key, None)
            task = self._inflight.get(key)
            if task is None:
                runtime_metrics.member_cache_misses += 1
                task = asyncio.create_task(self._fetch(bot, group_id, user_id_int))
                self._inflight[key] = task
                task.add_done_callback(
                    lambda finished, cache_key=key: asyncio.create_task(
                        self._remove_inflight(cache_key, finished)
                    )
                )

        try:
            name = await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except Exception:
            name = str(user_id_int)
        finally:
            if task.done():
                async with self._lock:
                    self._inflight.pop(key, None)

        async with self._lock:
            self._cache[key] = (time.monotonic(), name)
            self._cache.move_to_end(key)
            max_entries = config_parser.get_config("member_cache_max_entries", 4096)
            while len(self._cache) > max_entries:
                self._cache.popitem(last=False)
        return name

    async def _remove_inflight(
        self, key: tuple[str, int, int], task: asyncio.Task[str]
    ) -> None:
        async with self._lock:
            if self._inflight.get(key) is task:
                self._inflight.pop(key, None)

    async def _fetch(self, bot: Any, group_id: int, user_id: int) -> str:
        timeout = config_parser.get_config("member_lookup_timeout_seconds", 2)
        try:
            async with timeout_scope(timeout):
                member = await bot.get_group_member_info(
                    group_id=group_id,
                    user_id=user_id,
                    no_cache=False,
                )
            return str(member.get("card") or member.get("nickname") or user_id)
        except TimeoutError:
            runtime_metrics.member_lookup_timeouts += 1
            return str(user_id)
        except Exception:
            return str(user_id)

    def clear(self) -> None:
        self._cache.clear()


member_name_cache = MemberNameCache()

from __future__ import annotations

from collections import OrderedDict, deque
from collections.abc import Callable, Iterator, MutableMapping
import time
from typing import Any

from .config import config_parser


class BoundedDequeStore(MutableMapping[Any, deque]):
    """LRU/TTL bounded conversation store compatible with defaultdict usage."""

    def __init__(self, maxlen_getter: Callable[[], int]) -> None:
        self._values: OrderedDict[Any, tuple[float, deque]] = OrderedDict()
        self._maxlen_getter = maxlen_getter

    def __getitem__(self, key: Any) -> deque:
        now = time.monotonic()
        self._prune(now)
        item = self._values.get(key)
        if item is None:
            value: deque = deque(maxlen=self._maxlen_getter())
        else:
            value = item[1]
            if value.maxlen != self._maxlen_getter():
                value = deque(value, maxlen=self._maxlen_getter())
        self._values[key] = (now, value)
        self._values.move_to_end(key)
        self._evict()
        return value

    def __contains__(self, key: Any) -> bool:
        self._prune(time.monotonic())
        return key in self._values

    def __setitem__(self, key: Any, value: deque) -> None:
        self._values[key] = (time.monotonic(), value)
        self._values.move_to_end(key)
        self._evict()

    def __delitem__(self, key: Any) -> None:
        del self._values[key]

    def __iter__(self) -> Iterator[Any]:
        self._prune(time.monotonic())
        return iter(self._values)

    def __len__(self) -> int:
        self._prune(time.monotonic())
        return len(self._values)

    def get(self, key: Any, default: Any = None) -> Any:
        if key not in self._values:
            return default
        return self[key]

    def clear(self) -> None:
        self._values.clear()

    def _prune(self, now: float) -> None:
        ttl = config_parser.get_config("user_history_expire_seconds", 600)
        expired = [key for key, (seen, _) in self._values.items() if now - seen > ttl]
        for key in expired:
            self._values.pop(key, None)

    def _evict(self) -> None:
        limit = config_parser.get_config("max_context_sessions", 1000)
        while len(self._values) > limit:
            self._values.popitem(last=False)


class BoundedValueStore(MutableMapping[Any, Any]):
    """Default-producing TTL/LRU mapping for cooldown and transient user state."""

    def __init__(self, default_factory: Callable[[], Any]) -> None:
        self._values: OrderedDict[Any, tuple[float, Any]] = OrderedDict()
        self._default_factory = default_factory

    def __getitem__(self, key: Any) -> Any:
        self._prune()
        if key not in self._values:
            self[key] = self._default_factory()
        _, value = self._values[key]
        self._values[key] = (time.monotonic(), value)
        self._values.move_to_end(key)
        return value

    def __contains__(self, key: Any) -> bool:
        self._prune()
        return key in self._values

    def __setitem__(self, key: Any, value: Any) -> None:
        self._values[key] = (time.monotonic(), value)
        self._values.move_to_end(key)
        self._evict()

    def __delitem__(self, key: Any) -> None:
        del self._values[key]

    def __iter__(self) -> Iterator[Any]:
        self._prune()
        return iter(self._values)

    def __len__(self) -> int:
        self._prune()
        return len(self._values)

    def get(self, key: Any, default: Any = None) -> Any:
        if key not in self._values:
            return default
        return self[key]

    def clear(self) -> None:
        self._values.clear()

    def _prune(self) -> None:
        now = time.monotonic()
        ttl = config_parser.get_config("user_history_expire_seconds", 600)
        for key in [
            key for key, (seen, _) in self._values.items() if now - seen > ttl
        ]:
            self._values.pop(key, None)

    def _evict(self) -> None:
        limit = config_parser.get_config("max_context_sessions", 1000)
        while len(self._values) > limit:
            self._values.popitem(last=False)

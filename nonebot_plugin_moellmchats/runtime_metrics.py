from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
import time
from typing import Any


@dataclass
class RuntimeMetrics:
    started_at: float = field(default_factory=time.time)
    llm_active: int = 0
    llm_pending: int = 0
    llm_rejected: int = 0
    dispatch_active: int = 0
    dispatch_pending: int = 0
    dispatch_rejected: int = 0
    dispatch_timeouts: int = 0
    member_cache_hits: int = 0
    member_cache_misses: int = 0
    member_lookup_timeouts: int = 0
    tool_steps: int = 0
    tool_timeouts: int = 0
    generated_runner_active: int = 0
    generated_runner_pending: int = 0
    generated_runner_rejected: int = 0
    generated_runner_timeouts: int = 0
    generated_runner_killed: int = 0
    generated_runner_orphan_cleanups: int = 0
    generated_runner_failures: int = 0
    generated_authoring_active: int = 0
    classification_count: int = 0
    classification_seconds: float = 0.0
    reload_generation: int = 0
    reload_successes: int = 0
    reload_failures: int = 0
    last_reload_at: float | None = None
    last_reload_error: str | None = None
    dispatch_modes: Counter[str] = field(default_factory=Counter)

    def snapshot(self) -> dict[str, Any]:
        result = asdict(self)
        result["dispatch_modes"] = dict(self.dispatch_modes)
        return result


runtime_metrics = RuntimeMetrics()

"""Host-declared budgets shared by plugin dispatch and its enclosing tool call."""

import math

_plugin_timeouts: dict[str, float] = {}


def register_plugin_timeout(plugin_name: str, seconds: float) -> None:
    if not isinstance(plugin_name, str) or not plugin_name.strip():
        raise ValueError("plugin_name must be non-empty")
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 0 < seconds <= 900:
        raise ValueError("plugin timeout must be between 0 and 900 seconds")
    _plugin_timeouts[plugin_name] = float(seconds)


def plugin_timeout(plugin_name: str, configured: float) -> float:
    return max(float(configured), _plugin_timeouts.get(plugin_name, 0))

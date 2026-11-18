"""Short-lived classification backoff; never mark throttling as exhausted quota.

Backoff grows step-wise per consecutive rejection (429: 60/120/240/480/900s;
other transient statuses: 30/60/120/240/300s) instead of a flat retry every
60 seconds.  A bounded ``Retry-After`` is respected as a floor and stays
capped.  A confirmed success calls :func:`release` to reset the streak, so
only one probe is admitted after cooldown expiry, and a fresh
rejection starts over from the base delay.  All state is process-local and
clears on restart, matching the previous flat-cooldown contract.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
import math
import time

_cooldowns: dict[tuple[str, str], float] = {}
_streaks: dict[tuple[str, str], int] = {}
_epochs: dict[tuple[str, str], object] = {}
_probes: dict[tuple[str, str], object] = {}
_touched: dict[tuple[str, str], float] = {}

_RATE_LIMIT_BASE_SECONDS = 60.0
_RATE_LIMIT_MAX_SECONDS = 900.0
_TRANSIENT_BASE_SECONDS = 30.0
_TRANSIENT_MAX_SECONDS = 300.0
_MAX_TRACKED_IDENTITIES = 256


@dataclass(frozen=True)
class Admission:
    key: tuple[str, str]
    epoch: object | None
    probe: object | None


def identity(model: Mapping) -> tuple[str, str]:
    return str(model.get("provider", "")), str(model.get("model", ""))


def remaining(model: Mapping) -> float:
    key = identity(model)
    seconds = _cooldowns.get(key, 0.0) - time.monotonic()
    if seconds <= 0:
        _cooldowns.pop(key, None)
        return 0.0
    return seconds


def acquire(model: Mapping) -> Admission | None:
    """Admit healthy traffic, or one probe after a failed model's cooldown.

    Called before any await, on the classifier's event loop. Different input
    texts have different cache flights, so cache single-flight is insufficient.
    """
    key = identity(model)
    if remaining(model) or key in _probes:
        return None
    probe = object() if _streaks.get(key, 0) else None
    if probe is not None:
        _probes[key] = probe
    return Admission(key, _epochs.get(key), probe)


def finish(admission: Admission) -> None:
    """Cancellation, parse failure and timeout also release the owned probe."""
    if admission.probe is not None and _probes.get(admission.key) is admission.probe:
        _probes.pop(admission.key, None)


def _streak_seconds(status: int, streak: int) -> float:
    if status == 429:
        base, cap = _RATE_LIMIT_BASE_SECONDS, _RATE_LIMIT_MAX_SECONDS
    else:
        base, cap = _TRANSIENT_BASE_SECONDS, _TRANSIENT_MAX_SECONDS
    return min(base * (2 ** (streak - 1)), cap)


def _prune_expired(now: float) -> None:
    expired = [key for key, deadline in _cooldowns.items() if deadline <= now]
    for key in expired:
        _cooldowns.pop(key, None)
        # Expiry permits a probe; only confirmed success resets the streak.


def defer(model: Mapping, status: int, retry_after: str | None = None) -> float:
    key = identity(model)
    now = time.monotonic()
    _prune_expired(now)
    if key not in _touched and len(_touched) >= _MAX_TRACKED_IDENTITIES:
        idle = [item for item in _touched if item not in _probes]
        oldest = min(idle or _touched, key=_touched.get)
        for state in (_cooldowns, _streaks, _epochs, _probes, _touched):
            state.pop(oldest, None)
    streak = min(_streaks.get(key, 0) + 1, 10)
    _streaks[key] = streak
    _epochs[key] = object()
    _touched[key] = now
    seconds = _streak_seconds(status, streak)
    cap = _RATE_LIMIT_MAX_SECONDS if status == 429 else _TRANSIENT_MAX_SECONDS
    if retry_after:
        try:
            value = float(retry_after)
        except (TypeError, ValueError):
            try:
                value = parsedate_to_datetime(retry_after).timestamp() - time.time()
            except (TypeError, ValueError, OverflowError):
                value = seconds
        if math.isfinite(value) and value >= 0:
            seconds = min(cap, max(seconds, value))
    # Keep bounded, and do not let a concurrent shorter rejection undo backoff.
    _cooldowns[key] = max(_cooldowns.get(key, 0.0), now + seconds)
    return seconds


def release(model: Mapping, *, admission: Admission | None = None) -> None:
    """Reset the step-wise streak after one confirmed successful request.

    The active cooldown (if any) still applies until it expires naturally;
    release only stops the next rejection from compounding.
    """

    key = identity(model)
    if admission is not None and (admission.key != key or admission.epoch is not _epochs.get(key)):
        return  # A late success must not reset a newer rejection's streak.
    _streaks.pop(key, None)
    if not remaining(model):
        _epochs.pop(key, None)
        _touched.pop(key, None)

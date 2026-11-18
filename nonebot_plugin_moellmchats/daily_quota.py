"""Shared daily token allowances with durable reservations before HTTP I/O."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from datetime import time as day_time
import json
from pathlib import Path
import sqlite3
import threading
import time
from zoneinfo import ZoneInfo

from .private_files import ensure_private_directory, ensure_private_file


def quota_policy(model):
    policy = model.get("daily_quota")
    if policy is None:
        return None
    if not isinstance(policy, Mapping):
        raise ValueError("daily_quota must be a table")
    group, limit = policy.get("group"), policy.get("tokens")
    if not isinstance(group, str) or not group or len(group) > 80:
        raise ValueError("daily_quota.group is invalid")
    if type(limit) is not int or not 1 <= limit <= 10**10:
        raise ValueError("daily_quota.tokens is invalid")
    zone = ZoneInfo(policy.get("timezone", "UTC"))
    return str(model["provider"]), group, limit, zone


def quota_window(model, now=None):
    policy = quota_policy(model)
    if policy is None:
        return None
    provider, group, limit, zone = policy
    current = datetime.fromtimestamp(time.time() if now is None else now, zone)
    end = datetime.combine(current.date() + timedelta(days=1), day_time(), zone)
    return provider, group, limit, current.date().isoformat(), end.timestamp()


def apply_openai_chat_policy(model, payload):
    if not model.get("openai_chat_compat"):
        return
    if model.get("supports_reasoning_effort", False):
        if payload.get("reasoning_effort") == "max":
            payload["reasoning_effort"] = "high"
        if payload.get("tools") and model.get("tool_reasoning_effort") == "none":
            payload["reasoning_effort"] = "none"
        # Reasoning models reject sampling controls on common reasoning levels.
        for key in ("temperature", "top_p", "top_k"):
            payload.pop(key, None)
    else:
        payload.pop("reasoning_effort", None)
        payload.pop("reasoning", None)
        payload.pop("top_k", None)
    field = model.get("token_limit_field", "max_tokens")
    other = "max_tokens" if field == "max_completion_tokens" else "max_completion_tokens"
    if other in payload:
        previous = payload.pop(other)
        payload[field] = min(previous, payload[field]) if field in payload else previous
    payload.pop("thinking", None)
    payload.pop("tool_stream", None)


class DailyQuotaStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.initialized = False

    def connect(self):
        ensure_private_directory(self.path.parent)
        ensure_private_file(self.path)
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA busy_timeout=10000")
        if not self.initialized:
            db.execute("""CREATE TABLE IF NOT EXISTS daily_quota_attempts (
                id TEXT PRIMARY KEY, provider TEXT NOT NULL, quota_group TEXT NOT NULL,
                day TEXT NOT NULL, allowance INTEGER NOT NULL, reserved INTEGER NOT NULL,
                used INTEGER, state TEXT NOT NULL, created_at REAL NOT NULL)""")
            db.execute("""CREATE INDEX IF NOT EXISTS daily_quota_scope
                ON daily_quota_attempts(provider,quota_group,day)""")
            db.commit()
            self.path.chmod(0o600)
            self.initialized = True
        return db

    @staticmethod
    def reservation(model, payload):
        encoded = json.dumps(payload or {}, ensure_ascii=False)
        maximum = int(model.get("context_tokens", 128_000))
        has_media = any(
            part in encoded
            for part in (
                '"image_url"',
                '"input_audio"',
                '"file_url"',
                '"video_url"',
            )
        )
        incoming = maximum if has_media else len(encoded.encode("utf-8")) + 4096
        output = max(
            (value if type(value) is int and value > 0 else 0)
            for value in ((payload or {}).get("max_tokens"), (payload or {}).get("max_completion_tokens"))
        )
        # Never use an unbounded provider default on a free request.
        return incoming + (output if type(output) is int and output > 0 else 8192)

    @staticmethod
    def _spent(db, provider, group, day):
        return db.execute(
            """SELECT coalesce(sum(CASE
            WHEN state='released' THEN 0
            WHEN state='settled' THEN used ELSE reserved END),0)
            FROM daily_quota_attempts WHERE provider=? AND quota_group=? AND day=?""",
            (provider, group, day),
        ).fetchone()[0]

    def remaining(self, model, *, now=None):
        window = quota_window(model, now)
        if window is None:
            return None
        provider, group, limit, day, _ = window
        with self.lock:
            db = self.connect()
            try:
                return max(0, limit - self._spent(db, provider, group, day))
            finally:
                db.close()

    def reserve(self, model, payload, attempt_id, *, now=None):
        window = quota_window(model, now)
        if window is None:
            return None
        provider, group, limit, day, _ = window
        reserved = self.reservation(model, payload)
        with self.lock:
            db = self.connect()
            try:
                db.execute("BEGIN IMMEDIATE")
                if db.execute("SELECT 1 FROM daily_quota_attempts WHERE id=?", (attempt_id,)).fetchone():
                    raise ValueError("daily quota attempt cannot be submitted twice")
                remaining = max(0, limit - self._spent(db, provider, group, day))
                if remaining < reserved:
                    db.rollback()
                    return 403
                db.execute(
                    "INSERT INTO daily_quota_attempts VALUES (?,?,?,?,?,?,NULL,'reserved',?)",
                    (attempt_id, provider, group, day, limit, reserved, time.time() if now is None else now),
                )
                db.commit()
                return 0
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()

    def finish(self, attempt_id, *, used=None, definite_rejection=False):
        # Unknown usage retains its reservation across restarts until day end.
        known = type(used) is int and used >= 0
        state = "settled" if known else "released" if definite_rejection else "unknown"
        with self.lock:
            db = self.connect()
            try:
                db.execute(
                    "UPDATE daily_quota_attempts SET used=?,state=? WHERE id=?", (used if known else None, state, attempt_id)
                )
                db.commit()
            finally:
                db.close()


_stores = {}


def daily_store(ledger):
    path = ledger.path.with_name("daily_quotas.sqlite3")
    if str(path) not in _stores:
        _stores[str(path)] = DailyQuotaStore(path)
    return _stores[str(path)]

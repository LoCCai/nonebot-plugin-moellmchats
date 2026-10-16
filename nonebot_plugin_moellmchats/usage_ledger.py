"""Durable legacy-transport accounting, separate from the Agent run repository.

One row per HTTP attempt; streamed usage snapshots replace the same row. Token
subcategories are subsets, never added to the provider's input/output totals.
No prompts, attachments, authorization headers or provider errors are stored.
"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import contextmanager, asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from nonebot.log import logger

_scope: ContextVar[dict | None] = ContextVar("moellm_usage_scope", default=None)
_budget_locks = {}
_budget_reserved = {}
_attempt: ContextVar[dict | None] = ContextVar("moellm_usage_attempt", default=None)


@contextmanager
def bind_usage_scope(bot, event):
    from .onebot_facade import event_user_id
    token = _scope.set({"bot_id": str(getattr(bot, "self_id", "")),
        "user_id": str(getattr(event, "get_user_id", lambda: event_user_id(event))()),
        "group_id": str(getattr(event, "group_id", "") or ""),
        "scene": "group" if getattr(event, "group_id", None) else "private",
        "request_id": uuid.uuid4().hex})
    try:
        yield
    finally:
        _scope.reset(token)


def integer(value):
    return value if type(value) is int and 0 <= value <= (1 << 53) else 0


def numeric_usage(value, depth=0):
    """Keep returned counters, including unknown future numeric details."""
    if not isinstance(value, Mapping) or depth > 3:
        return {}
    return {str(k)[:100]: numeric_usage(v, depth + 1) if isinstance(v, Mapping) else v
            for k, v in list(value.items())[:64]
            if isinstance(v, Mapping) or (type(v) in (int, float) and 0 <= v <= (1 << 53))}


def normalize_usage(usage):
    payload = usage if isinstance(usage, Mapping) else {}
    prompt = integer(payload.get("prompt_tokens", payload.get("input_tokens")))
    output = integer(payload.get("completion_tokens", payload.get("output_tokens")))
    details = payload.get("prompt_tokens_details") or payload.get("input_tokens_details") or {}
    completion = payload.get("completion_tokens_details") or payload.get("output_tokens_details") or {}
    cache = integer(details.get("cached_tokens")) if isinstance(details, Mapping) else 0
    cache = min(prompt, cache or integer(payload.get("prompt_cache_hit_tokens")))
    reasoning = min(output, integer(completion.get("reasoning_tokens"))) if isinstance(completion, Mapping) else 0
    # Some non-chat APIs return total_tokens only. Preserve it without inventing
    # an input/output breakdown or pretending an absent usage is free.
    total = prompt + output or integer(payload.get("total_tokens"))
    return {"prompt": prompt, "completion": output, "cache": cache, "reasoning": reasoning,
            "total": total, "usage_known": int(bool(payload)), "raw_usage": numeric_usage(payload)}


class UsageLedger:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.totals = {}
        self.budgets = {}
        self.prices = {}
        self.initialized = False

    def initialize(self):
        with self.lock:
            if self.initialized:
                return
            from .private_files import ensure_private_directory, ensure_private_file
            ensure_private_directory(self.path.parent)
            ensure_private_file(self.path)
            with self.connect() as db:
                db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS usage (
                  id TEXT PRIMARY KEY, at REAL NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
                  bot_id TEXT NOT NULL, group_id TEXT NOT NULL, user_id TEXT NOT NULL,
                  scene TEXT NOT NULL, request_id TEXT NOT NULL, purpose TEXT NOT NULL,
                  status TEXT NOT NULL, prompt INTEGER NOT NULL, completion INTEGER NOT NULL,
                  cache INTEGER NOT NULL, reasoning INTEGER NOT NULL, total INTEGER NOT NULL,
                  usage_known INTEGER NOT NULL, raw_usage TEXT NOT NULL,
                  estimate TEXT, currency TEXT, price_snapshot TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS usage_at ON usage(at,id);
                CREATE INDEX IF NOT EXISTS usage_scope ON usage(group_id,user_id,at);
                CREATE INDEX IF NOT EXISTS usage_model ON usage(provider,model,at);
                CREATE TABLE IF NOT EXISTS budgets (
                  identity TEXT PRIMARY KEY, allowance INTEGER NOT NULL, baseline INTEGER NOT NULL,
                  updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS budget_changes (
                  id INTEGER PRIMARY KEY, identity TEXT NOT NULL, allowance INTEGER NOT NULL,
                  baseline INTEGER NOT NULL, actor TEXT NOT NULL, at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS prices (model TEXT PRIMARY KEY, snapshot TEXT NOT NULL);
                """)
                self.totals = {r[0]: r[1] for r in db.execute(
                    "SELECT model || ' (' || provider || ')',sum(total) FROM usage GROUP BY provider,model")}
                self.budgets = {r[0]: (r[1], r[2]) for r in db.execute("SELECT identity,allowance,baseline FROM budgets")}
                self.prices = {r[0]: json.loads(r[1]) for r in db.execute("SELECT model,snapshot FROM prices")}
            self.path.chmod(0o600)
            self.initialized = True

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=15000")
            yield db
            db.commit()
        finally:
            db.close()

    def configure_budget(self, identity, allowance, *, reset=False, actor="configuration"):
        self.initialize()
        if type(allowance) is not int or not 1 <= allowance <= 10**12:
            raise ValueError("额度必须为1至1000000000000之间的整数 Token")
        with self.lock, self.connect() as db:
            if identity in self.budgets and not reset:
                return
            baseline = self.totals.get(identity, 0) if reset else 0
            db.execute("INSERT OR REPLACE INTO budgets VALUES (?,?,?,?)", (identity, allowance, baseline, time.time()))
            db.execute("INSERT INTO budget_changes(identity,allowance,baseline,actor,at) VALUES (?,?,?,?,?)",
                       (identity, allowance, baseline, actor, time.time()))
            self.budgets[identity] = allowance, baseline

    def remaining(self, identity):
        budget = self.budgets.get(identity)
        return None if budget is None else max(0, budget[0] - max(0, self.totals.get(identity, 0) - budget[1]))

    def exhausted(self, identity):
        return self.remaining(identity) == 0

    def put_prices(self, prices):
        self.initialize()
        with self.lock, self.connect() as db:
            for model, row in prices.items():
                db.execute("INSERT OR REPLACE INTO prices VALUES (?,?)", (model, json.dumps(row)))
            self.prices.update(prices)

    def estimate(self, model, counters):
        identity = f"{model.get('model')} ({model.get('provider')})"
        price = self.prices.get(identity) or self.prices.get(model.get('model'))
        if not price or not counters["usage_known"]:
            return None, None, {}
        try:
            if counters['total'] and not (counters['prompt'] or counters['completion']):
                return None, None, price
            cost = ((Decimal(price["input"]) * (counters["prompt"] - counters["cache"])) +
                    Decimal(price["cached"]) * counters["cache"] +
                    Decimal(price["output"]) * counters["completion"]) / Decimal(1_000_000)
            return str(cost), price["currency"], price
        except (KeyError, InvalidOperation, ValueError, TypeError):
            return None, None, price

    def record(self, model, usage, *, scope=None, purpose="chat", attempt_id=None, status="completed"):
        self.initialize()
        counters = normalize_usage(usage)
        identity = f"{model.get('model', 'unknown')} ({model.get('provider', 'unknown')})"
        estimate, currency, price = self.estimate(model, counters)
        scope = scope or {}
        row = dict(id=attempt_id or uuid.uuid4().hex, at=time.time(),
            provider=str(model.get("provider", "unknown")), model=str(model.get("model", "unknown")),
            **{key: str(scope.get(key, "")) for key in ("bot_id", "group_id", "user_id", "request_id")},
            scene=scope.get("scene", "unattributed"), purpose=purpose, status=status,
            **counters, estimate=estimate, currency=currency, price_snapshot=json.dumps(price))
        row["raw_usage"] = json.dumps(row["raw_usage"])
        with self.lock, self.connect() as db:
            previous = db.execute("SELECT total,at FROM usage WHERE id=?", (row["id"],)).fetchone()
            if previous:
                # SSE reports cumulative counts. An old/duplicate snapshot must
                # never reduce accounting or move the request to a new day.
                if previous["total"] > row["total"]:
                    return
                row["at"] = previous["at"]
            db.execute("INSERT OR REPLACE INTO usage (" + ",".join(row) + ") VALUES (" +
                       ",".join("?" for _ in row) + ")", tuple(row.values()))
            db.commit()
            self.totals[identity] = self.totals.get(identity, 0) + row["total"] - (previous["total"] if previous else 0)

    def rows(self, *, since=0, until=None, group_id=None, user_id=None, model=None, after=None, limit=1000, reverse=False):
        self.initialize()
        where, values = ["at>=?"], [since]
        for field, value in (("group_id", group_id), ("user_id", user_id), ("model", model)):
            if value is not None:
                where.append(field + "=?")
                values.append(str(value))
        if until is not None:
            where.append("at<?")
            values.append(until)
        if after:
            where.append("(at>? OR (at=? AND id>?))")
            values.extend((after[0], after[0], after[1]))
        with self.lock, self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM usage WHERE " + " AND ".join(where) +
                    (" ORDER BY at DESC,id DESC LIMIT ?" if reverse else " ORDER BY at,id LIMIT ?"),
                    (*values, min(10_000, max(1, limit))))]


_ledger: UsageLedger | None = None


def get_ledger():
    global _ledger
    if _ledger is None:
        from .config import config_path
        _ledger = UsageLedger(config_path / "usage" / "usage.sqlite3")
    return _ledger


async def record_usage(model, usage, *, purpose="chat", attempt_id=None, status="completed"):
    ledger = get_ledger()
    # Capture the context before leaving the request task. Thread work inherits
    # it too, but a plain dict makes this ownership explicit.
    scope = dict(_scope.get() or {})
    if not scope:
        # Restart recovery executes its saved operation directly.
        from .request_recovery import current_binding
        if binding := current_binding():
            event = binding.record.get("event", {})
            scope = {"bot_id": binding.record.get("bot_id", ""),
                "user_id": event.get("user_id", ""), "group_id": event.get("group_id") or "",
                "scene": "group" if event.get("group_id") else "private",
                "request_id": binding.record["key"]}
    attempt = _attempt.get()
    if attempt and attempt_id is None:
        attempt_id = attempt["id"]
        attempt["recorded"] = True
    await asyncio.to_thread(ledger.record, model, usage, scope=scope,
                            purpose=purpose, attempt_id=attempt_id, status=status)


class _BudgetResponse:
    status = 403
    headers = {}
    def __init__(self, status=403):
        self.status = status
    async def text(self):
        return '{"error":{"code":"insufficient_quota","message":"quota exhausted"}}'
    async def json(self):
        return json.loads(await self.text())
    @property
    def content(self):
        owner = self
        class Content:
            async def read(self, n=-1):
                return (await owner.text()).encode()[:n if n >= 0 else None]
            async def iter_chunked(self, n):
                yield (await owner.text()).encode()
        return Content()


@asynccontextmanager
async def budgeted_post(session, model, *args, **kwargs):
    """Reserve a conservative allowance without queuing chats behind videos.

    The final in-flight response can cross the boundary; no subsequent request is
    sent after that response is counted. Limits apply across every user and group.
"""
    identity = f"{model.get('model')} ({model.get('provider')})"
    ledger = get_ledger()
    lock = _budget_locks.setdefault(identity, asyncio.Lock()) if identity in ledger.budgets else None
    reservation = 0
    rejected = None
    if lock:
        # JSON bytes conservatively cover text and tool schemas. Native media
        # has provider-dependent tokenization, so reserve its full context size.
        payload = kwargs.get('json')
        if payload is None:
            try:
                payload = json.loads(kwargs.get('data') or '{}')
            except (TypeError, ValueError):
                payload = {}
        encoded = json.dumps(payload, ensure_ascii=False)
        context_size = integer(model.get('context_tokens')) or 1_000_000
        media = any(kind in encoded for kind in ('"image_url"', '"video_url"', '"file_url"', '"input_audio"'))
        incoming = context_size if media else min(context_size, len(encoded.encode('utf-8')) + 4096)
        reservation = incoming + max(integer(payload.get('max_tokens')), integer(payload.get('max_completion_tokens')), 8192)
        async with lock:
            outstanding = _budget_reserved.get(identity, 0)
            remaining = ledger.remaining(identity)
            if remaining == 0:
                rejected = 403
            elif outstanding and remaining - outstanding < reservation:
                rejected = 429  # use another configured model, never mark exhausted
            else:
                _budget_reserved[identity] = outstanding + reservation
    attempt = {"id": uuid.uuid4().hex, "recorded": False}
    token = _attempt.set(attempt)
    status = "interrupted_usage_unknown"
    try:
        if rejected is not None or ledger.exhausted(identity):
            attempt["recorded"] = True
            yield _BudgetResponse(rejected or 403)
            return
        # Write intent before network I/O. A crash can leave an explicit
        # unknown-cost attempt, rather than making personal accounting vanish.
        await record_usage(model, None, status="started_usage_unknown")
        attempt["recorded"] = False
        async with session.post(*args, **kwargs) as response:
            status = "completed_usage_unknown" if response.status == 200 else f"http_{response.status}"
            yield response
    finally:
        try:
            if not attempt["recorded"]:
                await asyncio.shield(record_usage(model, None, status=status))
        finally:
            _attempt.reset(token)
            if lock and rejected is None:
                async with lock:
                    _budget_reserved[identity] = max(0, _budget_reserved.get(identity, 0) - reservation)


def apply_glm_policy(model, payload):
    if model.get("provider") != "bigmodel":
        return
    if model.get("model") != "glm-5.3-flash":
        raise ValueError("BigModel仅允许glm-5.3-flash")
    payload.update(temperature=1, top_p=0.95, reasoning_effort="max",
                   thinking={"type": "enabled", "clear_thinking": False})
    if payload.get("stream"):
        payload["tool_stream"] = True
    # Thinking is mandatory. Tiny classification/output limits must not spend
    # all tokens on reasoning and leave no answer.
    field = model.get("token_limit_field", "max_tokens")
    payload[field] = max(8192, integer(payload.get(field)))


async def budget_available(model):
    ledger = get_ledger()
    await asyncio.to_thread(ledger.initialize)
    return not ledger.exhausted(f"{model.get('model')} ({model.get('provider')})")

"""Optional host adapter; secrets and payer identities stay out of model state."""

from contextlib import asynccontextmanager
from functools import wraps

_adapter = None


def register(adapter):
    global _adapter
    _adapter = adapter


def billable_tool(function):
    @wraps(function)
    async def wrapped(*args, _tool_context, **kwargs):
        async with event_scope(_tool_context.bot, _tool_context.event):
            return await function(*args, _tool_context=_tool_context, **kwargs)

    return wrapped


def active():
    return _adapter is not None and _adapter.active()


def snapshot():
    return _adapter.snapshot() if _adapter is not None else None


def model(key, *, has_tools=False, has_images=False):
    return _adapter.model(key, has_tools=has_tools, has_images=has_images)


def config(key, default=None):
    return _adapter.config(key, default) if active() else default


@asynccontextmanager
async def event_scope(bot, event):
    if _adapter is None:
        yield
    else:
        async with _adapter.event_scope(bot, event):
            yield


@asynccontextmanager
async def recovery_scope(record):
    saved = record.get("billing_payer")
    if _adapter is None:
        if saved:
            raise RuntimeError("Personal billing adapter unavailable; saved payer cannot be changed")
        yield
    else:
        async with _adapter.recovery_scope(saved):
            yield


@asynccontextmanager
async def post(session, model, *args, **kwargs):
    async with _adapter.post(session, model, *args, **kwargs) as response:
        yield response

import pytest

from nonebot_plugin_moellmchats import classifier_cooldown as cooldown
from nonebot_plugin_moellmchats.diagnostics import exception_metadata


def test_classifier_backoff_is_shared_bounded_and_expires(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(cooldown.time, "monotonic", lambda: now[0])
    cooldown._cooldowns.clear()
    model = {"provider": "primary", "model": "classifier"}
    assert cooldown.defer(model, 429, "120") == 120
    assert cooldown.remaining(dict(model)) == 120
    cooldown.defer(model, 503, "0")
    assert cooldown.remaining(model) == 120
    assert cooldown.remaining({**model, "provider": "other"}) == 0
    now[0] += 121
    assert cooldown.remaining(model) == 0
    for number in range(300):
        cooldown.defer({"provider": str(number), "model": "classifier"}, 429)
    assert len(cooldown._cooldowns) <= 256
    cooldown._cooldowns.clear()


@pytest.mark.asyncio
async def test_cooled_classifier_skips_http_and_enters_existing_fallback():
    from nonebot_plugin_moellmchats.categorize import Categorize

    # Exercise the actual request entry without constructing a chat runtime.
    model = {"provider": "runtime-test", "model": "classifier"}
    cooldown.defer(model, 429)
    target = object.__new__(Categorize)
    try:
        assert await target._request_category_model_once(model, "prompt") is False
        assert target._phase == "http_status_429"
    finally:
        cooldown._cooldowns.clear()


@pytest.mark.asyncio
async def test_http_429_cools_following_requests_without_marking_quota_exhausted(monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from nonebot_plugin_moellmchats import categorize, usage_ledger

    model = {"provider": "rejection-test", "model": "classifier", "url": "https://example.invalid", "key": "test-only"}
    calls = []

    @asynccontextmanager
    async def post(*args, **kwargs):
        calls.append(1)
        yield SimpleNamespace(status=429, headers={"Retry-After": "120"})

    exhausted = AsyncMock()
    monkeypatch.setattr(categorize, "get_session", lambda: object())
    monkeypatch.setattr(usage_ledger, "budgeted_post", post)
    monkeypatch.setattr(usage_ledger, "budget_available", AsyncMock(return_value=True))
    monkeypatch.setattr(categorize.model_selector, "mark_model_exhausted", exhausted)
    cooldown._cooldowns.clear()
    try:
        first = categorize.Categorize("test")
        assert await first._request_category_model_once(model, "prompt") is False
        assert first._phase == "http_status_429"
        assert 119 < cooldown.remaining(model) <= 120
        second = categorize.Categorize("another test")
        assert await second._request_category_model_once(dict(model), "prompt") is False
        assert second.selection_source == "classification_cooldown_fallback"
        assert len(calls) == 1
        exhausted.assert_not_awaited()
    finally:
        cooldown._cooldowns.clear()


def test_tool_diagnostics_does_not_include_message_or_locals():
    secret = "private-cookie-and-prompt"
    try:
        raise ValueError(secret)
    except ValueError as error:
        metadata = exception_metadata(error)
    locations = str(metadata["frames"])
    assert metadata["exception_type"] == "ValueError"
    assert "test_runtime_diagnostics.py:" in locations
    assert secret not in locations
    assert "/" not in locations

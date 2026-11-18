from __future__ import annotations

import asyncio
import json

import pytest

from nonebot_plugin_moellmchats import classifier_cooldown as cooldown

_MODEL = {"provider": "workbuddy", "model": "ds-flash"}
_KEY = ("workbuddy", "ds-flash")


@pytest.fixture(autouse=True)
def _isolate_state():
    cooldown._cooldowns.clear()
    cooldown._streaks.clear()
    cooldown._epochs.clear()
    cooldown._probes.clear()
    cooldown._touched.clear()
    yield
    cooldown._cooldowns.clear()
    cooldown._streaks.clear()
    cooldown._epochs.clear()
    cooldown._probes.clear()
    cooldown._touched.clear()


def test_rate_limit_backoff_grows_stepwise_and_caps() -> None:
    expected = [60.0, 120.0, 240.0, 480.0, 900.0, 900.0]
    actual = [cooldown.defer(_MODEL, 429) for _ in expected]
    assert actual == expected


def test_transient_status_backoff_grows_and_caps() -> None:
    actual = [cooldown.defer(_MODEL, 503) for _ in range(5)]
    assert actual == [30.0, 60.0, 120.0, 240.0, 300.0]


def test_retry_after_is_respected_as_floor_but_capped() -> None:
    assert cooldown.defer(_MODEL, 429, retry_after="120") == 120.0
    assert cooldown.defer(_MODEL, 429, retry_after="99999") <= 900.0


def test_concurrent_shorter_rejection_cannot_undo_backoff() -> None:
    cooldown.defer(_MODEL, 429)
    cooldown.defer(_MODEL, 429)  # deadline now + 120
    cooldown.defer(_MODEL, 429, retry_after="0")  # must not shorten to 60
    assert cooldown.remaining(_MODEL) > 100.0


def test_release_resets_streak_but_keeps_active_cooldown() -> None:
    cooldown.defer(_MODEL, 429)
    cooldown.defer(_MODEL, 429)
    assert cooldown._streaks[_KEY] == 2
    cooldown.release(_MODEL)
    assert _KEY not in cooldown._streaks
    # The active cooldown still applies until it expires naturally.
    assert cooldown.remaining(_MODEL) > 0
    # The next rejection starts over from the base delay.
    assert cooldown.defer(_MODEL, 429) == 60.0


def test_remaining_expires_and_cleans_up() -> None:
    assert cooldown.remaining(_MODEL) == 0.0
    cooldown.defer(_MODEL, 503)
    assert cooldown.remaining(_MODEL) > 0.0
    cooldown._cooldowns[_KEY] = 0.0
    assert cooldown.remaining(_MODEL) == 0.0
    assert _KEY not in cooldown._cooldowns


def test_expired_rejections_keep_streak_even_when_another_model_defers(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(cooldown.time, "monotonic", lambda: now[0])
    for delay in (60, 120, 240, 480, 900, 900):
        assert cooldown.defer(_MODEL, 429) == delay
        now[0] += delay + 1
        # Pruning another model must not silently reset our failed probe count.
        cooldown.defer({"provider": "other", "model": "other"}, 503)


def test_expired_cooldown_only_admits_one_probe_and_success_resets(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(cooldown.time, "monotonic", lambda: now[0])
    cooldown.defer(_MODEL, 429)
    assert cooldown.acquire(_MODEL) is None
    now[0] += 61
    admitted = [cooldown.acquire(_MODEL) for _ in range(20)]
    assert sum(item is not None for item in admitted) == 1
    cooldown.release(_MODEL, admission=admitted[0])
    cooldown.finish(admitted[0])
    assert _KEY not in cooldown._streaks
    assert all(cooldown.acquire(_MODEL) is not None for _ in range(20))


def test_cancelled_probe_releases_only_its_own_claim(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(cooldown.time, "monotonic", lambda: now[0])
    cooldown.defer(_MODEL, 429)
    now[0] += 61
    first = cooldown.acquire(_MODEL)
    cooldown.finish(first)
    second = cooldown.acquire(_MODEL)
    assert second is not None
    cooldown.finish(first)
    assert cooldown.acquire(_MODEL) is None
    cooldown.finish(second)


def test_late_healthy_success_cannot_reset_a_newer_rejection():
    admitted = cooldown.acquire(_MODEL)
    cooldown.defer(_MODEL, 429)
    cooldown.release(_MODEL, admission=admitted)
    assert cooldown._streaks[_KEY] == 1


def test_all_cooldown_state_is_bounded(monkeypatch):
    monkeypatch.setattr(cooldown, "_MAX_TRACKED_IDENTITIES", 4)
    for index in range(20):
        cooldown.defer({"provider": "test", "model": str(index)}, 429)
    for state in (cooldown._cooldowns, cooldown._streaks, cooldown._epochs, cooldown._touched):
        assert len(state) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "cancel", "timeout", "invalid"])
async def test_real_classifier_flights_share_one_recovery_probe(monkeypatch, outcome):
    from nonebot_plugin_moellmchats import categorize, usage_ledger

    now = [100.0]
    monkeypatch.setattr(cooldown.time, "monotonic", lambda: now[0])
    cooldown.defer(_MODEL, 429)
    now[0] += 61
    entered, resume = asyncio.Event(), asyncio.Event()
    calls = []

    class Response:
        status = 200

        async def __aenter__(self):
            calls.append(True)
            entered.set()
            await resume.wait()
            if outcome == "timeout":
                raise TimeoutError
            return self

        async def __aexit__(self, *args):
            return False

        async def json(self):
            difficulty = "invalid" if outcome == "invalid" else "0"
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps({"difficulty": difficulty, "vision_required": False, "required_plugins": []})
                        }
                    }
                ]
            }

    async def available(*args):
        return True

    async def usage(*args, **kwargs):
        pass

    monkeypatch.setattr(usage_ledger, "budget_available", available)
    monkeypatch.setattr(usage_ledger, "record_usage", usage)
    monkeypatch.setattr(usage_ledger, "apply_glm_policy", lambda *args: None)
    monkeypatch.setattr(usage_ledger, "budgeted_post", lambda *args, **kwargs: Response())
    monkeypatch.setattr(categorize, "get_session", lambda: object())
    model = {**_MODEL, "key": "test-only", "url": "https://example.invalid"}
    first = categorize.Categorize("first question")
    task = asyncio.create_task(first._request_category_model_once(model, "catalog"))
    await entered.wait()
    other = categorize.Categorize("different question")
    assert await other._request_category_model_once(model, "catalog") is False
    assert other.selection_source == "classification_cooldown_fallback"
    assert len(calls) == 1
    if outcome == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        resume.set()
        if outcome == "timeout":
            with pytest.raises(TimeoutError):
                await task
        elif outcome == "invalid":
            assert await task is False
            assert cooldown._streaks[_KEY] == 1
        else:
            assert await task == ("0", False, [])
            assert _KEY not in cooldown._streaks
    assert _KEY not in cooldown._probes
    next_admission = cooldown.acquire(_MODEL)
    assert next_admission is not None
    cooldown.finish(next_admission)

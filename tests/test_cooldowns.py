from __future__ import annotations

import asyncio
from math import inf, nan

import pytest

from nonebot_plugin_moellmchats.cooldowns import (
    CooldownClaim,
    CooldownError,
    CooldownLease,
    MemoryCooldownStore,
)

_TOKEN_A = "a" * 32
_TOKEN_B = "b" * 32


def _token_factory(*tokens: str):
    values = iter(tokens)
    return lambda: next(values)


@pytest.mark.asyncio
async def test_memory_cooldown_preserves_legacy_timestamp_and_retry_semantics() -> None:
    values: dict[int | str, object] = {42: 990}
    store = MemoryCooldownStore(values, token_factory=lambda: _TOKEN_A)

    denied = await store.claim(
        user_id=42,
        event_time=1_000,
        cooldown_seconds=120,
    )
    acquired = await store.claim(
        user_id=42,
        event_time=1_110,
        cooldown_seconds=120,
    )

    assert denied == CooldownClaim(lease=None, retry_after_seconds=110)
    assert acquired.acquired is True
    assert acquired.lease == CooldownLease(
        user_id=42,
        token=_TOKEN_A,
        claimed_at=1_110,
    )
    assert values[42] == 1_110


@pytest.mark.asyncio
async def test_memory_cooldown_claim_is_atomic_for_one_user() -> None:
    values: dict[int | str, object] = {}
    store = MemoryCooldownStore(values, token_factory=lambda: _TOKEN_A)

    claims = await asyncio.gather(
        *(
            store.claim(
                user_id=42,
                event_time=1_000,
                cooldown_seconds=120,
            )
            for _index in range(20)
        )
    )

    assert sum(claim.acquired for claim in claims) == 1
    assert sorted(claim.retry_after_seconds for claim in claims) == [0] + [120] * 19


@pytest.mark.asyncio
async def test_memory_release_is_bound_to_the_exact_claim() -> None:
    values: dict[int | str, object] = {}
    store = MemoryCooldownStore(
        values,
        token_factory=_token_factory(_TOKEN_A, _TOKEN_B),
    )
    first = await store.claim(
        user_id=42,
        event_time=1_000,
        cooldown_seconds=1,
    )
    second = await store.claim(
        user_id=42,
        event_time=1_002,
        cooldown_seconds=1,
    )

    assert first.lease is not None
    assert second.lease is not None
    assert await store.release(first.lease) is False
    assert values[42] == 1_002
    assert await store.release(second.lease) is True
    assert values[42] == 0
    assert await store.release(second.lease) is False


@pytest.mark.asyncio
async def test_memory_reset_invalidates_lease_and_clear_preserves_mapping_contract() -> None:
    values: dict[int | str, object] = {}
    store = MemoryCooldownStore(values, token_factory=lambda: _TOKEN_A)
    claim = await store.claim(
        user_id=42,
        event_time=1_000,
        cooldown_seconds=120,
    )
    assert claim.lease is not None

    store.reset_user(42)
    assert values[42] == 0
    assert await store.release(claim.lease) is False

    store.clear()
    assert values == {}
    assert store.safe_diagnostics() == {"backend": "memory", "configured": True}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("user_id", True),
        ("user_id", ""),
        ("user_id", "bad\nuser"),
        ("user_id", "x" * 513),
        ("event_time", True),
        ("event_time", inf),
        ("event_time", nan),
        ("cooldown_seconds", True),
        ("cooldown_seconds", 1.5),
    ],
)
async def test_memory_cooldown_rejects_invalid_claim_inputs(
    field: str,
    value: object,
) -> None:
    store = MemoryCooldownStore({}, token_factory=lambda: _TOKEN_A)
    values: dict[str, object] = {
        "user_id": 42,
        "event_time": 1_000,
        "cooldown_seconds": 120,
    }
    values[field] = value

    with pytest.raises(CooldownError):
        await store.claim(**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_memory_cooldown_rejects_corrupt_timestamp_and_invalid_token_factory() -> None:
    corrupt = MemoryCooldownStore({42: "not-a-time"}, token_factory=lambda: _TOKEN_A)
    with pytest.raises(CooldownError, match="时间戳已损坏"):
        await corrupt.claim(user_id=42, event_time=1_000, cooldown_seconds=120)

    invalid_token = MemoryCooldownStore({}, token_factory=lambda: "not-a-token")
    with pytest.raises(CooldownError, match="claim token"):
        await invalid_token.claim(user_id=42, event_time=1_000, cooldown_seconds=120)

    def fail_with_secret() -> str:
        raise RuntimeError("top-secret")

    failing_token = MemoryCooldownStore({}, token_factory=fail_with_secret)
    with pytest.raises(CooldownError) as captured:
        await failing_token.claim(user_id=42, event_time=1_000, cooldown_seconds=120)
    assert "RuntimeError" in str(captured.value)
    assert "top-secret" not in str(captured.value)
    assert captured.value.__cause__ is None


def test_cooldown_value_objects_reject_impossible_states() -> None:
    with pytest.raises(ValueError, match="token"):
        CooldownLease(user_id=42, token="invalid", claimed_at=1.0)
    with pytest.raises(ValueError, match="retry_after_seconds"):
        CooldownClaim(lease=None, retry_after_seconds=-1)
    with pytest.raises(ValueError, match="不得携带"):
        CooldownClaim(
            lease=CooldownLease(user_id=42, token=_TOKEN_A, claimed_at=1.0),
            retry_after_seconds=1,
        )

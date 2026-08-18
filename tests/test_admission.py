from __future__ import annotations

import asyncio

import pytest

from nonebot_plugin_moellmchats.admission import (
    AdmissionController,
    AdmissionRejected,
)


@pytest.mark.asyncio
async def test_admission_bounds_active_pending_and_per_user() -> None:
    gate = AdmissionController(
        name="llm", max_active=1, max_pending=2, max_per_key=2
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold() -> None:
        async with gate.slot(1):
            entered.set()
            await release.wait()

    first = asyncio.create_task(hold())
    await entered.wait()
    second = asyncio.create_task(hold())
    await asyncio.sleep(0)

    with pytest.raises(AdmissionRejected):
        async with gate.slot(1):
            pass

    assert gate.active == 1
    assert gate.pending == 1
    release.set()
    await asyncio.gather(first, second)
    assert gate.active == gate.pending == 0


@pytest.mark.asyncio
async def test_global_stress_never_exceeds_active_or_pending_limits() -> None:
    gate = AdmissionController(
        name="llm", max_active=4, max_pending=32, max_per_key=2
    )
    release = asyncio.Event()
    entered = asyncio.Event()
    active = 0
    peak_active = 0

    async def hold(user_id: int) -> None:
        nonlocal active, peak_active
        async with gate.slot(user_id):
            active += 1
            peak_active = max(peak_active, active)
            if active == 4:
                entered.set()
            await release.wait()
            active -= 1

    running = [asyncio.create_task(hold(user_id)) for user_id in range(4)]
    await entered.wait()
    pending = [asyncio.create_task(hold(user_id)) for user_id in range(4, 36)]
    await asyncio.sleep(0)
    assert gate.active == 4
    assert gate.pending == 32
    with pytest.raises(AdmissionRejected):
        async with gate.slot(100):
            pass
    release.set()
    await asyncio.gather(*running, *pending)
    assert peak_active == 4
    assert gate.active == gate.pending == 0


@pytest.mark.asyncio
async def test_pending_request_cannot_occupy_global_slot_for_same_user() -> None:
    gate = AdmissionController(
        name="llm", max_active=2, max_pending=2, max_per_key=2
    )
    release = asyncio.Event()
    first_entered = asyncio.Event()
    other_entered = asyncio.Event()

    async def first() -> None:
        async with gate.slot(1):
            first_entered.set()
            await release.wait()

    async def same_user() -> None:
        async with gate.slot(1):
            await release.wait()

    async def other_user() -> None:
        async with gate.slot(2):
            other_entered.set()
            await release.wait()

    tasks = [asyncio.create_task(first())]
    await first_entered.wait()
    tasks.extend(
        [asyncio.create_task(same_user()), asyncio.create_task(other_user())]
    )
    await asyncio.wait_for(other_entered.wait(), 1)
    assert gate.active == 2
    assert gate.pending == 1
    release.set()
    await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_cancelled_pending_request_releases_queue_reservation() -> None:
    gate = AdmissionController(
        name="llm", max_active=1, max_pending=2, max_per_key=2
    )
    release = asyncio.Event()
    entered = asyncio.Event()

    async def hold(user_id: int) -> None:
        async with gate.slot(user_id):
            entered.set()
            await release.wait()

    active = asyncio.create_task(hold(1))
    await entered.wait()
    pending = asyncio.create_task(hold(2))
    await asyncio.sleep(0)
    assert gate.active == 1
    assert gate.pending == 1

    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    assert gate.active == 1
    assert gate.pending == 0
    release.set()
    await active
    assert gate.active == gate.pending == 0

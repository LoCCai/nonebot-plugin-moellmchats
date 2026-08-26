from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import FrozenInstanceError
import importlib
from typing import Any

import pytest

from nonebot_plugin_moellmchats.agent_runtime import DeadlineContext
from nonebot_plugin_moellmchats.tool_contracts import (
    ToolEffect,
    ToolPolicy,
    ToolSpec,
)
from nonebot_plugin_moellmchats.tool_providers import (
    DiscoveredTool,
    ProviderCatalogSnapshot,
    ProviderRegistration,
    ToolSource,
    ToolTrustDenied,
    trust_for_source,
)
from nonebot_plugin_moellmchats.trusted_runner_pool import (
    TrustedRunnerEligibilityError,
    TrustedRunnerExecutionError,
    TrustedRunnerExecutionReport,
    TrustedRunnerExecutionTimeout,
    TrustedRunnerPool,
    TrustedRunnerPoolBusy,
    TrustedRunnerPoolClosed,
    TrustedRunnerPoolLifecycleError,
    TrustedRunnerPoolOwnershipError,
    TrustedRunnerPoolPolicy,
    TrustedRunnerPoolSnapshot,
    TrustedRunnerPoolState,
)

_GENERATION = 91
_PARAMETERS = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": False,
}
_PROVIDER_IDS = {
    ToolSource.REGISTERED: "registered",
    ToolSource.BUILTIN: "builtin",
    ToolSource.MCP: "mcp",
}


async def _canonical_handler() -> str:
    return "ok"


def _spec(
    name: str,
    *,
    handler: object = _canonical_handler,
    effect: ToolEffect = ToolEffect.READ_ONLY,
    permission: str = "user",
) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=f"{name} trusted runner contract",
        parameters=_PARAMETERS,
        handler=handler,  # type: ignore[arg-type]
        effect=effect,
        permission=permission,
        timeout_seconds=30,
        result_limit=6000,
    )


def _catalog(
    *specs: ToolSpec,
    source: ToolSource = ToolSource.REGISTERED,
    generation: int = _GENERATION,
) -> ProviderCatalogSnapshot:
    provider_id = _PROVIDER_IDS[source]
    trust = trust_for_source(source)
    registration = ProviderRegistration(
        provider_id=provider_id,
        source=source,
        trust=trust,
    )
    return ProviderCatalogSnapshot(
        generation=generation,
        registrations={provider_id: registration},
        tools={
            spec.name: DiscoveredTool(
                provider_id=provider_id,
                source=source,
                trust=trust,
                generation=generation,
                spec=spec,
            )
            for spec in specs
        },
    )


def _deadline(seconds: float = 1.0) -> DeadlineContext:
    return DeadlineContext.from_timeout(seconds)


async def _execute(
    pool: TrustedRunnerPool,
    invocation,
    *,
    tool_name: str = "pure_compute",
    dependencies: Mapping[str, object] | None = None,
    deadline: DeadlineContext | None = None,
    is_superuser: bool = False,
) -> TrustedRunnerExecutionReport:
    return await pool.execute(
        tool_name=tool_name,
        invocation=invocation,
        dependencies={} if dependencies is None else dependencies,
        deadline=_deadline() if deadline is None else deadline,
        is_superuser=is_superuser,
    )


@pytest.mark.parametrize(
    ("worker_count", "max_outstanding"),
    [
        (True, 4),
        (0, 4),
        (65, 65),
        (2, True),
        (2, 1),
        (2, 4097),
    ],
)
def test_policy_rejects_invalid_bounds(
    worker_count: Any,
    max_outstanding: Any,
) -> None:
    with pytest.raises(ValueError, match="trusted runner"):
        TrustedRunnerPoolPolicy(  # type: ignore[arg-type]
            worker_count=worker_count,
            max_outstanding=max_outstanding,
        )


def test_pool_construction_is_explicit_sorted_and_task_free() -> None:
    catalog = _catalog(_spec("zeta"), _spec("alpha"))
    pool = TrustedRunnerPool(
        catalog=catalog,
        eligible_tools=("zeta", "alpha"),
        policy=TrustedRunnerPoolPolicy(worker_count=2, max_outstanding=3),
    )

    assert pool.state is TrustedRunnerPoolState.CREATED
    assert pool.generation == _GENERATION
    assert pool.eligible_tools == ("alpha", "zeta")
    assert pool.snapshot() == TrustedRunnerPoolSnapshot(
        generation=_GENERATION,
        state=TrustedRunnerPoolState.CREATED,
        eligible_tools=("alpha", "zeta"),
        worker_count=2,
        max_outstanding=3,
        pending=0,
        active=0,
        completed=0,
        failed=0,
        timed_out=0,
        cancelled=0,
        rejected=0,
    )

    module = importlib.import_module("nonebot_plugin_moellmchats.trusted_runner_pool")
    assert not any(isinstance(value, TrustedRunnerPool) for value in vars(module).values())


@pytest.mark.parametrize(
    "eligible_tools",
    [(), ("pure_compute", "pure_compute"), ["pure_compute"]],
)
def test_pool_rejects_invalid_explicit_allowlist(eligible_tools: object) -> None:
    with pytest.raises(TrustedRunnerEligibilityError):
        TrustedRunnerPool(
            catalog=_catalog(_spec("pure_compute")),
            eligible_tools=eligible_tools,  # type: ignore[arg-type]
        )


def test_pool_rejects_mutating_handler() -> None:
    tool = _spec("mutate", effect=ToolEffect.MUTATING)
    with pytest.raises(TrustedRunnerEligibilityError, match="read_only"):
        TrustedRunnerPool(
            catalog=_catalog(tool),
            eligible_tools=(tool.name,),
        )


def test_pool_rejects_sync_handler() -> None:
    def sync_handler() -> str:
        return "not-cancellable"

    tool = _spec("sync_compute", handler=sync_handler)
    with pytest.raises(TrustedRunnerEligibilityError, match="async handler"):
        TrustedRunnerPool(
            catalog=_catalog(tool),
            eligible_tools=(tool.name,),
        )


def test_pool_rejects_tool_with_declared_capability_policy() -> None:
    base = _spec("capability_tool")
    tool = ToolSpec(
        name=base.name,
        description=base.description,
        parameters=base.parameters,
        handler=base.handler,
        policy=ToolPolicy.generated(),
    )
    with pytest.raises(TrustedRunnerEligibilityError, match="池化边界"):
        TrustedRunnerPool(
            catalog=_catalog(tool),
            eligible_tools=(tool.name,),
        )


def test_pool_rejects_handler_needing_live_runtime_objects() -> None:
    async def runtime_handler(*, _bot=None) -> str:
        return str(_bot)

    tool = _spec("needs_bot", handler=runtime_handler)
    with pytest.raises(TrustedRunnerEligibilityError, match="runtime 参数"):
        TrustedRunnerPool(
            catalog=_catalog(tool),
            eligible_tools=(tool.name,),
        )


def test_pool_rejects_external_result_even_when_builtin_code_is_trusted() -> None:
    tool = _spec("web_search")
    with pytest.raises(TrustedRunnerEligibilityError, match="池化边界"):
        TrustedRunnerPool(
            catalog=_catalog(tool, source=ToolSource.BUILTIN),
            eligible_tools=(tool.name,),
        )


def test_pool_rejects_external_provider() -> None:
    tool = _spec("remote_tool")
    with pytest.raises(TrustedRunnerEligibilityError, match="池化边界"):
        TrustedRunnerPool(
            catalog=_catalog(tool, source=ToolSource.MCP),
            eligible_tools=(tool.name,),
        )


@pytest.mark.asyncio
async def test_pool_requires_explicit_start_and_cannot_restart() -> None:
    pool = TrustedRunnerPool(
        catalog=_catalog(_spec("pure_compute")),
        eligible_tools=("pure_compute",),
    )

    async def invocation(_dependencies: Mapping[str, object]) -> str:
        return "ok"

    with pytest.raises(TrustedRunnerPoolLifecycleError, match="尚未启动"):
        await _execute(pool, invocation)

    assert await pool.start() is pool
    assert await pool.start() is pool
    assert pool.state is TrustedRunnerPoolState.RUNNING
    await pool.close()
    await pool.close()
    assert pool.state is TrustedRunnerPoolState.CLOSED
    with pytest.raises(TrustedRunnerPoolLifecycleError, match="不得重启"):
        await pool.start()


@pytest.mark.asyncio
async def test_context_manager_executes_and_freezes_report() -> None:
    pool = TrustedRunnerPool(
        catalog=_catalog(_spec("pure_compute")),
        eligible_tools=("pure_compute",),
        policy=TrustedRunnerPoolPolicy(worker_count=1, max_outstanding=1),
    )
    dependencies = {"source": "value"}

    async def invocation(received: Mapping[str, object]) -> str:
        assert dict(received) == dependencies
        with pytest.raises(TypeError):
            received["other"] = "forbidden"  # type: ignore[index]
        dependencies["source"] = "mutated-after-submit"
        return str(received["source"])

    async with pool:
        report = await _execute(
            pool,
            invocation,
            dependencies=dependencies,
        )
        assert report.result == "value"
        assert report.tool_name == "pure_compute"
        assert report.generation == _GENERATION
        assert report.worker_id == 1
        assert report.decision.allowed
        with pytest.raises(FrozenInstanceError):
            report.worker_id = 2  # type: ignore[misc]

    assert pool.state is TrustedRunnerPoolState.CLOSED
    assert pool.snapshot().completed == 1


@pytest.mark.asyncio
async def test_pool_runs_only_worker_count_invocations_concurrently() -> None:
    pool = TrustedRunnerPool(
        catalog=_catalog(_spec("pure_compute")),
        eligible_tools=("pure_compute",),
        policy=TrustedRunnerPoolPolicy(worker_count=2, max_outstanding=6),
    )
    await pool.start()
    active = 0
    maximum = 0
    first_pair_started = asyncio.Event()
    release = asyncio.Event()

    async def invocation(_dependencies: Mapping[str, object]) -> int:
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        if active == 2:
            first_pair_started.set()
        try:
            await release.wait()
            return active
        finally:
            active -= 1

    tasks = [asyncio.create_task(_execute(pool, invocation)) for _ in range(4)]
    await asyncio.wait_for(first_pair_started.wait(), timeout=1)
    assert pool.snapshot().active == 2
    assert pool.snapshot().pending == 2
    release.set()
    reports = await asyncio.gather(*tasks)

    assert maximum == 2
    assert {report.worker_id for report in reports} == {1, 2}
    assert pool.snapshot().completed == 4
    await pool.close()


@pytest.mark.asyncio
async def test_pool_fails_fast_when_outstanding_limit_is_full() -> None:
    pool = TrustedRunnerPool(
        catalog=_catalog(_spec("pure_compute")),
        eligible_tools=("pure_compute",),
        policy=TrustedRunnerPoolPolicy(worker_count=1, max_outstanding=2),
    )
    await pool.start()
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked(_dependencies: Mapping[str, object]) -> str:
        started.set()
        await release.wait()
        return "ok"

    first = asyncio.create_task(_execute(pool, blocked))
    await started.wait()
    second = asyncio.create_task(_execute(pool, blocked))
    await asyncio.sleep(0)
    assert pool.snapshot().active == 1
    assert pool.snapshot().pending == 1

    with pytest.raises(TrustedRunnerPoolBusy, match="outstanding"):
        await _execute(pool, blocked)
    assert pool.snapshot().rejected == 1

    release.set()
    await asyncio.gather(first, second)
    await pool.close()


@pytest.mark.asyncio
async def test_pool_rechecks_permission_before_enqueue() -> None:
    tool = _spec("admin_compute", permission="superuser")
    pool = TrustedRunnerPool(
        catalog=_catalog(tool),
        eligible_tools=(tool.name,),
    )
    await pool.start()
    called = False

    async def invocation(_dependencies: Mapping[str, object]) -> str:
        nonlocal called
        called = True
        return "ok"

    with pytest.raises(ToolTrustDenied, match="超级用户"):
        await _execute(
            pool,
            invocation,
            tool_name=tool.name,
            is_superuser=False,
        )
    assert called is False
    assert pool.snapshot().pending == 0

    report = await _execute(
        pool,
        invocation,
        tool_name=tool.name,
        is_superuser=True,
    )
    assert report.result == "ok"
    await pool.close()


@pytest.mark.asyncio
async def test_pool_rejects_unlisted_and_invalid_invocations_before_starting() -> None:
    pool = TrustedRunnerPool(
        catalog=_catalog(_spec("pure_compute"), _spec("other_compute")),
        eligible_tools=("pure_compute",),
    )
    await pool.start()

    async def valid(_dependencies: Mapping[str, object]) -> str:
        return "ok"

    with pytest.raises(TrustedRunnerEligibilityError, match="未显式加入"):
        await _execute(pool, valid, tool_name="other_compute")
    with pytest.raises(TrustedRunnerEligibilityError, match="async callable"):
        await _execute(pool, lambda _dependencies: "sync")

    async def bad_signature() -> str:
        return "bad"

    with pytest.raises(TrustedRunnerEligibilityError, match="依赖结果映射"):
        await _execute(pool, bad_signature)
    assert pool.snapshot().completed == 0
    await pool.close()


@pytest.mark.asyncio
async def test_handler_failure_hides_message_and_worker_recovers() -> None:
    pool = TrustedRunnerPool(
        catalog=_catalog(_spec("pure_compute")),
        eligible_tools=("pure_compute",),
        policy=TrustedRunnerPoolPolicy(worker_count=1, max_outstanding=2),
    )
    await pool.start()

    async def failed(_dependencies: Mapping[str, object]) -> str:
        raise RuntimeError("secret-token=do-not-leak")

    with pytest.raises(TrustedRunnerExecutionError) as captured:
        await _execute(pool, failed)
    assert "secret-token" not in str(captured.value)
    assert "do-not-leak" not in str(captured.value)

    async def recovered(_dependencies: Mapping[str, object]) -> str:
        return "recovered"

    assert (await _execute(pool, recovered)).result == "recovered"
    snapshot = pool.snapshot()
    assert snapshot.failed == 1
    assert snapshot.completed == 1
    await pool.close()


@pytest.mark.asyncio
async def test_child_self_cancellation_becomes_safe_error() -> None:
    pool = TrustedRunnerPool(
        catalog=_catalog(_spec("pure_compute")),
        eligible_tools=("pure_compute",),
    )
    await pool.start()

    async def self_cancel(_dependencies: Mapping[str, object]) -> None:
        raise asyncio.CancelledError

    with pytest.raises(TrustedRunnerExecutionError, match="非调用方取消"):
        await _execute(pool, self_cancel)
    assert pool.snapshot().failed == 1
    await pool.close()


@pytest.mark.asyncio
async def test_shared_deadline_cancels_and_drains_running_invocation() -> None:
    pool = TrustedRunnerPool(
        catalog=_catalog(_spec("pure_compute")),
        eligible_tools=("pure_compute",),
        policy=TrustedRunnerPoolPolicy(worker_count=1, max_outstanding=1),
    )
    await pool.start()
    started = asyncio.Event()
    drained = asyncio.Event()

    async def blocked(_dependencies: Mapping[str, object]) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            drained.set()

    with pytest.raises(TrustedRunnerExecutionTimeout, match="deadline"):
        await _execute(pool, blocked, deadline=_deadline(0.03))
    assert started.is_set()
    assert drained.is_set()
    assert pool.snapshot().active == 0
    assert pool.snapshot().timed_out == 1
    await pool.close()


@pytest.mark.asyncio
async def test_queued_deadline_expiry_never_starts_invocation() -> None:
    pool = TrustedRunnerPool(
        catalog=_catalog(_spec("pure_compute")),
        eligible_tools=("pure_compute",),
        policy=TrustedRunnerPoolPolicy(worker_count=1, max_outstanding=2),
    )
    await pool.start()
    started = asyncio.Event()
    release = asyncio.Event()
    queued_called = False

    async def occupied(_dependencies: Mapping[str, object]) -> str:
        started.set()
        await release.wait()
        return "first"

    async def queued(_dependencies: Mapping[str, object]) -> str:
        nonlocal queued_called
        queued_called = True
        return "second"

    first = asyncio.create_task(_execute(pool, occupied))
    await started.wait()
    with pytest.raises(TrustedRunnerExecutionTimeout, match="deadline"):
        await _execute(pool, queued, deadline=_deadline(0.02))

    assert queued_called is False
    assert pool.snapshot().pending == 0
    assert pool.snapshot().timed_out == 1
    release.set()
    assert (await first).result == "first"
    await pool.close()


@pytest.mark.asyncio
async def test_caller_cancellation_drains_handler_and_releases_worker() -> None:
    pool = TrustedRunnerPool(
        catalog=_catalog(_spec("pure_compute")),
        eligible_tools=("pure_compute",),
        policy=TrustedRunnerPoolPolicy(worker_count=1, max_outstanding=1),
    )
    await pool.start()
    started = asyncio.Event()
    drained = asyncio.Event()

    async def blocked(_dependencies: Mapping[str, object]) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            drained.set()

    task = asyncio.create_task(_execute(pool, blocked))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert drained.is_set()
    assert pool.snapshot().cancelled == 1
    assert pool.snapshot().active == 0

    async def recovered(_dependencies: Mapping[str, object]) -> str:
        return "recovered"

    assert (await _execute(pool, recovered)).result == "recovered"
    await pool.close()


@pytest.mark.asyncio
async def test_close_cancels_active_and_rejects_pending_without_starting_it() -> None:
    pool = TrustedRunnerPool(
        catalog=_catalog(_spec("pure_compute")),
        eligible_tools=("pure_compute",),
        policy=TrustedRunnerPoolPolicy(worker_count=1, max_outstanding=2),
    )
    await pool.start()
    started = asyncio.Event()
    drained = asyncio.Event()
    pending_called = False

    async def active(_dependencies: Mapping[str, object]) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            drained.set()

    async def pending(_dependencies: Mapping[str, object]) -> None:
        nonlocal pending_called
        pending_called = True

    active_task = asyncio.create_task(_execute(pool, active))
    await started.wait()
    pending_task = asyncio.create_task(_execute(pool, pending))
    await asyncio.sleep(0)
    assert pool.snapshot().pending == 1

    await pool.close()
    with pytest.raises(TrustedRunnerPoolClosed):
        await active_task
    with pytest.raises(TrustedRunnerPoolClosed):
        await pending_task

    assert drained.is_set()
    assert pending_called is False
    assert pool.state is TrustedRunnerPoolState.CLOSED
    assert pool.snapshot().cancelled == 2


@pytest.mark.asyncio
async def test_pool_rejects_cross_process_reuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = TrustedRunnerPool(
        catalog=_catalog(_spec("pure_compute")),
        eligible_tools=("pure_compute",),
    )
    await pool.start()
    module = importlib.import_module("nonebot_plugin_moellmchats.trusted_runner_pool")
    real_pid = module.os.getpid()

    async def invocation(_dependencies: Mapping[str, object]) -> str:
        return "never"

    with monkeypatch.context() as context:
        context.setattr(module.os, "getpid", lambda: real_pid + 1)
        with pytest.raises(TrustedRunnerPoolOwnershipError, match="跨进程"):
            await _execute(pool, invocation)

    await pool.close()


@pytest.mark.asyncio
async def test_expired_deadline_is_rejected_before_enqueue() -> None:
    pool = TrustedRunnerPool(
        catalog=_catalog(_spec("pure_compute")),
        eligible_tools=("pure_compute",),
    )
    await pool.start()
    called = False

    async def invocation(_dependencies: Mapping[str, object]) -> None:
        nonlocal called
        called = True

    with pytest.raises(TrustedRunnerExecutionTimeout, match="入池前"):
        await _execute(
            pool,
            invocation,
            deadline=DeadlineContext.from_timeout(0),
        )
    assert called is False
    assert pool.snapshot().timed_out == 1
    await pool.close()

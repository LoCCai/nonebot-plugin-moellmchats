from __future__ import annotations

import asyncio
import os
from pathlib import Path
import threading

import pytest

from nonebot_plugin_moellmchats.generated_tool_lifecycle import (
    DraftRecord,
    DraftState,
    LifecycleConflictError,
    LifecycleState,
    plan_record_draft,
)
from nonebot_plugin_moellmchats.generated_tools import (
    GeneratedToolStore,
    PreparedLifecycleChange,
    generated_tool_store,
)
from nonebot_plugin_moellmchats.runtime_metrics import runtime_metrics
import nonebot_plugin_moellmchats.runtime_reload as reload_module
from nonebot_plugin_moellmchats.runtime_reload import (
    RuntimeReloader,
    _RuntimeCandidate,
)
from nonebot_plugin_moellmchats.runtime_snapshot import (
    RuntimeSnapshot,
    runtime_snapshots,
)
from nonebot_plugin_moellmchats.tool_manager import ToolSnapshot

_SOURCE = """async def date_difference(value: int) -> str:
    return f"result={value}"
"""
_TESTS = """async def run_tests(tool_module):
    assert await tool_module.date_difference(3) == "result=3"
    return "1 passed"
"""


def _generated_manifest() -> dict:
    return {
        "bundle_id": "date_math",
        "description": "runtime transaction integration",
        "tools": [
            {
                "name": "date_difference",
                "description": "calculate a difference",
                "parameters": {
                    "type": "object",
                    "properties": {"value": {"type": "integer"}},
                    "required": ["value"],
                },
                "handler": "date_difference",
                "permission": "user",
                "effect": "read_only",
                "timeout_seconds": 5,
                "result_limit": 100,
            }
        ],
    }


def _real_generated_store(tmp_path: Path) -> GeneratedToolStore:
    current = tmp_path
    while current != Path("/tmp") and Path("/tmp") in current.parents:
        os.chmod(current, 0o755)
        current = current.parent
    store = GeneratedToolStore()
    store.root = tmp_path / "generated"
    store.drafts_dir = store.root / "drafts"
    store.versions_dir = store.root / "versions"
    store.active_file = store.root / "active.json"
    store.ensure_initialized()
    return store


def _prepared_change() -> tuple[LifecycleState, PreparedLifecycleChange]:
    before = LifecycleState.empty()
    digest = "a" * 64
    record = DraftRecord(
        draft_id="draft000001",
        bundle_id="weather",
        digest=digest,
        state=DraftState.DRAFT,
        created_at=1.0,
        updated_at=1.0,
    )
    change = PreparedLifecycleChange(
        plan=plan_record_draft(before, record),
        result={"draft_id": record.draft_id},
        generated_source_overrides={
            (record.bundle_id, digest): Path("/staged/weather/tool.py")
        },
    )
    return before, change


def _snapshot(generation: int, state: LifecycleState) -> RuntimeSnapshot:
    tools = ToolSnapshot(
        generation=generation,
        plugin_info={},
        custom_tools={},
        tool_dependencies={},
        mcp_tool_names=set(),
        generated_state_revision=state.revision,
        generated_state_digest=state.state_digest,
        generated_active=state.active,
    )
    return RuntimeSnapshot(
        generation=generation,
        config={},
        model_state=None,
        temperaments={},
        temperament_assignments={},
        replies={},
        tool_snapshot=tools,
        emotions=(),
        reloaded_at=1.0,
        generated_state_revision=state.revision,
        generated_state_digest=state.state_digest,
        generated_active=state.active,
    )


def _candidate(generation: int, state: LifecycleState) -> _RuntimeCandidate:
    return _RuntimeCandidate(
        snapshot=_snapshot(generation, state),
        mcp_servers={},
        mcp_mapping={},
    )


def _configure_transaction(
    monkeypatch: pytest.MonkeyPatch,
    reloader: RuntimeReloader,
    before: LifecycleState,
) -> tuple[RuntimeSnapshot, dict[str, LifecycleState]]:
    previous = _snapshot(17, before)
    canonical = {"state": before}
    monkeypatch.setattr(runtime_snapshots, "_current", previous)
    monkeypatch.setattr(
        generated_tool_store,
        "read_lifecycle_state",
        lambda: canonical["state"],
    )
    monkeypatch.setattr(
        reloader,
        "fingerprint",
        lambda *, generated_state=None: (("stable",),),
    )
    monkeypatch.setattr(reloader, "_record_reload_success", lambda _snapshot: None)
    monkeypatch.setattr(reloader, "_record_reload_failure", lambda _error: None)
    return previous, canonical


def _publish_candidate(
    candidate: _RuntimeCandidate,
    *,
    expected_current: RuntimeSnapshot | object | None,
) -> None:
    runtime_snapshots.publish(
        candidate.snapshot,
        expected_current=expected_current,
    )


@pytest.mark.asyncio
async def test_generated_change_builds_exact_after_state_and_override_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before, change = _prepared_change()
    reloader = RuntimeReloader()
    previous, canonical = _configure_transaction(monkeypatch, reloader, before)
    received: dict[str, object] = {}

    async def build_candidate(
        generation: int,
        *,
        generated_state: LifecycleState,
        generated_source_overrides,
    ) -> _RuntimeCandidate:
        received.update(
            generation=generation,
            state=generated_state,
            overrides=generated_source_overrides,
        )
        return _candidate(generation, generated_state)

    def commit_prepared(prepared: PreparedLifecycleChange) -> LifecycleState:
        assert prepared is change
        canonical["state"] = prepared.plan.after_state
        return canonical["state"]

    monkeypatch.setattr(reloader, "_build_candidate", build_candidate)
    monkeypatch.setattr(reloader, "_commit", _publish_candidate)
    monkeypatch.setattr(
        generated_tool_store,
        "_commit_prepared_internal",
        commit_prepared,
    )

    value, _ = await reloader.apply_generated_change("draft-create", change)

    assert received["generation"] == previous.generation + 1
    assert received["state"] is change.plan.after_state
    assert received["overrides"] is change.generated_source_overrides
    assert value is change.result


@pytest.mark.asyncio
async def test_candidate_failure_performs_no_durable_write_or_runtime_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before, change = _prepared_change()
    reloader = RuntimeReloader()
    previous, _ = _configure_transaction(monkeypatch, reloader, before)
    durable_calls = 0
    publish_calls = 0

    async def fail_build(*_args, **_kwargs) -> _RuntimeCandidate:
        raise ValueError("candidate invalid")

    def commit_prepared(_change: PreparedLifecycleChange) -> LifecycleState:
        nonlocal durable_calls
        durable_calls += 1
        return change.plan.after_state

    def publish(*_args, **_kwargs) -> None:
        nonlocal publish_calls
        publish_calls += 1

    monkeypatch.setattr(reloader, "_build_candidate", fail_build)
    monkeypatch.setattr(reloader, "_commit", publish)
    monkeypatch.setattr(
        generated_tool_store,
        "_commit_prepared_internal",
        commit_prepared,
    )

    with pytest.raises(ValueError, match="candidate invalid"):
        await reloader.apply_generated_change("draft-create", change)

    assert durable_calls == 0
    assert publish_calls == 0
    assert runtime_snapshots.current() is previous


@pytest.mark.asyncio
async def test_durable_cas_conflict_never_publishes_runtime_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before, change = _prepared_change()
    reloader = RuntimeReloader()
    previous, _ = _configure_transaction(monkeypatch, reloader, before)
    publish_calls = 0

    async def build_candidate(generation: int, **_kwargs) -> _RuntimeCandidate:
        return _candidate(generation, change.plan.after_state)

    def conflict(_change: PreparedLifecycleChange) -> LifecycleState:
        raise LifecycleConflictError("durable CAS conflict")

    def publish(*_args, **_kwargs) -> None:
        nonlocal publish_calls
        publish_calls += 1

    monkeypatch.setattr(reloader, "_build_candidate", build_candidate)
    monkeypatch.setattr(reloader, "_commit", publish)
    monkeypatch.setattr(
        generated_tool_store,
        "_commit_prepared_internal",
        conflict,
    )

    with pytest.raises(LifecycleConflictError, match="durable CAS conflict"):
        await reloader.apply_generated_change("draft-create", change)

    assert publish_calls == 0
    assert runtime_snapshots.current() is previous


@pytest.mark.asyncio
async def test_successful_durable_commit_publishes_exact_lifecycle_stamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before, change = _prepared_change()
    reloader = RuntimeReloader()
    previous, canonical = _configure_transaction(monkeypatch, reloader, before)

    async def build_candidate(generation: int, **_kwargs) -> _RuntimeCandidate:
        return _candidate(generation, change.plan.after_state)

    def commit_prepared(_change: PreparedLifecycleChange) -> LifecycleState:
        canonical["state"] = change.plan.after_state
        return canonical["state"]

    monkeypatch.setattr(reloader, "_build_candidate", build_candidate)
    monkeypatch.setattr(reloader, "_commit", _publish_candidate)
    monkeypatch.setattr(
        generated_tool_store,
        "_commit_prepared_internal",
        commit_prepared,
    )

    _, result = await reloader.apply_generated_change("draft-create", change)

    published = runtime_snapshots.current()
    assert published is not None
    assert published is not previous
    assert published.generated_state_revision == canonical["state"].revision
    assert published.generated_state_digest == canonical["state"].state_digest
    assert published.tool_snapshot.generated_state_revision == canonical["state"].revision
    assert published.tool_snapshot.generated_state_digest == canonical["state"].state_digest
    assert result.generated_state_revision == canonical["state"].revision
    assert result.generated_state_digest == canonical["state"].state_digest


@pytest.mark.asyncio
async def test_post_publish_fingerprint_failure_keeps_success_and_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before, change = _prepared_change()
    reloader = RuntimeReloader()
    previous, canonical = _configure_transaction(monkeypatch, reloader, before)
    fingerprint_calls = 0

    async def build_candidate(generation: int, **_kwargs) -> _RuntimeCandidate:
        return _candidate(generation, change.plan.after_state)

    def commit_prepared(_change: PreparedLifecycleChange) -> LifecycleState:
        canonical["state"] = change.plan.after_state
        return canonical["state"]

    def fingerprint(*, generated_state=None) -> tuple:
        nonlocal fingerprint_calls
        fingerprint_calls += 1
        if fingerprint_calls == 3:
            raise OSError("post-publish fingerprint unavailable")
        return (("stable",),)

    monkeypatch.setattr(reloader, "_build_candidate", build_candidate)
    monkeypatch.setattr(reloader, "_commit", _publish_candidate)
    monkeypatch.setattr(reloader, "fingerprint", fingerprint)
    monkeypatch.setattr(
        generated_tool_store,
        "_commit_prepared_internal",
        commit_prepared,
    )

    _, result = await reloader.apply_generated_change("draft-create", change)

    published = runtime_snapshots.current()
    assert published is not previous
    assert published is not None
    assert published.generated_state_revision == canonical["state"].revision
    assert result.converged is True
    assert reloader._fingerprint == ()


@pytest.mark.asyncio
async def test_runtime_publish_failure_keeps_committed_canonical_state_for_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before, change = _prepared_change()
    reloader = RuntimeReloader()
    previous, canonical = _configure_transaction(monkeypatch, reloader, before)
    reloader._fingerprint = (("published",),)

    async def build_candidate(generation: int, **_kwargs) -> _RuntimeCandidate:
        return _candidate(generation, change.plan.after_state)

    def commit_prepared(_change: PreparedLifecycleChange) -> LifecycleState:
        canonical["state"] = change.plan.after_state
        return canonical["state"]

    def fail_publish(*_args, **_kwargs) -> None:
        raise RuntimeError("runtime publish failed")

    monkeypatch.setattr(reloader, "_build_candidate", build_candidate)
    monkeypatch.setattr(reloader, "_commit", fail_publish)
    monkeypatch.setattr(
        generated_tool_store,
        "_commit_prepared_internal",
        commit_prepared,
    )

    with pytest.raises(RuntimeError, match="runtime publish failed"):
        await reloader.apply_generated_change("draft-create", change)

    assert canonical["state"] is change.plan.after_state
    assert runtime_snapshots.current() is previous
    assert reloader._fingerprint == ()


async def _assert_cancellation_waits_for_durable_thread(
    monkeypatch: pytest.MonkeyPatch,
    *,
    cancellation_count: int,
) -> None:
    before, change = _prepared_change()
    reloader = RuntimeReloader()
    _, canonical = _configure_transaction(monkeypatch, reloader, before)
    commit_started = threading.Event()
    release_commit = threading.Event()
    commit_finished = threading.Event()

    async def build_candidate(generation: int, **_kwargs) -> _RuntimeCandidate:
        return _candidate(generation, change.plan.after_state)

    def blocking_commit(_change: PreparedLifecycleChange) -> LifecycleState:
        commit_started.set()
        if not release_commit.wait(timeout=5):
            raise TimeoutError("test did not release durable commit")
        canonical["state"] = change.plan.after_state
        commit_finished.set()
        return canonical["state"]

    monkeypatch.setattr(reloader, "_build_candidate", build_candidate)
    monkeypatch.setattr(reloader, "_commit", _publish_candidate)
    monkeypatch.setattr(
        generated_tool_store,
        "_commit_prepared_internal",
        blocking_commit,
    )

    task = asyncio.create_task(
        reloader.apply_generated_change("draft-create", change)
    )
    try:
        started = await asyncio.wait_for(
            asyncio.to_thread(commit_started.wait, 2),
            timeout=3,
        )
        assert started
        for _ in range(cancellation_count):
            assert task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        release_commit.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=3)
    finally:
        release_commit.set()
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    assert commit_finished.is_set()
    assert canonical["state"] is change.plan.after_state


@pytest.mark.asyncio
async def test_cancellation_during_durable_commit_waits_for_thread_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _assert_cancellation_waits_for_durable_thread(
        monkeypatch,
        cancellation_count=1,
    )


@pytest.mark.asyncio
async def test_repeated_cancellation_still_waits_for_durable_thread_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _assert_cancellation_waits_for_durable_thread(
        monkeypatch,
        cancellation_count=2,
    )


@pytest.mark.asyncio
async def test_generation_advances_current_snapshot_instead_of_stale_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before, change = _prepared_change()
    reloader = RuntimeReloader()
    previous, _ = _configure_transaction(monkeypatch, reloader, before)
    received_generation = -1
    monkeypatch.setattr(runtime_metrics, "reload_generation", 9000)

    async def capture_generation(generation: int, **_kwargs) -> _RuntimeCandidate:
        nonlocal received_generation
        received_generation = generation
        raise RuntimeError("stop after generation capture")

    monkeypatch.setattr(reloader, "_build_candidate", capture_generation)

    with pytest.raises(RuntimeError, match="generation capture"):
        await reloader.apply_generated_change("draft-create", change)

    assert received_generation == previous.generation + 1
    assert received_generation != runtime_metrics.reload_generation + 1


@pytest.mark.asyncio
async def test_ordinary_reload_rejects_lifecycle_drift_during_candidate_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before, change = _prepared_change()
    reloader = RuntimeReloader()
    previous, canonical = _configure_transaction(monkeypatch, reloader, before)
    publish_calls = 0

    async def build_candidate(
        generation: int,
        *,
        generated_state: LifecycleState,
    ) -> _RuntimeCandidate:
        assert generated_state is before
        canonical["state"] = change.plan.after_state
        return _candidate(generation, generated_state)

    def publish(*_args, **_kwargs) -> None:
        nonlocal publish_calls
        publish_calls += 1

    monkeypatch.setattr(reloader, "_build_candidate", build_candidate)
    monkeypatch.setattr(reloader, "_commit", publish)

    with pytest.raises(RuntimeError, match="重载期间再次变化"):
        await reloader.reload("file-watch")

    assert canonical["state"].revision != before.revision
    assert canonical["state"].state_digest != before.state_digest
    assert publish_calls == 0
    assert runtime_snapshots.current() is previous


def _configure_real_generated_loader(
    monkeypatch: pytest.MonkeyPatch,
    store: GeneratedToolStore,
) -> None:
    monkeypatch.setattr(reload_module, "generated_tool_store", store)
    monkeypatch.setattr(runtime_snapshots, "_current", None)
    monkeypatch.setattr(
        reload_module.config_parser,
        "load_candidate",
        lambda: {},
    )
    monkeypatch.setattr(
        reload_module.config_parser,
        "get_config",
        lambda key, default=None: (
            True
            if key == "runtime_watch_enabled"
            else 0
            if key == "runtime_watch_interval_seconds"
            else default
        ),
    )
    monkeypatch.setattr(
        reload_module.model_selector,
        "build_candidate",
        lambda: None,
    )
    monkeypatch.setattr(
        reload_module.temperament_manager,
        "load_candidate",
        lambda: ({}, {}),
    )
    monkeypatch.setattr(reload_module, "load_replies_candidate", lambda: {})
    monkeypatch.setattr(
        reload_module.tool_manager,
        "build_plugin_info",
        lambda: {},
    )

    def load_generated(
        *,
        commit: bool,
        generation: int,
        generated_state,
        generated_source_overrides=None,
        registered_tools=None,
        registered_discovery=None,
    ):
        assert commit is False
        assert registered_tools is not None
        assert registered_discovery is not None
        return store.load_active_tools(
            generation=generation,
            generated_state=generated_state,
            generated_source_overrides=generated_source_overrides,
        )

    monkeypatch.setattr(
        reload_module.tool_manager,
        "load_custom_tools",
        load_generated,
    )
    monkeypatch.setattr(
        reload_module.mcp_manager,
        "load_config_candidate",
        lambda: {},
    )

    async def discover_tools(*, commit: bool, servers, strict: bool):
        assert commit is False
        assert strict is True
        assert servers == {}
        return {}, {}

    monkeypatch.setattr(
        reload_module.mcp_manager,
        "discover_tools",
        discover_tools,
    )
    monkeypatch.setattr(
        reload_module,
        "load_emotions_candidate",
        lambda _config: (),
    )


@pytest.mark.asyncio
async def test_real_generated_management_chain_and_second_watcher_converge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _real_generated_store(tmp_path)
    _configure_real_generated_loader(monkeypatch, store)
    draft_id, validation = store.create_draft(
        _generated_manifest(),
        _SOURCE,
        _TESTS,
        request="real loader transaction",
        review={"approved": True},
    )
    store.mark_static_validated(draft_id)
    store.mark_sandbox_tested(draft_id, "1 passed")
    store.mark_model_reviewed(draft_id, summary="review passed")
    store.mark_awaiting_approval(draft_id)
    review = store.get_draft_review_snapshot(draft_id)
    approval = store.prepare_approval(
        draft_id,
        validation.digest[:12],
        review.review_stamp,
    )

    first = RuntimeReloader()
    monkeypatch.setattr(first, "_commit", _publish_candidate)
    monkeypatch.setattr(
        first,
        "fingerprint",
        lambda *, generated_state=None: (
            (
                "generated",
                generated_state.revision,
                generated_state.state_digest,
            ),
        ),
    )
    _, approved_result = await first.apply_generated_change(
        "generated-tool-approve",
        approval,
    )
    approved_snapshot = runtime_snapshots.current()
    assert approved_snapshot is not None
    approved_schema = approved_snapshot.tool_snapshot.custom_tools[
        "date_difference"
    ]
    assert approved_schema["bundle_digest"] == validation.digest
    assert approved_schema["tool_spec"].permission == "superuser"
    assert approved_snapshot.generated_active == {
        "date_math": validation.digest
    }
    assert approved_result.generated_state_digest == (
        approved_snapshot.generated_state_digest
    )

    permission = store.prepare_permission(
        "date_math",
        validation.digest,
        "date_difference",
        allow_user=True,
        approved_by="pytest",
        require_active=True,
    )
    await first.apply_generated_change("generated-tool-permission", permission)
    permission_snapshot = runtime_snapshots.current()
    assert permission_snapshot is not None
    assert permission_snapshot.tool_snapshot.custom_tools[
        "date_difference"
    ]["tool_spec"].permission == "user"

    deactivation = store.prepare_deactivation("date_math")
    await first.apply_generated_change(
        "generated-tool-deactivate",
        deactivation,
    )
    deactivated_snapshot = runtime_snapshots.current()
    assert deactivated_snapshot is not None
    assert "date_difference" not in (
        deactivated_snapshot.tool_snapshot.custom_tools
    )
    assert not deactivated_snapshot.generated_active

    rollback = store.prepare_rollback("date_math", validation.digest[:12])
    await first.apply_generated_change("generated-tool-rollback", rollback)
    rolled_back_snapshot = runtime_snapshots.current()
    assert rolled_back_snapshot is not None
    assert rolled_back_snapshot.generated_active == {
        "date_math": validation.digest
    }
    assert "date_difference" in rolled_back_snapshot.tool_snapshot.custom_tools

    rejected_id, _ = store.create_draft(
        {**_generated_manifest(), "bundle_id": "rejected_bundle"},
        _SOURCE,
        _TESTS,
        request="reject through reloader",
        review={"approved": None},
    )
    rejection = store.prepare_rejection(
        rejected_id,
        actor="pytest",
        reason="operator rejected",
    )
    await first.apply_generated_change("generated-tool-reject", rejection)
    rejected_state = store.read_lifecycle_state()
    assert rejected_state.drafts[rejected_id].state is DraftState.REJECTED
    assert rejected_state.drafts[rejected_id].evidence[-1].producer == "pytest"
    rolled_back_snapshot = runtime_snapshots.current()
    assert rolled_back_snapshot is not None

    second = RuntimeReloader()
    monkeypatch.setattr(second, "_commit", _publish_candidate)
    monkeypatch.setattr(
        second,
        "fingerprint",
        lambda *, generated_state=None: (
            (
                "generated",
                (
                    store.read_lifecycle_state()
                    if generated_state is None
                    else generated_state
                ).revision,
                (
                    store.read_lifecycle_state()
                    if generated_state is None
                    else generated_state
                ).state_digest,
            ),
        ),
    )

    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(second, "_watch_sleep", no_sleep)
    second._fingerprint = (("stale",),)
    before_generation = rolled_back_snapshot.generation
    await second._watch_once()

    converged = runtime_snapshots.current()
    desired = store.read_lifecycle_state()
    assert converged is not None
    assert converged.generation == before_generation + 1
    assert converged.generated_state_revision == desired.revision
    assert converged.generated_state_digest == desired.state_digest
    assert converged.generated_active == desired.active

from __future__ import annotations

import hashlib
import json
import multiprocessing
from pathlib import Path
import stat
import subprocess
import sys
from typing import Any

import pytest

from nonebot_plugin_moellmchats.generated_tool_lifecycle import (
    DraftEvidence,
    DraftRecord,
    DraftState,
    ImmutableVersionError,
    ImmutableVersionPublish,
    LifecycleCommitUncertainError,
    LifecycleConflictError,
    LifecycleCorruptionError,
    LifecycleLockTimeout,
    LifecyclePlatformError,
    LifecycleState,
    LifecycleStore,
    LifecycleTransitionError,
    PermissionGrant,
    VersionRecord,
    VersionState,
    decode_lifecycle_state,
    encode_lifecycle_state,
    permission_key,
    plan_activate_from_draft,
    plan_activate_version,
    plan_approve_draft,
    plan_archive,
    plan_deactivate,
    plan_permission,
    plan_record_draft,
    plan_reject,
    plan_restore_snapshot,
    plan_rollback,
    plan_transition_draft,
)

_DIGEST_A = "a" * 64
_DIGEST_B = "b" * 64


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def _write_bundle(directory: Path, bundle_id: str = "weather") -> str:
    directory.mkdir(mode=0o700, parents=True)
    manifest = {
        "bundle_id": bundle_id,
        "description": "test bundle",
        "tools": [],
    }
    source = b"async def run():\n    return 'ok'\n"
    tests = b"async def run_tests(tool_module):\n    return None\n"
    (directory / "manifest.json").write_bytes(json.dumps(manifest, ensure_ascii=False, indent=2).encode())
    (directory / "tool.py").write_bytes(source)
    (directory / "tests.py").write_bytes(tests)
    digest = hashlib.sha256(_canonical_json(manifest) + b"\0" + source + b"\0" + tests).hexdigest()
    return digest


def _draft(
    *,
    draft_id: str = "draft000001",
    digest: str = _DIGEST_A,
    state: DraftState = DraftState.DRAFT,
    now: float = 10.0,
) -> DraftRecord:
    evidence_states: tuple[DraftState, ...]
    if state is DraftState.DRAFT:
        evidence_states = ()
    elif state is DraftState.STATIC_VALIDATED:
        evidence_states = (DraftState.STATIC_VALIDATED,)
    elif state is DraftState.SANDBOX_TESTED:
        evidence_states = (
            DraftState.STATIC_VALIDATED,
            DraftState.SANDBOX_TESTED,
        )
    elif state in {
        DraftState.MODEL_REVIEWED,
        DraftState.AWAITING_APPROVAL,
        DraftState.APPROVED,
    }:
        evidence_states = (
            DraftState.STATIC_VALIDATED,
            DraftState.SANDBOX_TESTED,
            DraftState.MODEL_REVIEWED,
        )
    elif state is DraftState.TEST_FAILED:
        evidence_states = (DraftState.STATIC_VALIDATED, DraftState.TEST_FAILED)
    elif state is DraftState.REVIEW_FAILED:
        evidence_states = (
            DraftState.STATIC_VALIDATED,
            DraftState.SANDBOX_TESTED,
            DraftState.REVIEW_FAILED,
        )
    else:
        evidence_states = (state,)
    return DraftRecord(
        draft_id=draft_id,
        bundle_id="weather",
        digest=digest,
        state=state,
        created_at=now,
        updated_at=now,
        evidence=tuple(
            _evidence(target, digest=digest, now=now)
            for target in evidence_states
        ),
    )


def _evidence(
    state: DraftState,
    *,
    digest: str = _DIGEST_A,
    now: float,
) -> DraftEvidence:
    return DraftEvidence(
        state=state,
        draft_digest=digest,
        producer="pytest",
        outcome=(
            "passed"
            if state
            in {
                DraftState.STATIC_VALIDATED,
                DraftState.SANDBOX_TESTED,
                DraftState.MODEL_REVIEWED,
            }
            else "rejected"
            if state is DraftState.REJECTED
            else "failed"
        ),
        summary=f"evidence for {state.value}",
        recorded_at=now,
    )


def _transition_plan(
    state: LifecycleState,
    draft_id: str,
    target: DraftState,
    *,
    now: float,
):
    record = state.drafts[draft_id]
    evidence = (
        None
        if target is DraftState.AWAITING_APPROVAL
        else _evidence(target, digest=record.digest, now=now)
    )
    return plan_transition_draft(
        state,
        draft_id,
        target,
        now=now,
        evidence=evidence,
    )


_SUCCESS_STAGES = (
    DraftState.STATIC_VALIDATED,
    DraftState.SANDBOX_TESTED,
    DraftState.MODEL_REVIEWED,
    DraftState.AWAITING_APPROVAL,
)


def _advance_draft(
    state: LifecycleState,
    record: DraftRecord,
    *,
    target: DraftState = DraftState.AWAITING_APPROVAL,
) -> LifecycleState:
    assert record.state is DraftState.DRAFT
    current = plan_record_draft(state, record).after_state
    if target is DraftState.DRAFT:
        return current
    for offset, stage in enumerate(_SUCCESS_STAGES, start=1):
        current = _transition_plan(
            current,
            record.draft_id,
            stage,
            now=record.updated_at + offset,
        ).after_state
        if stage is target:
            return current
    raise AssertionError(f"unsupported test target: {target}")


def _commit_awaiting(store: LifecycleStore, state: LifecycleState, record: DraftRecord) -> LifecycleState:
    current = store._commit_plan_internal(plan_record_draft(state, record))
    for offset, stage in enumerate(_SUCCESS_STAGES, start=1):
        current = store._commit_plan_internal(
            _transition_plan(
                current,
                record.draft_id,
                stage,
                now=record.updated_at + offset,
            )
        )
    return current


def _activated_state() -> LifecycleState:
    draft = _draft(state=DraftState.APPROVED)
    version = VersionRecord(
        bundle_id="weather",
        digest=_DIGEST_A,
        state=VersionState.ACTIVATED,
        source_draft_id=draft.draft_id,
        created_at=10.0,
        approved_at=11.0,
        activated_at=12.0,
    )
    return LifecycleState(
        revision=3,
        drafts={draft.draft_id: draft},
        versions={"weather": {_DIGEST_A: version}},
        active={"weather": _DIGEST_A},
        permission_grants={},
    )


def _cas_worker(root: str, marker: str, ready, start, results) -> None:
    store = LifecycleStore(Path(root), lock_timeout_seconds=2)
    state = store.load()
    record = DraftRecord(
        draft_id=f"draft{marker * 7}",
        bundle_id="weather",
        digest=marker * 64,
        state=DraftState.DRAFT,
        created_at=1.0,
        updated_at=1.0,
    )
    plan = plan_record_draft(state, record)
    ready.put(marker)
    start.wait(5)
    try:
        store._commit_plan_internal(plan)
    except LifecycleConflictError:
        results.put((marker, "conflict"))
    except BaseException as error:  # pragma: no cover - diagnostic for child failures
        results.put((marker, f"error:{type(error).__name__}:{error}"))
    else:
        results.put((marker, "committed"))


def test_schema_v3_round_trip_is_canonical_and_immutable() -> None:
    state = _activated_state()

    encoded = encode_lifecycle_state(state)
    decoded = decode_lifecycle_state(encoded)

    assert decoded == state
    assert encode_lifecycle_state(decoded) == encoded
    assert decoded.state_digest == state.state_digest
    with pytest.raises(TypeError):
        decoded.active["other"] = _DIGEST_B  # type: ignore[index]
    with pytest.raises(TypeError):
        decoded.versions["weather"][_DIGEST_B] = decoded.versions["weather"][_DIGEST_A]  # type: ignore[index]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update({"unknown": True}),
        lambda value: value.update({"schema_version": 99}),
        lambda value: value["drafts"]["draft000001"].update({"extra": 1}),
        lambda value: value["versions"]["weather"][_DIGEST_A].update({"state": "future_state"}),
    ],
)
def test_unknown_schema_fields_versions_and_states_fail_closed(mutate) -> None:
    raw = _activated_state().as_dict()
    mutate(raw)
    with pytest.raises(LifecycleCorruptionError):
        decode_lifecycle_state(_canonical_json(raw))


def test_duplicate_and_damaged_json_fail_closed() -> None:
    with pytest.raises(LifecycleCorruptionError, match="重复字段"):
        decode_lifecycle_state(b'{"schema_version":2,"schema_version":2}')
    with pytest.raises(LifecycleCorruptionError, match="损坏"):
        decode_lifecycle_state(b'{"schema_version":')


def test_schema_v2_is_upgraded_with_explicit_unverified_evidence() -> None:
    raw = _activated_state().as_dict()
    raw["schema_version"] = 2
    for draft in raw["drafts"].values():
        draft.pop("evidence")

    migrated = decode_lifecycle_state(_canonical_json(raw))

    assert migrated.schema_version == 3
    evidence = migrated.drafts["draft000001"].evidence
    assert [item.state for item in evidence] == [
        DraftState.STATIC_VALIDATED,
        DraftState.SANDBOX_TESTED,
        DraftState.MODEL_REVIEWED,
    ]
    assert {item.producer for item in evidence} == {"schema-v2-migration"}


def test_evidenced_states_and_transitions_fail_closed_without_evidence() -> None:
    with pytest.raises(LifecycleCorruptionError, match="canonical evidence"):
        DraftRecord(
            draft_id="draft000001",
            bundle_id="weather",
            digest=_DIGEST_A,
            state=DraftState.STATIC_VALIDATED,
            created_at=1,
            updated_at=2,
        )

    state = plan_record_draft(LifecycleState.empty(), _draft()).after_state
    with pytest.raises(LifecycleTransitionError, match="canonical evidence"):
        plan_transition_draft(
            state,
            "draft000001",
            DraftState.STATIC_VALIDATED,
            now=20,
        )


def test_active_and_unique_activated_invariants_are_enforced() -> None:
    version_a = VersionRecord(
        bundle_id="weather",
        digest=_DIGEST_A,
        state=VersionState.ACTIVATED,
        source_draft_id=None,
        created_at=1,
        approved_at=1,
        activated_at=1,
    )
    version_b = VersionRecord(
        bundle_id="weather",
        digest=_DIGEST_B,
        state=VersionState.ACTIVATED,
        source_draft_id=None,
        created_at=1,
        approved_at=1,
        activated_at=1,
    )
    with pytest.raises(LifecycleCorruptionError, match="唯一 Activated"):
        LifecycleState(
            revision=0,
            drafts={},
            versions={"weather": {_DIGEST_A: version_a, _DIGEST_B: version_b}},
            active={"weather": _DIGEST_A},
            permission_grants={},
        )
    with pytest.raises(LifecycleCorruptionError, match="必须指向 Activated"):
        LifecycleState(
            revision=0,
            drafts={},
            versions={},
            active={"weather": _DIGEST_A},
            permission_grants={},
        )


def test_explicit_draft_state_machine_and_failures() -> None:
    state = plan_record_draft(
        LifecycleState.empty(),
        _draft(state=DraftState.DRAFT),
    ).after_state
    assert state.revision == 1

    for index, target in enumerate(
        (
            DraftState.STATIC_VALIDATED,
            DraftState.SANDBOX_TESTED,
            DraftState.MODEL_REVIEWED,
            DraftState.AWAITING_APPROVAL,
        ),
        start=2,
    ):
        plan = _transition_plan(
            state,
            "draft000001",
            target,
            now=float(index * 10),
        )
        assert not plan.no_op
        assert plan.expected_revision == index - 1
        state = plan.after_state

    with pytest.raises(LifecycleTransitionError, match="不能从"):
        _transition_plan(
            state,
            "draft000001",
            DraftState.SANDBOX_TESTED,
            now=100,
        )

    failed = _advance_draft(
        LifecycleState.empty(),
        _draft(draft_id="draft000002"),
        target=DraftState.STATIC_VALIDATED,
    )
    failed = _transition_plan(
        failed,
        "draft000002",
        DraftState.TEST_FAILED,
        now=20,
    ).after_state
    assert failed.drafts["draft000002"].state is DraftState.TEST_FAILED
    assert "execution_blocked" not in {item.value for item in DraftState}


def test_activation_permission_deactivate_rollback_and_archive_plans() -> None:
    first = _advance_draft(LifecycleState.empty(), _draft())
    activation = plan_activate_from_draft(
        first,
        "draft000001",
        now=20,
        expected_digest=_DIGEST_A,
    )
    assert activation.after_state.active == {"weather": _DIGEST_A}
    assert activation.after_state.drafts["draft000001"].state is DraftState.APPROVED
    assert activation.after_state.versions["weather"][_DIGEST_A].state is VersionState.ACTIVATED
    assert plan_activate_from_draft(
        activation.after_state,
        "draft000001",
        now=21,
    ).no_op

    permission = plan_permission(
        activation.after_state,
        "weather",
        _DIGEST_A,
        "forecast",
        allow_user=True,
        approved_by="superuser:1",
        now=22,
    )
    key = permission_key("weather", _DIGEST_A, "forecast")
    assert permission.after_state.permission_grants[key].approved_by == "superuser:1"
    assert plan_permission(
        permission.after_state,
        "weather",
        _DIGEST_A,
        "forecast",
        allow_user=True,
        approved_by="superuser:1",
        now=99,
    ).no_op

    second_record = _draft(draft_id="draft000002", digest=_DIGEST_B, now=30)
    second_state = _advance_draft(permission.after_state, second_record)
    second = plan_activate_from_draft(second_state, second_record.draft_id, now=40).after_state
    assert second.active["weather"] == _DIGEST_B
    assert second.versions["weather"][_DIGEST_A].state is VersionState.DEPRECATED

    rolled_back = plan_rollback(second, "weather", _DIGEST_A, now=41).after_state
    assert rolled_back.active["weather"] == _DIGEST_A
    assert rolled_back.versions["weather"][_DIGEST_B].state is VersionState.DEPRECATED
    deactivated = plan_deactivate(rolled_back, "weather", now=42).after_state
    assert "weather" not in deactivated.active
    assert plan_deactivate(deactivated, "weather", now=43).no_op
    with pytest.raises(LifecycleTransitionError, match="仅允许"):
        plan_activate_from_draft(deactivated, "draft000001", now=43)

    archived = plan_archive(deactivated, "weather", _DIGEST_A, now=44).after_state
    assert archived.versions["weather"][_DIGEST_A].state is VersionState.ARCHIVED
    assert key not in archived.permission_grants
    with pytest.raises(LifecycleTransitionError, match="已归档"):
        plan_rollback(archived, "weather", _DIGEST_A, now=45)


def test_model_reviewed_cannot_skip_awaiting_approval() -> None:
    with pytest.raises(LifecycleTransitionError, match="只能登记 Draft"):
        plan_record_draft(
            LifecycleState.empty(),
            _draft(state=DraftState.AWAITING_APPROVAL),
        )
    state = _advance_draft(
        LifecycleState.empty(),
        _draft(),
        target=DraftState.MODEL_REVIEWED,
    )
    with pytest.raises(LifecycleTransitionError, match="不可批准"):
        plan_activate_from_draft(state, "draft000001", now=20)


def test_explicit_approve_then_activate_produces_approved_version() -> None:
    state = _advance_draft(LifecycleState.empty(), _draft())
    approved = plan_approve_draft(
        state,
        "draft000001",
        now=20,
        expected_digest=_DIGEST_A,
    ).after_state
    assert approved.drafts["draft000001"].state is DraftState.APPROVED
    assert approved.versions["weather"][_DIGEST_A].state is VersionState.APPROVED
    assert not approved.active
    with pytest.raises(LifecycleTransitionError, match="只有 Deprecated"):
        plan_archive(approved, "weather", _DIGEST_A, now=21)

    activated = plan_activate_version(
        approved,
        "weather",
        _DIGEST_A,
        now=22,
    ).after_state
    assert activated.active == {"weather": _DIGEST_A}
    assert activated.versions["weather"][_DIGEST_A].state is VersionState.ACTIVATED


def test_reject_is_terminal_and_idempotent() -> None:
    state = _advance_draft(
        LifecycleState.empty(),
        _draft(),
        target=DraftState.MODEL_REVIEWED,
    )
    rejection = _evidence(
        DraftState.REJECTED,
        digest=_DIGEST_A,
        now=20,
    )
    rejected = plan_reject(
        state,
        "draft000001",
        now=20,
        evidence=rejection,
    ).after_state
    assert rejected.drafts["draft000001"].state is DraftState.REJECTED
    assert plan_reject(
        rejected,
        "draft000001",
        now=30,
        evidence=_evidence(
            DraftState.REJECTED,
            digest=_DIGEST_A,
            now=30,
        ),
    ).no_op


def test_store_commit_plan_and_cas_reject_stale_state(tmp_path: Path) -> None:
    store = LifecycleStore(tmp_path / "generated_tools")
    initial = store.load()
    assert initial == LifecycleState.empty()
    assert stat.S_IMODE(store.state_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.lock_file.stat().st_mode) == 0o600

    first_plan = plan_record_draft(initial, _draft(state=DraftState.DRAFT))
    persisted = store._commit_plan_internal(first_plan)
    assert persisted.revision == 1
    assert store.load() == persisted
    with pytest.raises(LifecycleConflictError, match="CAS 冲突"):
        store._commit_plan_internal(first_plan)

    second_plan = _transition_plan(
        persisted,
        "draft000001",
        DraftState.STATIC_VALIDATED,
        now=20,
    )
    swapped = store._compare_and_swap_internal(
        expected_revision=persisted.revision,
        expected_state_digest=persisted.state_digest,
        new_state=second_plan.after_state,
    )
    assert swapped.revision == 2
    with pytest.raises(LifecycleConflictError, match="CAS 冲突"):
        store._compare_and_swap_internal(
            expected_revision=persisted.revision,
            expected_state_digest=persisted.state_digest,
            new_state=second_plan.after_state,
        )

    with store.read_snapshot() as snapshot:
        assert snapshot == swapped
    with store.latest_exclusive_snapshot() as snapshot:
        assert snapshot == swapped


def test_restore_snapshot_compensates_with_new_revision() -> None:
    before = LifecycleState.empty()
    changed_plan = plan_record_draft(before, _draft(state=DraftState.DRAFT))
    changed = changed_plan.after_state
    compensation = plan_restore_snapshot(
        changed,
        before,
        failed_plan=changed_plan,
    )
    assert compensation.operation == "restore_snapshot"
    assert compensation.after_state.revision == changed.revision + 1
    assert not compensation.after_state.drafts
    assert compensation.after_state.active == before.active

    later = _transition_plan(
        changed,
        "draft000001",
        DraftState.STATIC_VALIDATED,
        now=20,
    ).after_state
    with pytest.raises(LifecycleConflictError, match="其他变更"):
        plan_restore_snapshot(later, before, failed_plan=changed_plan)


def test_lifecycle_timestamps_cannot_move_backwards() -> None:
    with pytest.raises(LifecycleCorruptionError, match="activated_at"):
        VersionRecord(
            bundle_id="weather",
            digest=_DIGEST_A,
            state=VersionState.ACTIVATED,
            source_draft_id=None,
            created_at=10,
            approved_at=20,
            activated_at=19,
        )
    state = _activated_state()
    with pytest.raises(LifecycleTransitionError, match="不能倒退"):
        plan_deactivate(state, "weather", now=11)


def test_multiprocess_cas_allows_exactly_one_writer(tmp_path: Path) -> None:
    root = tmp_path / "generated_tools"
    LifecycleStore(root).load()
    context = multiprocessing.get_context("fork")
    ready = context.Queue()
    results = context.Queue()
    start = context.Event()
    processes = [
        context.Process(
            target=_cas_worker,
            args=(str(root), marker, ready, start, results),
        )
        for marker in ("a", "b")
    ]
    for process in processes:
        process.start()
    assert {ready.get(timeout=5), ready.get(timeout=5)} == {"a", "b"}
    start.set()
    outcomes = {results.get(timeout=5), results.get(timeout=5)}
    for process in processes:
        process.join(timeout=5)
        assert process.exitcode == 0
    assert {outcome for _, outcome in outcomes} == {"committed", "conflict"}
    assert LifecycleStore(root).load().revision == 1


def _locking_subprocess(lock_file: Path, *, shared: bool, crash: bool) -> subprocess.Popen[str]:
    operation = "fcntl.LOCK_SH" if shared else "fcntl.LOCK_EX"
    ending = "os._exit(23)" if crash else "sys.exit(0)"
    code = (
        "import fcntl, os, pathlib, sys\n"
        "fd=os.open(pathlib.Path(sys.argv[1]), os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)\n"
        f"fcntl.flock(fd, {operation})\n"
        "print('locked', flush=True)\n"
        "sys.stdin.readline()\n"
        f"{ending}\n"
    )
    return subprocess.Popen(
        [sys.executable, "-c", code, str(lock_file)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_shared_exclusive_lock_contention_is_bounded(tmp_path: Path) -> None:
    store = LifecycleStore(tmp_path / "generated_tools", lock_timeout_seconds=0.15)
    store.load()
    child = _locking_subprocess(store.lock_file, shared=True, crash=False)
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "locked"
        with store.shared_lock(timeout_seconds=0.1):
            pass
        with pytest.raises(LifecycleLockTimeout, match="exclusive"):
            with store.exclusive_lock(timeout_seconds=0.05):
                pass
    finally:
        if child.stdin is not None:
            child.stdin.write("release\n")
            child.stdin.flush()
        child.wait(timeout=5)
    assert child.returncode == 0


def test_process_crash_releases_fixed_flock(tmp_path: Path) -> None:
    store = LifecycleStore(tmp_path / "generated_tools", lock_timeout_seconds=0.1)
    store.load()
    child = _locking_subprocess(store.lock_file, shared=False, crash=True)
    assert child.stdout is not None
    assert child.stdout.readline().strip() == "locked"
    with pytest.raises(LifecycleLockTimeout):
        with store.shared_lock(timeout_seconds=0.03):
            pass
    assert child.stdin is not None
    child.stdin.write("crash\n")
    child.stdin.flush()
    child.wait(timeout=5)
    assert child.returncode == 23
    with store.exclusive_lock(timeout_seconds=0.2):
        pass


def test_lock_descriptor_is_closed_across_exec(tmp_path: Path) -> None:
    store = LifecycleStore(tmp_path / "generated_tools", lock_timeout_seconds=0.1)
    store.load()
    child: subprocess.Popen[str]
    with store.exclusive_lock():
        child = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.readline()"],
            stdin=subprocess.PIPE,
            close_fds=False,
            text=True,
        )
    try:
        with store.exclusive_lock(timeout_seconds=0.05):
            pass
    finally:
        assert child.stdin is not None
        child.stdin.write("exit\n")
        child.stdin.flush()
        child.wait(timeout=5)
    assert child.returncode == 0


def test_lock_rejects_symlink_and_insecure_mode(tmp_path: Path) -> None:
    root = tmp_path / "generated_tools"
    root.mkdir()
    target = tmp_path / "attacker-lock"
    target.write_text("", encoding="utf-8")
    (root / ".lifecycle.lock").symlink_to(target)
    with pytest.raises(LifecycleCorruptionError, match="固定 lifecycle lock"):
        LifecycleStore(root).load()

    (root / ".lifecycle.lock").unlink()
    (root / ".lifecycle.lock").write_text("", encoding="utf-8")
    (root / ".lifecycle.lock").chmod(0o644)
    with pytest.raises(LifecycleCorruptionError, match="权限必须为 0o600"):
        LifecycleStore(root).load()


def test_state_file_rejects_symlink_and_insecure_mode(tmp_path: Path) -> None:
    insecure_root = tmp_path / "insecure"
    insecure = LifecycleStore(insecure_root)
    insecure.load()
    insecure.state_file.chmod(0o644)
    with pytest.raises(LifecycleCorruptionError, match="权限必须为 0o600"):
        insecure.load()

    symlink_root = tmp_path / "symlink"
    symlink = LifecycleStore(symlink_root)
    symlink.load()
    target = tmp_path / "attacker-state"
    target.write_bytes(encode_lifecycle_state(LifecycleState.empty()))
    symlink.state_file.unlink()
    symlink.state_file.symlink_to(target)
    with pytest.raises(LifecycleCorruptionError, match="无法安全读取"):
        symlink.load()


def test_non_posix_platform_fails_closed(tmp_path: Path, monkeypatch) -> None:
    module = sys.modules[LifecycleStore.__module__]
    monkeypatch.setattr(module, "fcntl", None)
    with pytest.raises(LifecyclePlatformError, match="POSIX"):
        LifecycleStore(tmp_path / "generated_tools").load()


def test_legacy_migration_is_strict_and_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "generated_tools"
    draft_path = root / "drafts" / "abc123def456"
    digest = _write_bundle(draft_path)
    (draft_path / "metadata.json").write_text(
        json.dumps(
            {
                "draft_id": "abc123def456",
                "digest": digest,
                "status": "reviewed",
                "created_at": 5.0,
            }
        ),
        encoding="utf-8",
    )
    version_path = root / "versions" / "weather" / digest
    _write_bundle(version_path)
    (version_path / "metadata.json").write_text("{}", encoding="utf-8")
    (root / "active.json").write_text(
        json.dumps({"weather": digest}),
        encoding="utf-8",
    )
    grant_key = permission_key("weather", digest, "forecast")
    (root / "permission_policy.json").write_text(
        json.dumps(
            {
                "version": 1,
                "grants": {
                    grant_key: {
                        "approved": True,
                        "approved_by": "superuser:1",
                        "approved_at": 6.0,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    store = LifecycleStore(root)
    migrated = store.load()
    assert migrated.revision == 0
    assert migrated.active == {"weather": digest}
    assert migrated.drafts["abc123def456"].state is DraftState.AWAITING_APPROVAL
    assert migrated.versions["weather"][digest].state is VersionState.ACTIVATED
    assert migrated.permission_grants[grant_key] == PermissionGrant(
        approved_by="superuser:1",
        approved_at=6.0,
    )
    assert store.state_file.read_bytes() == encode_lifecycle_state(migrated)

    (root / "active.json").write_text("{}", encoding="utf-8")
    (draft_path / "metadata.json").write_text("broken", encoding="utf-8")
    reloaded = LifecycleStore(root).load()
    assert reloaded == migrated
    assert reloaded.active == {"weather": digest}


def test_unknown_legacy_state_fails_without_creating_canonical_state(tmp_path: Path) -> None:
    root = tmp_path / "generated_tools"
    draft_path = root / "drafts" / "abc123def456"
    digest = _write_bundle(draft_path)
    (draft_path / "metadata.json").write_text(
        json.dumps(
            {
                "draft_id": "abc123def456",
                "digest": digest,
                "status": "execution_blocked",
                "created_at": 5.0,
            }
        ),
        encoding="utf-8",
    )
    store = LifecycleStore(root)
    with pytest.raises(LifecycleCorruptionError, match="未知 legacy draft status"):
        store.load()
    assert not store.state_file.exists()


def test_corrupt_canonical_state_never_falls_back_to_legacy(tmp_path: Path) -> None:
    root = tmp_path / "generated_tools"
    store = LifecycleStore(root)
    store.load()
    (root / "active.json").write_text("{}", encoding="utf-8")
    store.state_file.write_text('{"schema_version":2,"unexpected":true}', encoding="utf-8")
    store.state_file.chmod(0o600)
    with pytest.raises(LifecycleCorruptionError, match="字段不匹配"):
        store.load()


def test_durable_immutable_publish_and_commit_plan(tmp_path: Path) -> None:
    root = tmp_path / "generated_tools"
    source = tmp_path / "draft"
    digest = _write_bundle(source)
    store = LifecycleStore(root)
    initial = store.load()
    state = _commit_awaiting(store, initial, _draft(digest=digest))
    activation = plan_activate_from_draft(
        state,
        "draft000001",
        now=20,
        expected_digest=digest,
    )
    committed = store._commit_plan_internal(
        activation,
        publish=ImmutableVersionPublish(source, "weather", digest),
    )
    destination = root / "versions" / "weather" / digest
    assert committed.active == {"weather": digest}
    assert stat.S_IMODE(destination.stat().st_mode) == 0o500
    assert {stat.S_IMODE(path.stat().st_mode) for path in destination.iterdir()} == {0o400}
    assert {path.name for path in destination.iterdir()} == {
        "manifest.json",
        "tool.py",
        "tests.py",
    }
    assert (
        store._publish_immutable_version_internal(source, "weather", digest)
        == destination
    )

    (source / "tool.py").write_text("async def run():\n    return 'changed'\n", encoding="utf-8")
    with pytest.raises(ImmutableVersionError, match="digest 不匹配"):
        store._publish_immutable_version_internal(source, "weather", digest)


def test_noop_plan_cannot_publish_unregistered_version(tmp_path: Path) -> None:
    root = tmp_path / "generated_tools"
    source = tmp_path / "draft"
    digest = _write_bundle(source)
    store = LifecycleStore(root)
    state = store.load()
    noop = plan_deactivate(state, "weather", now=1)
    assert noop.no_op
    with pytest.raises(LifecycleConflictError, match=r"不在 plan\.after_state"):
        store._commit_plan_internal(
            noop,
            publish=ImmutableVersionPublish(source, "weather", digest),
        )
    assert not (root / "versions" / "weather" / digest).exists()


def test_post_replace_directory_fsync_retries_then_succeeds(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "generated_tools"
    store = LifecycleStore(root)
    state = store.load()
    plan = plan_record_draft(state, _draft(state=DraftState.DRAFT))
    module = sys.modules[LifecycleStore.__module__]
    original_fsync_directory = module._fsync_directory
    fsync_calls = 0

    def fail_once(path: Path) -> None:
        nonlocal fsync_calls
        if path == root:
            fsync_calls += 1
            if fsync_calls == 1:
                raise OSError("simulated transient parent fsync failure")
        original_fsync_directory(path)

    monkeypatch.setattr(module, "_fsync_directory", fail_once)
    assert store._commit_plan_internal(plan) == plan.after_state
    assert fsync_calls == 2
    assert store.load().state_digest == plan.after_digest


def test_post_replace_directory_fsync_exhaustion_remains_uncertain(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "generated_tools"
    store = LifecycleStore(root)
    state = store.load()
    plan = plan_record_draft(state, _draft(state=DraftState.DRAFT))
    module = sys.modules[LifecycleStore.__module__]
    original_fsync_directory = module._fsync_directory
    fsync_calls = 0

    def fail_persistently(path: Path) -> None:
        nonlocal fsync_calls
        if path == root:
            fsync_calls += 1
            raise OSError("simulated persistent parent fsync failure")
        original_fsync_directory(path)

    monkeypatch.setattr(module, "_fsync_directory", fail_persistently)
    with pytest.raises(LifecycleCommitUncertainError) as captured:
        store._commit_plan_internal(plan)

    assert captured.value.state_visible is True
    assert captured.value.durability_confirmed is False
    assert fsync_calls == module._STATE_DIRECTORY_FSYNC_ATTEMPTS
    # Visibility is useful for reconciliation, but must not be reported as a
    # durable success after the directory barrier was exhausted.
    assert store.load().state_digest == plan.after_digest


def test_publish_rejects_manifest_bundle_mismatch_and_writable_existing_version(
    tmp_path: Path,
) -> None:
    root = tmp_path / "generated_tools"
    source = tmp_path / "draft"
    digest = _write_bundle(source, bundle_id="different")
    store = LifecycleStore(root)
    store.load()
    with pytest.raises(ImmutableVersionError, match="bundle_id"):
        store._publish_immutable_version_internal(source, "weather", digest)

    correct = tmp_path / "correct"
    correct_digest = _write_bundle(correct)
    destination = store._publish_immutable_version_internal(
        correct,
        "weather",
        correct_digest,
    )
    destination.chmod(0o700)
    with pytest.raises(ImmutableVersionError, match="0500"):
        store._publish_immutable_version_internal(
            correct,
            "weather",
            correct_digest,
        )

from __future__ import annotations

from dataclasses import FrozenInstanceError
import json
import os
from pathlib import Path

import pytest

from nonebot_plugin_moellmchats.generated_tool_lifecycle import (
    DraftState,
    ImmutableVersionError,
    LifecycleCommitUncertainError,
    LifecycleConflictError,
    LifecycleCorruptionError,
    LifecycleTransitionError,
    plan_permission,
)
from nonebot_plugin_moellmchats.generated_tools import GeneratedToolStore


def _manifest(bundle_id: str = "date_math") -> dict:
    return {
        "bundle_id": bundle_id,
        "description": "lifecycle integration bundle",
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


_SOURCE = """async def date_difference(value: int) -> str:
    return f"result={value}"
"""
_TESTS = """async def run_tests(tool_module):
    result = await tool_module.date_difference(3)
    assert result == "result=3"
    return "1 passed"
"""


def _store(tmp_path: Path) -> GeneratedToolStore:
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


def _reviewed_draft(store: GeneratedToolStore) -> tuple[str, str]:
    draft_id, validation = store.create_draft(
        _manifest(),
        _SOURCE,
        _TESTS,
        request="lifecycle",
        review={"approved": True},
    )
    store.mark_static_validated(draft_id)
    store.mark_sandbox_tested(draft_id, "1 passed")
    store.mark_model_reviewed(
        draft_id,
        summary="independent review passed",
    )
    store.mark_awaiting_approval(draft_id)
    return draft_id, validation.digest


def _prepare_approval(
    store: GeneratedToolStore,
    draft_id: str,
    digest: str,
):
    review = store.get_draft_review_snapshot(draft_id)
    return store.prepare_approval(
        draft_id,
        digest[:12],
        review.review_stamp,
    )


def _approve_fixture(
    store: GeneratedToolStore,
    draft_id: str,
    digest: str,
) -> None:
    # Low-level durability fixture only; production mutations go through the
    # RuntimeReloader three-phase transaction.
    store._commit_prepared_internal(
        _prepare_approval(store, draft_id, digest)
    )


def test_create_draft_only_records_draft_and_rejects_legacy_status(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    draft_id, _ = store.create_draft(
        _manifest(),
        _SOURCE,
        _TESTS,
        request="draft only",
        review={"approved": True},
    )
    assert store.read_lifecycle_state().drafts[draft_id].state is DraftState.DRAFT
    with pytest.raises(TypeError, match="status"):
        store.create_draft(
            _manifest(),
            _SOURCE,
            _TESTS,
            request="legacy composite transition",
            review={"approved": True},
            status="reviewed",  # type: ignore[call-arg]
        )


def test_failure_entrypoints_persist_structured_canonical_evidence(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)

    validation_id, _ = store.create_draft(
        _manifest("validation_bundle"),
        _SOURCE,
        _TESTS,
        request="validation failure",
        review={"approved": None},
    )
    validation_state = store.mark_validation_failed(
        validation_id,
        "static validation failed",
    )
    validation_record = validation_state.drafts[validation_id]
    assert validation_record.state is DraftState.VALIDATION_FAILED
    assert validation_record.evidence[-1].outcome == "failed"

    test_id, _ = store.create_draft(
        _manifest("test_bundle"),
        _SOURCE,
        _TESTS,
        request="test failure",
        review={"approved": None},
    )
    store.mark_static_validated(test_id)
    test_state = store.mark_test_failed(test_id, "sandbox test failed")
    assert test_state.drafts[test_id].state is DraftState.TEST_FAILED
    assert test_state.drafts[test_id].evidence[-1].producer == (
        "generated-tool-sandbox"
    )

    review_id, _ = store.create_draft(
        _manifest("review_bundle"),
        _SOURCE,
        _TESTS,
        request="review failure",
        review={"approved": None},
    )
    store.mark_static_validated(review_id)
    store.mark_sandbox_tested(review_id, "1 passed")
    review_state = store.mark_review_failed(
        review_id,
        summary="model rejected",
        risks=("unsafe behavior",),
    )
    review_record = review_state.drafts[review_id]
    assert review_record.state is DraftState.REVIEW_FAILED
    assert review_record.evidence[-1].risks == ("unsafe behavior",)


def test_prepare_approval_is_immutable_and_does_not_publish(tmp_path: Path) -> None:
    store = _store(tmp_path)
    draft_id, digest = _reviewed_draft(store)
    before = store.read_lifecycle_state()

    change = _prepare_approval(store, draft_id, digest)

    assert store.read_lifecycle_state() == before
    assert change.plan.after_state.active["date_math"] == digest
    assert not store.version_path("date_math", digest).exists()
    assert change.publish is not None
    assert change.publish.source_directory == store.drafts_dir / draft_id
    assert change.generated_source_overrides[("date_math", digest)] == (store.drafts_dir / draft_id)
    with pytest.raises(TypeError):
        change.generated_source_overrides[("date_math", digest)] = tmp_path  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        change.result = None  # type: ignore[misc]

    tools, _ = store.load_active_tools(
        generation=7,
        generated_state=change.plan.after_state,
        generated_source_overrides=change.generated_source_overrides,
    )
    assert tools["date_difference"]["bundle_digest"] == digest
    assert tools["date_difference"]["generation"] == 7

    committed = store._commit_prepared_internal(change)
    assert committed == change.plan.after_state
    assert store.version_path("date_math", digest).is_dir()
    assert store.read_active() == {"date_math": digest}


def test_stale_prepared_plan_conflicts(tmp_path: Path) -> None:
    store = _store(tmp_path)
    draft_id, digest = _reviewed_draft(store)
    _approve_fixture(store, draft_id, digest)
    first = store.prepare_deactivation("date_math")
    stale = store.prepare_deactivation("date_math")

    store._commit_prepared_internal(first)
    with pytest.raises(LifecycleConflictError, match="CAS"):
        store._commit_prepared_internal(stale)


def test_uncertain_commit_requires_exact_before_or_after(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    draft_id, digest = _reviewed_draft(store)
    _approve_fixture(store, draft_id, digest)
    before = store.read_lifecycle_state()
    change = store.prepare_deactivation("date_math")
    lifecycle = store._lifecycle()

    def uncertain(*_args, **_kwargs):
        raise LifecycleCommitUncertainError(
            "uncertain",
            state_visible=False,
            durability_confirmed=True,
        )

    monkeypatch.setattr(lifecycle, "_commit_plan_internal", uncertain)
    monkeypatch.setattr(lifecycle, "load", lambda: change.plan.after_state)
    assert store._commit_lifecycle_plan(change.plan) == change.plan.after_state

    monkeypatch.setattr(lifecycle, "load", lambda: before)
    with pytest.raises(LifecycleCommitUncertainError):
        store._commit_lifecycle_plan(change.plan)

    third = plan_permission(
        before,
        "date_math",
        digest,
        "date_difference",
        allow_user=True,
        approved_by="operator",
        now=before.versions["date_math"][digest].activated_at or 0,
    ).after_state
    monkeypatch.setattr(lifecycle, "load", lambda: third)
    with pytest.raises(LifecycleConflictError, match=r"before.*after"):
        store._commit_lifecycle_plan(change.plan)


def test_visible_after_never_resolves_unconfirmed_directory_durability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    draft_id, digest = _reviewed_draft(store)
    _approve_fixture(store, draft_id, digest)
    change = store.prepare_deactivation("date_math")
    lifecycle = store._lifecycle()
    load_called = False

    def uncertain(*_args, **_kwargs):
        raise LifecycleCommitUncertainError(
            "directory fsync exhausted",
            state_visible=True,
            durability_confirmed=False,
        )

    def visible_after():
        nonlocal load_called
        load_called = True
        return change.plan.after_state

    monkeypatch.setattr(lifecycle, "_commit_plan_internal", uncertain)
    monkeypatch.setattr(lifecycle, "load", visible_after)

    with pytest.raises(
        LifecycleCommitUncertainError,
        match="directory fsync exhausted",
    ):
        store._commit_lifecycle_plan(change.plan)
    assert load_called is False


def test_explicit_state_load_does_not_read_lifecycle_again(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    draft_id, digest = _reviewed_draft(store)
    _approve_fixture(store, draft_id, digest)
    state = store.read_lifecycle_state()
    validation = store.validate_bundle(store.version_path("date_math", digest))

    def forbidden_load():
        raise AssertionError("explicit generated_state performed a second load")

    monkeypatch.setattr(store._lifecycle(), "load", forbidden_load)
    assert store.read_active(generated_state=state) == {"date_math": digest}
    assert store.read_permission_policy(generated_state=state)["version"] == 1
    assert (
        store.describe_permissions(
            validation,
            generated_state=state,
        )[0]["effective_permission"]
        == "superuser"
    )
    assert store.get_draft(draft_id, generated_state=state)[1]["canonical_state"] == "approved"
    assert (
        store.get_draft_review_snapshot(
            draft_id,
            generated_state=state,
        ).lifecycle_revision
        == state.revision
    )
    assert store.draft_diff(draft_id, generated_state=state)
    assert store._lifecycle().state_file in store.watched_paths(generated_state=state)
    assert store.list_status(generated_state=state)["active"] == {"date_math": digest}
    tools, _ = store.load_active_tools(generated_state=state)
    assert "date_difference" in tools


def test_review_uses_canonical_state_and_stamp_after_metadata_tamper(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    draft_id, _ = _reviewed_draft(store)
    state = store.read_lifecycle_state()
    assert state.drafts[draft_id].state is DraftState.AWAITING_APPROVAL

    metadata_path = store.drafts_dir / draft_id / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["status"] = "approved"
    metadata["canonical_state"] = "approved"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    snapshot = store.get_draft_review_snapshot(
        draft_id,
        generated_state=state,
    )
    summary = json.loads(snapshot.section_content("summary"))
    assert summary["status"] == "reviewed"
    assert summary["canonical_state"] == "awaiting_approval"
    assert summary["lifecycle_revision"] == state.revision
    assert summary["lifecycle_state_digest"] == state.state_digest
    page = snapshot.get_page("summary")
    assert f"lifecycle_revision: {state.revision}" in page.header
    assert f"lifecycle_state_digest: {state.state_digest}" in page.header
    assert "active_digest: none" in page.header
    assert f"review_stamp: {snapshot.review_stamp}" in page.header
    assert snapshot.approval_command in page.header


def test_review_stamp_rejects_any_lifecycle_change_after_review(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    draft_id, digest = _reviewed_draft(store)
    reviewed = store.get_draft_review_snapshot(draft_id)

    other_id, _ = store.create_draft(
        _manifest("other_bundle"),
        _SOURCE,
        _TESTS,
        request="unrelated canonical change",
        review={"approved": True},
    )
    assert other_id != draft_id
    with pytest.raises(LifecycleConflictError, match="review stamp 已过期"):
        store.prepare_approval(
            draft_id,
            digest[:12],
            reviewed.review_stamp,
        )

    refreshed = store.get_draft_review_snapshot(draft_id)
    assert refreshed.review_stamp != reviewed.review_stamp
    change = store.prepare_approval(
        draft_id,
        digest[:12],
        refreshed.review_stamp,
    )
    assert change.plan.after_state.active["date_math"] == digest


def test_review_stamp_explicitly_binds_active_digest(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first_id, first_digest = _reviewed_draft(store)
    _approve_fixture(store, first_id, first_digest)

    second_source = _SOURCE.replace("result={value}", "result={value + 1}")
    second_tests = _TESTS.replace("result=3", "result=4")
    second_id, second = store.create_draft(
        _manifest(),
        second_source,
        second_tests,
        request="review against active version",
        review={"approved": True},
    )
    store.mark_static_validated(second_id)
    store.mark_sandbox_tested(second_id, "1 passed")
    store.mark_model_reviewed(second_id, summary="review passed")
    store.mark_awaiting_approval(second_id)
    review = store.get_draft_review_snapshot(second_id)
    assert review.active_digest == first_digest
    assert f"active_digest: {first_digest}" in review.get_page("summary").header

    deactivation = store.prepare_deactivation("date_math")
    store._commit_prepared_internal(deactivation)
    with pytest.raises(LifecycleConflictError, match="review stamp 已过期"):
        store.prepare_approval(
            second_id,
            second.digest[:12],
            review.review_stamp,
        )


def test_rollback_requires_exact_readonly_owned_bundle_integrity(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    first_id, first_digest = _reviewed_draft(store)
    _approve_fixture(store, first_id, first_digest)

    second_source = _SOURCE.replace("result={value}", "result={value + 1}")
    second_tests = _TESTS.replace("result=3", "result=4")
    second_id, second = store.create_draft(
        _manifest(),
        second_source,
        second_tests,
        request="second",
        review={"approved": True},
    )
    store.mark_static_validated(second_id)
    store.mark_sandbox_tested(second_id, "1 passed")
    store.mark_model_reviewed(second_id, summary="review passed")
    store.mark_awaiting_approval(second_id)
    _approve_fixture(store, second_id, second.digest)

    first_path = store.version_path("date_math", first_digest)
    first_path.chmod(0o700)
    with pytest.raises(ImmutableVersionError, match="0500"):
        store.prepare_rollback("date_math", first_digest[:12])

    first_path.chmod(0o500)
    tool_path = first_path / "tool.py"
    tool_path.chmod(0o600)
    tool_path.write_text(_SOURCE + "\n# tampered\n", encoding="utf-8")
    tool_path.chmod(0o400)
    with pytest.raises(ImmutableVersionError, match="digest"):
        store.prepare_rollback("date_math", first_digest[:12])

    symlink_target = first_path.with_name(f"{first_digest}.tampered")
    first_path.rename(symlink_target)
    first_path.symlink_to(symlink_target, target_is_directory=True)
    with pytest.raises(ImmutableVersionError, match="禁止符号链接"):
        store.prepare_rollback("date_math", first_digest[:12])


def test_latest_projection_repairs_tampered_legacy_files(tmp_path: Path) -> None:
    first = _store(tmp_path)
    draft_id, digest = _reviewed_draft(first)
    _approve_fixture(first, draft_id, digest)
    permission_change = first.prepare_permission(
        "date_math",
        digest,
        "date_difference",
        allow_user=True,
        approved_by="operator",
    )
    first._commit_prepared_internal(permission_change)
    metadata_path = first.drafts_dir / draft_id / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))

    first.active_file.write_text("{}", encoding="utf-8")
    first.permission_policy_file.write_text(
        '{"version":1,"grants":{}}',
        encoding="utf-8",
    )
    metadata["status"] = "draft"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    second = _store(tmp_path)
    state = second.read_lifecycle_state()
    assert json.loads(second.active_file.read_text(encoding="utf-8")) == {"date_math": digest}
    policy = json.loads(second.permission_policy_file.read_text(encoding="utf-8"))
    assert len(policy["grants"]) == 1
    repaired = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert repaired["status"] == "approved"
    assert repaired["lifecycle_revision"] == state.revision
    assert repaired["lifecycle_state_digest"] == state.state_digest


def test_projection_failure_does_not_hide_canonical_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    draft_id, digest = _reviewed_draft(store)
    _approve_fixture(store, draft_id, digest)
    change = store.prepare_deactivation("date_math")

    def fail_projection(_state):
        raise OSError("projection unavailable")

    monkeypatch.setattr(store, "_project_lifecycle_state", fail_projection)
    committed = store._commit_prepared_internal(change)

    assert committed == change.plan.after_state
    assert store.read_lifecycle_state().active == {}
    status = store.list_status(generated_state=committed)
    assert status["legacy_projection_stale"] is True
    assert "projection unavailable" in status["legacy_projection_error"]


def test_older_store_repairs_projection_from_latest_revision(tmp_path: Path) -> None:
    older = _store(tmp_path)
    first_id, first_digest = _reviewed_draft(older)
    _approve_fixture(older, first_id, first_digest)
    old_state = older.read_lifecycle_state()

    newer = _store(tmp_path)
    second_source = _SOURCE.replace("result={value}", "result={value + 1}")
    second_tests = _TESTS.replace("result=3", "result=4")
    second_id, second = newer.create_draft(
        _manifest(),
        second_source,
        second_tests,
        request="newer revision",
        review={"approved": True},
    )
    newer.mark_static_validated(second_id)
    newer.mark_sandbox_tested(second_id, "1 passed")
    newer.mark_model_reviewed(
        second_id,
        summary="independent review passed",
    )
    newer.mark_awaiting_approval(second_id)
    _approve_fixture(newer, second_id, second.digest)
    latest = newer.read_lifecycle_state()
    assert latest.revision > old_state.revision

    projected = older.repair_legacy_projections(strict=True)
    assert projected == latest
    assert json.loads(older.active_file.read_text(encoding="utf-8")) == {"date_math": second.digest}


def test_canonical_corruption_never_falls_back_to_legacy(tmp_path: Path) -> None:
    store = _store(tmp_path)
    draft_id, digest = _reviewed_draft(store)
    _approve_fixture(store, draft_id, digest)
    lifecycle_file = store._lifecycle().state_file
    lifecycle_file.write_text("{broken", encoding="utf-8")

    reloaded = GeneratedToolStore()
    reloaded.root = store.root
    reloaded.drafts_dir = store.drafts_dir
    reloaded.versions_dir = store.versions_dir
    reloaded.active_file = store.active_file
    with pytest.raises(LifecycleCorruptionError):
        reloaded.read_lifecycle_state()


def test_replace_active_is_not_an_authoritative_entrypoint(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(LifecycleTransitionError, match=r"replace_active.*停用"):
        store.replace_active({})


def test_runtime_mutators_are_not_public_store_entrypoints(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for name in (
        "approve",
        "commit_prepared",
        "set_user_permission_policy",
        "reject",
        "deactivate",
        "rollback",
    ):
        assert not hasattr(store, name)

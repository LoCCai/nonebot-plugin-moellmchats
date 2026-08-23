from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time

import pytest

from nonebot_plugin_moellmchats import _parse_draft_review_args
from nonebot_plugin_moellmchats.custom_tool_loader import load_file_tools
from nonebot_plugin_moellmchats.generated_tool_runner import GeneratedToolRunner
from nonebot_plugin_moellmchats.generated_tools import (
    DraftReviewPage,
    DraftReviewSnapshot,
    GeneratedToolStore,
)
from nonebot_plugin_moellmchats.tool_contracts import ToolSpec
from nonebot_plugin_moellmchats.tool_manager import ToolManager, ToolSnapshot


def _manifest(bundle_id: str = "date_math", *, permission: str = "user") -> dict:
    return {
        "bundle_id": bundle_id,
        "description": "test bundle",
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
                "permission": permission,
                "effect": "read_only",
                "timeout_seconds": 5,
                "result_limit": 100,
            }
        ],
    }


_SOURCE = """async def date_difference(value: int) -> str:
    \"\"\"calculate a difference\"\"\"
    return f\"result={value}\"
"""


def _processes_named(token: str) -> list[str]:
    matches: list[str] = []
    for item in Path("/proc").iterdir():
        if not item.name.isdigit():
            continue
        try:
            if (item / "comm").read_text().strip() == token:
                matches.append(item.name)
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
    return matches
_TESTS = """async def run_tests(tool_module):
    result = await tool_module.date_difference(3)
    assert result == \"result=3\"
    return \"1 passed\"
"""

_REQUIRES_ROOT = pytest.mark.skipif(
    os.geteuid() != 0,
    reason="hard runner behavior requires root so the worker can drop to nobody",
)


def _can_unshare_network() -> bool:
    unshare = shutil.which("unshare")
    if not unshare:
        return False
    result = subprocess.run(
        [unshare, "--net", "--fork", "/bin/true"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=2,
    )
    return result.returncode == 0


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
    store._init_files()
    return store


def _mark_reviewed(
    store: GeneratedToolStore,
    draft_id: str,
    *,
    summary: str = "independent review passed",
) -> None:
    store.mark_static_validated(draft_id)
    store.mark_sandbox_tested(draft_id, "1 passed")
    store.mark_model_reviewed(draft_id, summary=summary)
    store.mark_awaiting_approval(draft_id)


def _approve_fixture(
    store: GeneratedToolStore,
    draft_id: str,
    digest: str,
) -> tuple[str, str]:
    _mark_reviewed(store, draft_id)
    review = store.get_draft_review_snapshot(draft_id)
    change = store.prepare_approval(
        draft_id,
        digest[:12],
        review.review_stamp,
    )
    store._commit_prepared_internal(change)
    result = tuple(change.result)
    assert len(result) == 2
    return result[0], result[1]


def _permission_fixture(
    store: GeneratedToolStore,
    bundle_id: str,
    digest: str,
    *,
    allow_user: bool,
) -> dict:
    change = store.prepare_permission(
        bundle_id,
        digest,
        "date_difference",
        allow_user=allow_user,
        approved_by="superuser:1",
    )
    store._commit_prepared_internal(change)
    return dict(change.result)


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_generated_permission_requires_persisted_human_policy(tmp_path: Path) -> None:
    store = _store(tmp_path)
    draft_id, validation = store.create_draft(
        _manifest(permission="user"),
        _SOURCE,
        _TESTS,
        request="permission",
        review={"approved": True},
    )
    bundle_id, digest = _approve_fixture(store, draft_id, validation.digest)

    tools, _ = store.load_active_tools()
    schema = tools["date_difference"]
    assert schema["requested_permission"] == "user"
    assert schema["effective_permission"] == "superuser"
    assert schema["tool_spec"].permission == "superuser"
    assert not schema["user_policy_approved"]

    status = store.list_status()
    assert status["drafts"][0]["tools"][0]["requested_permission"] == "user"
    assert status["active_tools"][0]["tools"][0]["effective_permission"] == "superuser"

    granted = _permission_fixture(
        store,
        bundle_id,
        digest,
        allow_user=True,
    )
    assert granted["effective_permission"] == "user"
    assert granted["user_policy_approved"]

    # The grant is tied to the exact digest and survives a store re-instantiation.
    reloaded = _store(tmp_path)
    tools, _ = reloaded.load_active_tools()
    assert tools["date_difference"]["tool_spec"].permission == "user"
    revoked = _permission_fixture(
        reloaded,
        bundle_id,
        digest,
        allow_user=False,
    )
    assert revoked["effective_permission"] == "superuser"


def test_generated_superuser_request_cannot_be_relaxed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    draft_id, validation = store.create_draft(
        _manifest(permission="superuser"),
        _SOURCE,
        _TESTS,
        request="admin",
        review={"approved": True},
    )
    bundle_id, digest = _approve_fixture(store, draft_id, validation.digest)
    with pytest.raises(ValueError, match="未请求 user"):
        store.prepare_permission(
            bundle_id,
            digest,
            "date_difference",
            allow_user=True,
            approved_by="superuser:1",
        )


def test_generated_capabilities_are_bounded_by_safe_policy(tmp_path: Path) -> None:
    store = _store(tmp_path)
    manifest = _manifest()
    manifest["capabilities"] = {
        "network": True,
        "process": True,
        "workspace": True,
        "host_filesystem": True,
        "secrets": True,
    }
    draft_id, validation = store.create_draft(
        manifest,
        _SOURCE,
        _TESTS,
        request="capabilities",
        review={"approved": True},
    )
    _approve_fixture(store, draft_id, validation.digest)
    tools, _ = store.load_active_tools()
    schema = tools["date_difference"]
    assert schema["requested_capabilities"] == {
        "network": True,
        "process": True,
        "workspace": True,
        "host_filesystem": True,
        "secrets": True,
    }
    assert schema["effective_capabilities"] == {
        "network": False,
        "process": False,
        "workspace": True,
        "host_filesystem": False,
        "secrets": False,
    }
    assert not schema["tool_spec"].policy.effective.network
    assert not schema["tool_spec"].policy.effective.process
    assert not schema["tool_spec"].policy.effective.host_filesystem
    assert not schema["tool_spec"].policy.effective.secrets
    assert schema["tool_contract_version"] == 2
    assert schema["artifact_digest_version"] == 2
    assert schema["detected_capabilities"] == {
        "network": False,
        "process": False,
        "workspace": False,
        "host_filesystem": False,
        "secrets": False,
    }


def test_generated_structured_request_is_digest_bound_but_not_auto_granted(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    manifest = _manifest()
    manifest["capabilities"] = {
        "network": {"allow": ["api.example"]},
        "filesystem": {"workspace": True, "host": False},
        "database": {"read": True, "write": False},
        "bot": False,
        "secrets": False,
    }
    draft_id, validation = store.create_draft(
        manifest,
        _SOURCE,
        _TESTS,
        request="structured capabilities",
        review={"approved": True},
    )
    _approve_fixture(store, draft_id, validation.digest)
    tools, _ = store.load_active_tools()
    schema = tools["date_difference"]
    policy = schema["tool_spec"].policy

    assert policy.requested_v2.network_allow == ("api.example",)
    assert policy.requested_v2.database_read is True
    assert policy.effective_v2.network_allow == ()
    assert policy.effective_v2.database_read is False
    assert policy.effective.network is False
    assert schema["capability_policy"]["requested"]["network"] == {
        "allow": ["api.example"]
    }
    assert schema["tool_artifact"].contract.requested_capabilities_v2[
        "database"
    ]["read"] is True


def test_generated_manifest_rejects_unknown_capability(tmp_path: Path) -> None:
    store = _store(tmp_path)
    manifest = _manifest()
    manifest["capabilities"] = {"kernel": True}
    with pytest.raises(ValueError, match="manifest capabilities 非法"):
        store.create_draft(
            manifest,
            _SOURCE,
            _TESTS,
            request="bad capability",
            review={"approved": True},
        )


def test_generated_storage_permissions_are_private_and_versions_stay_immutable(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    draft_id, validation = store.create_draft(
        _manifest(),
        _SOURCE,
        _TESTS,
        request="modes",
        review={"approved": True},
    )
    draft = store.drafts_dir / draft_id
    assert _mode(store.root) == 0o700
    assert _mode(store.drafts_dir) == 0o700
    assert _mode(store.active_file) == 0o600
    assert _mode(store.permission_policy_file) == 0o600
    assert _mode(draft) == 0o700
    assert {_mode(path) for path in draft.iterdir()} == {0o600}

    bundle_id, digest = _approve_fixture(store, draft_id, validation.digest)
    version = store.version_path(bundle_id, digest)
    assert _mode(version) == 0o500
    assert {_mode(path) for path in version.iterdir()} == {0o400}
    assert version.stat().st_mode & 0o222 == 0


@pytest.mark.asyncio
async def test_bundle_approve_deactivate_and_rollback(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first_id, first = store.create_draft(_manifest(), _SOURCE, _TESTS, request="first", review={"approved": True})
    runner = GeneratedToolRunner()
    if _can_unshare_network():
        assert "1 passed" in await runner.run_tests(store.drafts_dir / first_id)
    else:
        with pytest.raises(RuntimeError, match=r"隔离|unshare"):
            await runner.run_tests(store.drafts_dir / first_id)
    bundle_id, first_digest = _approve_fixture(store, first_id, first.digest)
    assert store.read_active()[bundle_id] == first_digest

    second_source = _SOURCE.replace("result={value}", "result={value + 1}")
    second_tests = _TESTS.replace("result=3", "result=4")
    second_id, second = store.create_draft(
        _manifest(),
        second_source,
        second_tests,
        request="second",
        review={"approved": True},
    )
    _approve_fixture(store, second_id, second.digest)
    assert store.read_active()[bundle_id] == second.digest
    rollback_change = store.prepare_rollback(bundle_id, first_digest[:10])
    store._commit_prepared_internal(rollback_change)
    assert rollback_change.result == first_digest
    deactivate_change = store.prepare_deactivation(bundle_id)
    store._commit_prepared_internal(deactivate_change)
    assert deactivate_change.result
    assert bundle_id not in store.read_active()


def test_hash_change_and_failed_review_cannot_be_approved(tmp_path: Path) -> None:
    store = _store(tmp_path)
    draft_id, validation = store.create_draft(
        _manifest(),
        _SOURCE,
        _TESTS,
        request="blocked",
        review={"approved": False},
    )
    store.mark_static_validated(draft_id)
    store.mark_sandbox_tested(draft_id, "1 passed")
    store.mark_review_failed(
        draft_id,
        summary="independent review rejected",
    )
    review = store.get_draft_review_snapshot(draft_id)
    with pytest.raises(ValueError, match="不可批准"):
        store.prepare_approval(
            draft_id,
            validation.digest[:12],
            review.review_stamp,
        )
    (store.drafts_dir / draft_id / "tool.py").write_text(_SOURCE + "\n# changed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="哈希"):
        store.get_draft(draft_id)


def test_complete_draft_review_is_canonical_lossless_and_bounded(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    manifest = _manifest()
    manifest["description"] = "超长说明🙂" * 900
    source = "# " + ("中文🙂e\u0301" * 1400) + "\n" + _SOURCE
    tests_source = "# " + ("测试🧪" * 1200) + "\n" + _TESTS
    draft_id, validation = store.create_draft(
        manifest,
        source,
        tests_source,
        request="分页请求📄" * 300,
        review={"approved": True, "summary": "逐字审阅✅" * 350},
    )
    _mark_reviewed(store, draft_id)

    snapshot = store.get_draft_review_snapshot(draft_id)
    lifecycle = store.read_lifecycle_state()
    assert isinstance(snapshot, DraftReviewSnapshot)
    assert snapshot.digest == validation.digest
    assert snapshot.lifecycle_revision == lifecycle.revision
    assert snapshot.lifecycle_state_digest == lifecycle.state_digest
    summary = json.loads(snapshot.section_content("summary"))
    assert summary["status"] == "reviewed"
    assert summary["canonical_state"] == "awaiting_approval"
    assert summary["lifecycle_revision"] == lifecycle.revision
    assert summary["lifecycle_state_digest"] == lifecycle.state_digest
    assert snapshot.available_sections == (
        "summary",
        "manifest",
        "source",
        "tests",
        "risks",
        "capabilities",
        "diff",
    )
    assert snapshot.section_content("source") == source
    assert snapshot.section_content("tests") == tests_source
    assert snapshot.section_content("manifest") == json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    for structured_section in (
        "summary",
        "manifest",
        "risks",
        "capabilities",
    ):
        content = snapshot.section_content(structured_section)
        assert content == json.dumps(
            json.loads(content),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    diff = snapshot.section_content("diff")
    assert "===== manifest.json =====" in diff
    assert "===== tool.py =====" in diff
    assert "===== tests.py =====" in diff

    for section in snapshot.available_sections:
        pages = snapshot.pages(section)
        content = snapshot.section_content(section)
        expected_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        assert "".join(page.content for page in pages) == content
        assert [page.page for page in pages] == list(range(1, len(pages) + 1))
        assert all(isinstance(page, DraftReviewPage) for page in pages)
        assert all(page.total_pages == len(pages) for page in pages)
        assert all(page.content_sha256 == expected_hash for page in pages)
        assert all(len(page.text) <= 1800 for page in pages)
        assert all(f"digest: {validation.digest}" in page.header for page in pages)
        assert all(f"lifecycle_revision: {lifecycle.revision}" in page.header for page in pages)
        assert all(f"lifecycle_state_digest: {lifecycle.state_digest}" in page.header for page in pages)
        assert all(f"section: {section}" in page.header for page in pages)

    with pytest.raises(FrozenInstanceError):
        snapshot.digest = "0" * 64  # type: ignore[misc]


def test_draft_review_diff_includes_manifest_source_and_tests(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    first_id, first = store.create_draft(
        _manifest(),
        _SOURCE,
        _TESTS,
        request="first",
        review={"approved": True},
    )
    _approve_fixture(store, first_id, first.digest)

    manifest = _manifest()
    manifest["description"] = "changed manifest"
    second_source = _SOURCE.replace("result={value}", "changed={value}")
    second_tests = _TESTS.replace("result=3", "changed=3")
    second_id, _ = store.create_draft(
        manifest,
        second_source,
        second_tests,
        request="second",
        review={"approved": True},
    )
    diff = store.get_draft_review_snapshot(second_id).section_content("diff")

    assert "changed manifest" in diff
    assert "changed={value}" in diff
    assert "changed=3" in diff
    assert diff.count("===== ") == 3


def test_draft_review_snapshot_holds_lock_and_survives_later_tamper(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    draft_id, validation = store.create_draft(
        _manifest(),
        _SOURCE,
        _TESTS,
        request="race",
        review={"approved": True},
    )
    _mark_reviewed(store, draft_id)
    original_validate = store.validate_bundle
    validation_entered = threading.Event()
    release_validation = threading.Event()
    approve_started = threading.Event()
    approve_done = threading.Event()

    def gated_validate(path: Path):
        result = original_validate(path)
        if not validation_entered.is_set():
            validation_entered.set()
            if not release_validation.wait(timeout=3):
                raise RuntimeError("test review validation gate timed out")
        return result

    store.validate_bundle = gated_validate  # type: ignore[method-assign]

    def approve() -> tuple[str, str]:
        approve_started.set()
        try:
            current_review = store.get_draft_review_snapshot(draft_id)
            change = store.prepare_approval(
                draft_id,
                validation.digest[:12],
                current_review.review_stamp,
            )
            store._commit_prepared_internal(change)
            return tuple(change.result)  # type: ignore[return-value]
        finally:
            approve_done.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        review_future = pool.submit(store.get_draft_review_snapshot, draft_id)
        assert validation_entered.wait(timeout=3)
        approve_future = pool.submit(approve)
        assert approve_started.wait(timeout=3)
        assert not approve_done.wait(timeout=0.05)
        release_validation.set()
        snapshot = review_future.result(timeout=3)
        assert approve_future.result(timeout=3)[1] == validation.digest

    assert json.loads(snapshot.section_content("summary"))["status"] == "reviewed"
    original_source = snapshot.section_content("source")
    draft_source = store.drafts_dir / draft_id / "tool.py"
    os.chmod(draft_source, 0o600)
    draft_source.write_text(_SOURCE + "\n# tampered\n", encoding="utf-8")
    assert snapshot.section_content("source") == original_source
    with pytest.raises(ValueError, match="哈希"):
        store.get_draft_review_snapshot(draft_id)


def test_draft_review_rejects_links_traversal_invalid_sections_and_pages(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    draft_id, _ = store.create_draft(
        _manifest(),
        _SOURCE,
        _TESTS,
        request="paths",
        review={"approved": True},
    )
    snapshot = store.get_draft_review_snapshot(draft_id)
    with pytest.raises(ValueError, match="草稿 ID 非法"):
        store.get_draft_review_snapshot("../../etc/passwd")
    with pytest.raises(ValueError, match="区段非法"):
        snapshot.get_page("secrets", 1)
    with pytest.raises(ValueError, match="从 1 开始"):
        snapshot.get_page("source", 0)
    with pytest.raises(ValueError, match="页码越界"):
        snapshot.get_page("source", 999)

    external = tmp_path / "external.py"
    external.write_text(_SOURCE, encoding="utf-8")
    source_path = store.drafts_dir / draft_id / "tool.py"
    source_path.unlink()
    source_path.symlink_to(external)
    with pytest.raises(ValueError, match=r"非普通文件|符号链接"):
        store.get_draft_review_snapshot(draft_id)


def test_draft_review_command_legacy_and_extended_grammar() -> None:
    draft_id = "012345abcdef"
    assert _parse_draft_review_args(draft_id) == (draft_id, "summary", 1)
    assert _parse_draft_review_args(f"{draft_id} source") == (
        draft_id,
        "source",
        1,
    )
    assert _parse_draft_review_args(f"{draft_id} tests 2") == (
        draft_id,
        "tests",
        2,
    )
    with pytest.raises(ValueError, match="格式"):
        _parse_draft_review_args("")
    with pytest.raises(ValueError, match="格式"):
        _parse_draft_review_args(f"{draft_id} source 1 extra")
    with pytest.raises(ValueError, match="区段非法"):
        _parse_draft_review_args(f"{draft_id} unknown")
    with pytest.raises(ValueError, match="从 1 开始"):
        _parse_draft_review_args(f"{draft_id} source 0")


def test_file_tool_is_parsed_without_import_and_rejects_bot_context(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "marker"
    source = tmp_path / "safe.py"
    source.write_text(
        f"async def safe(value: str) -> str:\n" f"    '{marker}'\n" "    return value\n",
        encoding="utf-8",
    )
    tools, _ = load_file_tools([source])
    assert "safe" in tools
    assert not marker.exists()

    source.write_text(
        "async def unsafe(_bot=None):\n    return str(_bot)\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="register_tool"):
        load_file_tools([source])


def test_legacy_registry_is_strictly_validated(tmp_path: Path) -> None:
    source = tmp_path / "registry.py"
    source.write_text(
        "async def safe(value: str):\n    return value\n\n"
        "TOOLS_REGISTRY = [{\n"
        "  'name': 'safe', 'description': 'safe tool',\n"
        "  'parameters': {'type': 'array'}, 'func': safe,\n"
        "}]\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=r"parameters\.type"):
        load_file_tools([source])


def test_stale_parallel_approval_requires_fresh_review_stamp(tmp_path: Path) -> None:
    store = _store(tmp_path)
    drafts = []
    for index in range(2):
        source = _SOURCE.replace("result={value}", f"result={{value + {index}}}")
        tests = _TESTS.replace("result=3", f"result={3 + index}")
        draft_id, validation = store.create_draft(
            _manifest(),
            source,
            tests,
            request=str(index),
            review={"approved": True},
        )
        _mark_reviewed(store, draft_id)
        drafts.append((draft_id, validation.digest))

    prepared = []
    for draft_id, digest in drafts:
        review = store.get_draft_review_snapshot(draft_id)
        prepared.append(
            store.prepare_approval(
                draft_id,
                digest[:12],
                review.review_stamp,
            )
        )

    store._commit_prepared_internal(prepared[0])
    with pytest.raises(Exception, match=r"CAS|过期"):
        store._commit_prepared_internal(prepared[1])

    second_id, second_digest = drafts[1]
    refreshed = store.get_draft_review_snapshot(second_id)
    second_change = store.prepare_approval(
        second_id,
        second_digest[:12],
        refreshed.review_stamp,
    )
    store._commit_prepared_internal(second_change)

    active_digest = store.read_active()["date_math"]
    assert active_digest in {digest for _, digest in drafts}
    for _, digest in drafts:
        path = store.version_path("date_math", digest)
        assert store.validate_bundle(path).digest == digest
        assert path.stat().st_mode & 0o222 == 0


@pytest.mark.asyncio
async def test_runner_preflight_verifies_nobody_identity() -> None:
    runner = GeneratedToolRunner()
    if os.geteuid() == 0:
        await runner.preflight()
        assert runner.isolation_status == "ready"
    else:
        # Preflight verifies the aggregate isolation contract.  An ordinary
        # non-root CI user may fail at network namespace creation before the
        # worker reaches the UID transition; either prerequisite must fail
        # closed and preserve a specific unavailable status.
        with pytest.raises(RuntimeError):
            await runner.preflight()
        assert runner.isolation_status.startswith("unavailable:")


def test_duplicate_sources_fail_closed() -> None:
    async def handler():
        return "ok"

    spec = ToolSpec(
        name="duplicate",
        description="duplicate",
        parameters={"type": "object", "properties": {}},
        handler=handler,
    )
    target = {"duplicate": {**spec.as_legacy_schema(), "source": "registered"}}
    with pytest.raises(ValueError, match="工具名冲突"):
        ToolManager._merge_unique_tools(
            target,
            {"duplicate": {**spec.as_legacy_schema(), "source": "generated"}},
        )


def test_superuser_tools_are_filtered_from_catalog_and_schema() -> None:
    async def handler():
        return "ok"

    spec = ToolSpec(
        name="admin_only",
        description="admin only",
        parameters={"type": "object", "properties": {}},
        handler=handler,
        permission="superuser",
    )
    snapshot = ToolSnapshot(
        generation=1,
        plugin_info={},
        custom_tools={"admin_only": spec.as_legacy_schema()},
        tool_dependencies={},
        mcp_tool_names=set(),
    )
    assert snapshot.get_tool_schema(["admin_only"], is_superuser=False) == []
    assert snapshot.get_tool_schema(["admin_only"], is_superuser=True)


@pytest.mark.asyncio
@_REQUIRES_ROOT
async def test_runner_kills_timed_out_process(tmp_path: Path, monkeypatch) -> None:
    current = tmp_path
    while current != Path("/tmp") and Path("/tmp") in current.parents:
        os.chmod(current, 0o755)
        current = current.parent
    source = tmp_path / "loop.py"
    source.write_text(
        "async def loop():\n    while True:\n        pass\n",
        encoding="utf-8",
    )
    source.chmod(0o644)
    module = importlib.import_module("nonebot_plugin_moellmchats.generated_tool_runner")

    original = module.config_parser.get_config

    def configured(key, default=None):
        if key == "generated_tool_timeout_seconds":
            return 1
        return original(key, default)

    monkeypatch.setattr(module.config_parser, "get_config", configured)
    runner = GeneratedToolRunner()
    with pytest.raises(RuntimeError, match="超时"):
        await runner.execute(source, "loop", {}, {})
    await asyncio.sleep(0)
    assert runner._semaphore._value == 1


@pytest.mark.asyncio
@_REQUIRES_ROOT
async def test_runner_drops_privileges_and_scrubs_environment(tmp_path: Path) -> None:
    current = tmp_path
    while current != Path("/tmp") and Path("/tmp") in current.parents:
        os.chmod(current, 0o755)
        current = current.parent
    source = tmp_path / "identity.py"
    source.write_text(
        "import os\n\n"
        "async def identity():\n"
        "    return {'text': f\"uid={os.geteuid()} secret={bool(os.getenv('QI_WEB_DATABASE_URL'))}\"}\n",
        encoding="utf-8",
    )
    source.chmod(0o644)
    result = await GeneratedToolRunner().execute(source, "identity", {}, {})
    assert result.text == "uid=65534 secret=False"


@pytest.mark.asyncio
@_REQUIRES_ROOT
async def test_runner_rejects_output_flood(tmp_path: Path) -> None:
    current = tmp_path
    while current != Path("/tmp") and Path("/tmp") in current.parents:
        os.chmod(current, 0o755)
        current = current.parent
    source = tmp_path / "flood.py"
    source.write_text(
        "import os\n\n" "async def flood():\n" "    os.write(1, b'x' * 70000)\n" "    return 'done'\n",
        encoding="utf-8",
    )
    source.chmod(0o644)
    with pytest.raises(RuntimeError, match="输出超过"):
        await GeneratedToolRunner().execute(source, "flood", {}, {})


@pytest.mark.asyncio
@_REQUIRES_ROOT
async def test_runner_rejects_fd3_protocol_result_flood(tmp_path: Path) -> None:
    current = tmp_path
    while current != Path("/tmp") and Path("/tmp") in current.parents:
        os.chmod(current, 0o755)
        current = current.parent
    source = tmp_path / "protocol_flood.py"
    source.write_text(
        "async def protocol_flood():\n" "    return 'x' * 70000\n",
        encoding="utf-8",
    )
    source.chmod(0o644)
    with pytest.raises(
        RuntimeError,
        match=r"ValueError: tool result exceeds 48 KiB",
    ):
        await GeneratedToolRunner().execute_custom(
            source,
            "protocol_flood",
            {},
            {},
            allow_network=True,
            allow_process=False,
        )


@pytest.mark.asyncio
@_REQUIRES_ROOT
async def test_runner_cleans_spawned_descendants(tmp_path: Path) -> None:
    current = tmp_path
    while current != Path("/tmp") and Path("/tmp") in current.parents:
        os.chmod(current, 0o755)
        current = current.parent
    source = tmp_path / "spawn.py"
    token = f"mlm{os.getpid():x}{time.time_ns():x}"[-15:]
    source.write_text(
        "import ctypes\n"
        "import os\n"
        "import time\n\n"
        "async def spawn():\n"
        "    ready_read, ready_write = os.pipe()\n"
        "    child = os.fork()\n"
        "    if child == 0:\n"
        "        os.close(ready_read)\n"
        "        os.setsid()\n"
        f"        token = {token!r}.encode()\n"
        "        if ctypes.CDLL(None).prctl(15, ctypes.c_char_p(token), 0, 0, 0):\n"
        "            os._exit(91)\n"
        "        os.write(ready_write, b'1')\n"
        "        os.close(ready_write)\n"
        "        time.sleep(60)\n"
        "        os._exit(0)\n"
        "    os.close(ready_write)\n"
        "    if os.read(ready_read, 1) != b'1':\n"
        "        raise RuntimeError('child setup failed')\n"
        "    os.close(ready_read)\n"
        f"    return {token!r}\n",
        encoding="utf-8",
    )
    source.chmod(0o644)
    result = await GeneratedToolRunner().execute_custom(
        source,
        "spawn",
        {},
        {},
        allow_network=True,
        allow_process=True,
    )
    assert result.text == token
    matches = await asyncio.to_thread(_processes_named, token)
    assert matches == []


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.geteuid() != 0 or not _can_unshare_network(),
    reason="generated process isolation needs root and a network namespace",
)
async def test_generated_runner_rejects_subprocess_before_execution(tmp_path: Path) -> None:
    current = tmp_path
    while current != Path("/tmp") and Path("/tmp") in current.parents:
        os.chmod(current, 0o755)
        current = current.parent
    source = tmp_path / "blocked_spawn.py"
    source.write_text(
        "import subprocess\n\n"
        "async def blocked_spawn():\n"
        "    subprocess.run(['/bin/true'], check=True)\n"
        "    return 'unexpected'\n",
        encoding="utf-8",
    )
    source.chmod(0o644)
    with pytest.raises(
        RuntimeError,
        match=r"generated import is denied: subprocess",
    ):
        await GeneratedToolRunner().execute_generated(source, "blocked_spawn", {}, {})


@pytest.mark.asyncio
@_REQUIRES_ROOT
async def test_process_false_blocks_exec_replacing_worker(tmp_path: Path) -> None:
    current = tmp_path
    while current != Path("/tmp") and Path("/tmp") in current.parents:
        os.chmod(current, 0o755)
        current = current.parent
    source = tmp_path / "blocked_exec.py"
    source.write_text(
        "import os\n\n"
        "async def blocked_exec():\n"
        "    try:\n"
        "        os.execv('/bin/echo', ['echo', 'exec-bypass'])\n"
        "    except PermissionError as error:\n"
        "        return f'blocked:{error.errno}'\n"
        "    return 'unexpected'\n",
        encoding="utf-8",
    )
    source.chmod(0o644)

    result = await GeneratedToolRunner().execute_custom(
        source,
        "blocked_exec",
        {},
        {},
        allow_network=True,
        allow_process=False,
    )

    assert result.text == "blocked:1"


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.geteuid() != 0 or not _can_unshare_network(),
    reason="generated network isolation needs root and a network namespace",
)
async def test_generated_runner_rejects_network_import_before_execution(tmp_path: Path) -> None:
    current = tmp_path
    while current != Path("/tmp") and Path("/tmp") in current.parents:
        os.chmod(current, 0o755)
        current = current.parent
    server = await asyncio.start_server(lambda _reader, _writer: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    source = tmp_path / "network_probe.py"
    source.write_text(
        "import socket\n\n"
        "async def network_probe(port: int):\n"
        "    sock = socket.socket()\n"
        "    sock.settimeout(0.5)\n"
        "    try:\n"
        "        sock.connect(('127.0.0.1', port))\n"
        "    except OSError:\n"
        "        return 'blocked'\n"
        "    finally:\n"
        "        sock.close()\n"
        "    return 'connected'\n",
        encoding="utf-8",
    )
    source.chmod(0o644)
    try:
        with pytest.raises(
            RuntimeError,
            match=r"generated import is denied: socket",
        ):
            await GeneratedToolRunner().execute_generated(
                source,
                "network_probe",
                {"port": port},
                {},
            )
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@_REQUIRES_ROOT
async def test_runner_stops_memory_expansion(tmp_path: Path) -> None:
    current = tmp_path
    while current != Path("/tmp") and Path("/tmp") in current.parents:
        os.chmod(current, 0o755)
        current = current.parent
    source = tmp_path / "memory.py"
    source.write_text(
        "async def memory():\n" "    blocks = []\n" "    while True:\n" "        blocks.append(b'x' * 16_000_000)\n",
        encoding="utf-8",
    )
    source.chmod(0o644)
    with pytest.raises(RuntimeError, match="MemoryError"):
        await GeneratedToolRunner().execute(source, "memory", {}, {})


@pytest.mark.asyncio
@_REQUIRES_ROOT
async def test_runner_cancellation_kills_process_and_releases_slot(tmp_path: Path) -> None:
    current = tmp_path
    while current != Path("/tmp") and Path("/tmp") in current.parents:
        os.chmod(current, 0o755)
        current = current.parent
    source = tmp_path / "sleep.py"
    source.write_text(
        "import asyncio\n\n" "async def sleep_forever():\n" "    await asyncio.sleep(60)\n",
        encoding="utf-8",
    )
    source.chmod(0o644)
    runner = GeneratedToolRunner()
    task = asyncio.create_task(runner.execute(source, "sleep_forever", {}, {}))
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runner._semaphore._value == 1


@pytest.mark.asyncio
async def test_runner_fails_closed_when_non_root_isolation_is_unavailable(
    tmp_path: Path,
) -> None:
    if os.geteuid() == 0:
        pytest.skip("root environment exercises the successful privilege drop tests")
    source = tmp_path / "closed.py"
    source.write_text("async def closed():\n    return 'no'\n", encoding="utf-8")
    runner = GeneratedToolRunner()
    with pytest.raises(
        RuntimeError,
        match=r"(?:强隔离.*已拒绝执行|nobody identity)",
    ):
        # An ordinary non-root CI user can fail at namespace creation before
        # reaching the UID transition.  Either missing prerequisite must reject
        # execution; the mandatory root suite separately proves the 65534 drop.
        await runner.execute_custom(
            source,
            "closed",
            {},
            {},
            allow_network=True,
            allow_process=False,
        )
    assert runner.isolation_status.startswith("unavailable:")

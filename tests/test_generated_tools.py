from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import importlib
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from nonebot_plugin_moellmchats.custom_tool_loader import load_file_tools
from nonebot_plugin_moellmchats.generated_tool_runner import GeneratedToolRunner
from nonebot_plugin_moellmchats.generated_tools import GeneratedToolStore
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


@pytest.mark.asyncio
async def test_bundle_approve_deactivate_and_rollback(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first_id, first = store.create_draft(
        _manifest(), _SOURCE, _TESTS, request="first", review={"approved": True}
    )
    runner = GeneratedToolRunner()
    if _can_unshare_network():
        assert "1 passed" in await runner.run_tests(store.drafts_dir / first_id)
    else:
        with pytest.raises(RuntimeError, match=r"隔离不可用|unshare"):
            await runner.run_tests(store.drafts_dir / first_id)
    bundle_id, first_digest = store.approve(first_id, first.digest[:12])
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
    store.approve(second_id, second.digest[:12])
    assert store.read_active()[bundle_id] == second.digest
    assert store.rollback(bundle_id, first_digest[:10]) == first_digest
    assert store.deactivate(bundle_id)
    assert bundle_id not in store.read_active()


def test_hash_change_and_failed_review_cannot_be_approved(tmp_path: Path) -> None:
    store = _store(tmp_path)
    draft_id, validation = store.create_draft(
        _manifest(),
        _SOURCE,
        _TESTS,
        request="blocked",
        review={"approved": False},
        status="review_failed",
    )
    with pytest.raises(ValueError, match="不可批准"):
        store.approve(draft_id, validation.digest[:12])
    (store.drafts_dir / draft_id / "tool.py").write_text(
        _SOURCE + "\n# changed\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="哈希"):
        store.get_draft(draft_id)


def test_file_tool_is_parsed_without_import_and_rejects_bot_context(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "marker"
    source = tmp_path / "safe.py"
    source.write_text(
        f"async def safe(value: str) -> str:\n"
        f"    '{marker}'\n"
        "    return value\n",
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


def test_concurrent_approvals_publish_complete_immutable_versions(tmp_path: Path) -> None:
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
        drafts.append((draft_id, validation.digest))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda item: store.approve(item[0], item[1][:12]), drafts)
        )
    active_digest = store.read_active()["date_math"]
    assert active_digest in {digest for _, digest in drafts}
    for _, digest in results:
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
        with pytest.raises(RuntimeError, match="nobody identity"):
            await runner.preflight()
        assert runner.isolation_status == "unavailable:RuntimeError"


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
    module = importlib.import_module(
        "nonebot_plugin_moellmchats.generated_tool_runner"
    )

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
        "import os\n\n"
        "async def flood():\n"
        "    os.write(1, b'x' * 70000)\n"
        "    return 'done'\n",
        encoding="utf-8",
    )
    source.chmod(0o644)
    with pytest.raises(RuntimeError, match="输出超过"):
        await GeneratedToolRunner().execute(source, "flood", {}, {})


@pytest.mark.asyncio
@_REQUIRES_ROOT
async def test_runner_cleans_spawned_descendants(tmp_path: Path) -> None:
    current = tmp_path
    while current != Path("/tmp") and Path("/tmp") in current.parents:
        os.chmod(current, 0o755)
        current = current.parent
    source = tmp_path / "spawn.py"
    source.write_text(
        "import subprocess\n\n"
        "async def spawn():\n"
        "    child = subprocess.Popen(\n"
        "        ['/bin/sleep', '60'],\n"
        "        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,\n"
        "    )\n"
        "    return str(child.pid)\n",
        encoding="utf-8",
    )
    source.chmod(0o644)
    result = await GeneratedToolRunner().execute(source, "spawn", {}, {})
    child_pid = int(result.text)
    await asyncio.sleep(0.1)
    stat_path = Path(f"/proc/{child_pid}/stat")
    child_state = await asyncio.to_thread(
        lambda: stat_path.read_text().split()[2] if stat_path.exists() else None
    )
    if child_state is not None:
        assert child_state == "Z"


@pytest.mark.asyncio
@_REQUIRES_ROOT
async def test_runner_stops_memory_expansion(tmp_path: Path) -> None:
    current = tmp_path
    while current != Path("/tmp") and Path("/tmp") in current.parents:
        os.chmod(current, 0o755)
        current = current.parent
    source = tmp_path / "memory.py"
    source.write_text(
        "async def memory():\n"
        "    blocks = []\n"
        "    while True:\n"
        "        blocks.append(b'x' * 16_000_000)\n",
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
        "import asyncio\n\n"
        "async def sleep_forever():\n"
        "    await asyncio.sleep(60)\n",
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
async def test_runner_fails_closed_when_nobody_transition_is_unavailable(
    tmp_path: Path,
) -> None:
    if os.geteuid() == 0:
        pytest.skip("root environment exercises the successful privilege drop tests")
    source = tmp_path / "closed.py"
    source.write_text("async def closed():\n    return 'no'\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="nobody identity"):
        await GeneratedToolRunner().execute(source, "closed", {}, {})

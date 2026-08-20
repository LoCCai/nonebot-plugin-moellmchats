from __future__ import annotations

import asyncio
import importlib
import os
from pathlib import Path
import time

import pytest

from nonebot_plugin_moellmchats.config import (
    DEFAULT_CONFIG,
    ConfigParser,
    config_parser,
)
from nonebot_plugin_moellmchats.generated_tool_runner import (
    GeneratedToolRunner,
    WorkspaceLimits,
)
from nonebot_plugin_moellmchats.tool_artifacts import (
    ToolArtifact,
    ToolContractSnapshot,
    canonical_bundle_digest,
    source_sha256,
)
from nonebot_plugin_moellmchats.tool_contracts import (
    ToolCapability,
    ToolEffect,
    ToolPolicy,
    ToolResult,
    ToolSpec,
)


def _custom_artifact(
    source: bytes,
    *,
    generation: int = 4,
    workspace: bool = True,
    host_filesystem: bool = False,
    secrets: bool = False,
) -> ToolArtifact:
    async def placeholder(value: str = "") -> str:
        return value

    capability = ToolCapability(
        network=True,
        process=False,
        workspace=workspace,
        host_filesystem=host_filesystem,
        secrets=secrets,
    )
    policy = ToolPolicy(requested=capability, admin=capability)
    parameters = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    spec = ToolSpec(
        name="snapshot_echo",
        description="return the snapshotted value",
        parameters=parameters,
        handler=placeholder,
        policy=policy,
    )
    return ToolArtifact(
        tool_name=spec.name,
        handler_name="snapshot_echo",
        source=source,
        source_hash=source_sha256(source),
        schema={
            "name": spec.name,
            "description": spec.description,
            "parameters": parameters,
        },
        spec=spec,
        contract=ToolContractSnapshot.from_spec(spec),
        source_type="custom_file",
        generation=generation,
        filename="snapshot.py",
    )


def _generated_artifact(
    source: bytes,
    *,
    generation: int = 4,
) -> ToolArtifact:
    async def placeholder() -> str:
        return "placeholder"

    parameters = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    policy = ToolPolicy.generated()
    spec = ToolSpec(
        name="snapshot_echo",
        description="return the snapshotted value",
        parameters=parameters,
        handler=placeholder,
        permission="superuser",
        timeout_seconds=30,
        result_limit=6000,
        policy=policy,
    )
    tests_source = b"async def run_tests(tool_module):\n    return 'ok'\n"
    manifest = {
        "bundle_id": "snapshot_bundle",
        "description": "snapshot bundle",
        "tools": [
            {
                "name": spec.name,
                "description": spec.description,
                "parameters": parameters,
                "handler": "snapshot_echo",
                "permission": "user",
                "effect": "read_only",
                "timeout_seconds": 30,
                "result_limit": 6000,
                "dependencies": [],
            }
        ],
    }
    bundle_digest = canonical_bundle_digest(
        manifest,
        source,
        tests_source,
    )
    return ToolArtifact(
        tool_name=spec.name,
        handler_name="snapshot_echo",
        source=source,
        source_hash=source_sha256(source),
        schema={
            "name": spec.name,
            "description": spec.description,
            "parameters": parameters,
        },
        spec=spec,
        contract=ToolContractSnapshot.from_spec(
            spec,
            requested_permission="user",
            declared_effect=ToolEffect.READ_ONLY,
        ),
        source_type="generated",
        generation=generation,
        filename="tool.py",
        tests_source=tests_source,
        bundle_manifest=manifest,
        bundle_id="snapshot_bundle",
        bundle_digest=bundle_digest,
    )


@pytest.mark.parametrize(
    "field",
    [
        "generated_tool_workspace_max_files",
        "generated_tool_workspace_max_depth",
        "generated_tool_workspace_max_file_bytes",
    ],
)
@pytest.mark.parametrize("invalid", [0, -1, True, 1.5])
def test_workspace_limit_config_requires_positive_integers(
    field: str,
    invalid: object,
) -> None:
    candidate = dict(DEFAULT_CONFIG)
    candidate[field] = invalid
    with pytest.raises(ValueError, match=field):
        ConfigParser._validate(candidate)


@pytest.mark.asyncio
async def test_generated_and_compatibility_entrypoints_force_network_isolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = GeneratedToolRunner()
    policies: list[tuple[bool, bool, bool]] = []

    async def invoke(
        bundle: Path,
        handler: str,
        arguments: dict,
        context: dict,
        *,
        disable_network: bool,
        allow_process: bool,
        generated_runtime_guard: bool = False,
    ) -> dict:
        policies.append(
            (disable_network, allow_process, generated_runtime_guard)
        )
        return {"ok": True, "text": "ok", "images": []}

    monkeypatch.setattr(runner, "_invoke", invoke)
    arguments = {"value": 1}
    context = {"request_id": 2}

    generated = await runner.execute_generated(
        Path("bundle"), "handler", arguments, context
    )
    compatibility = await runner.execute(
        Path("bundle"), "handler", arguments, context
    )

    assert generated.text == compatibility.text == "ok"
    assert policies == [(True, False, True), (True, False, True)]


@pytest.mark.asyncio
async def test_custom_entrypoint_requires_and_applies_explicit_network_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = GeneratedToolRunner()
    policies: list[tuple[bool, bool, bool, bool]] = []

    async def invoke(
        bundle: Path,
        handler: str,
        arguments: dict,
        context: dict,
        *,
        disable_network: bool,
        allow_process: bool,
        allow_host_filesystem: bool = False,
        allow_secrets: bool = False,
    ) -> dict:
        policies.append(
            (
                disable_network,
                allow_process,
                allow_host_filesystem,
                allow_secrets,
            )
        )
        return {"ok": True, "text": "ok", "images": []}

    monkeypatch.setattr(runner, "_invoke", invoke)

    await runner.execute_custom(
        Path("bundle"),
        "handler",
        {},
        {},
        allow_network=False,
        allow_process=False,
    )
    await runner.execute_custom(
        Path("bundle"),
        "handler",
        {},
        {},
        allow_network=True,
        allow_process=True,
        allow_host_filesystem=True,
        allow_secrets=True,
    )
    with pytest.raises(TypeError, match="allow_network"):
        await runner.execute_custom(
            Path("bundle"),
            "handler",
            {},
            {},
            allow_network="yes",  # type: ignore[arg-type]
            allow_process=False,
        )
    with pytest.raises(TypeError, match="allow_host_filesystem"):
        await runner.execute_custom(
            Path("bundle"),
            "handler",
            {},
            {},
            allow_network=False,
            allow_process=False,
            allow_host_filesystem="yes",  # type: ignore[arg-type]
        )

    assert policies == [
        (True, False, False, False),
        (False, True, True, True),
    ]


@pytest.mark.asyncio
async def test_generated_execution_fails_closed_without_unshare(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = GeneratedToolRunner()
    runner_module = importlib.import_module(
        "nonebot_plugin_moellmchats.generated_tool_runner"
    )
    monkeypatch.setattr(
        runner_module.shutil,
        "which",
        lambda _command: None,
    )

    with pytest.raises(RuntimeError, match=r"Generated Tool.*unshare.*拒绝执行"):
        await runner.execute_generated(Path("bundle"), "handler", {}, {})

    assert runner.isolation_status == "unavailable:no-unshare"
    assert runner._semaphore._value == 1


def test_isolation_command_has_mount_pid_and_kill_boundary() -> None:
    command = GeneratedToolRunner()._isolation_command(disable_network=True)

    assert command[1:10] == [
        "--mount",
        "--ipc",
        "--uts",
        "--pid",
        "--fork",
        "--kill-child=SIGKILL",
        "--propagation",
        "private",
        "--net",
    ]
    assert command[-3] == "-I"
    assert command[-2].endswith("generated_tool_isolation.py")
    assert command[-1].endswith("generated_tool_worker.py")


@pytest.mark.asyncio
async def test_preflight_uses_generated_policy_and_checks_network_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = GeneratedToolRunner()
    calls: list[tuple[str, dict]] = []

    async def execute_generated(
        bundle: Path,
        handler: str,
        arguments: dict,
        context: dict,
    ) -> ToolResult:
        assert bundle.name == "probe.py"
        calls.append((handler, arguments))
        return ToolResult(
            text=(
                "65534:65534:True:True:True:True:"
                "moellm-sandbox:localdomain"
            )
        )

    monkeypatch.setattr(runner, "execute_generated", execute_generated)

    await runner.preflight()

    assert len(calls) == 1
    handler, arguments = calls[0]
    assert handler == "probe"
    assert set(arguments) == {
        "parent_net_ns",
        "parent_ipc_ns",
        "parent_uts_ns",
    }
    assert str(arguments["parent_net_ns"]).startswith("net:[")
    assert str(arguments["parent_ipc_ns"]).startswith("ipc:[")
    assert str(arguments["parent_uts_ns"]).startswith("uts:[")
    assert runner.isolation_status == "ready"


@pytest.mark.asyncio
async def test_preflight_preserves_specific_network_isolation_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = GeneratedToolRunner()

    async def execute_generated(*_args, **_kwargs) -> ToolResult:
        runner.isolation_status = "unavailable:no-network-namespace"
        raise RuntimeError("Generated Tool 网络隔离不可用")

    monkeypatch.setattr(runner, "execute_generated", execute_generated)

    with pytest.raises(RuntimeError, match="网络隔离不可用"):
        await runner.preflight()

    assert runner.isolation_status == "unavailable:no-network-namespace"


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.geteuid() != 0,
    reason="real artifact runner needs root so the worker can drop to nobody",
)
async def test_artifact_execution_uses_snapshot_and_fd3_not_live_path(
    tmp_path: Path,
) -> None:
    live_source = tmp_path / "live.py"
    live_source.write_text(
        "import os\n\n"
        "async def snapshot_echo():\n"
        "    os.write(1, b'{\\\"protocol_version\\\":999}')\n"
        "    os.write(2, b'tool-stderr')\n"
        "    return 'snapshot-old'\n",
        encoding="utf-8",
    )
    artifact = _custom_artifact(live_source.read_bytes())
    live_source.write_text(
        "async def snapshot_echo():\n    return 'live-new'\n",
        encoding="utf-8",
    )

    result = await GeneratedToolRunner().execute_artifact(
        artifact,
        {},
        {},
        expected_artifact_digest=artifact.artifact_digest,
        expected_bundle_digest=None,
        generation=artifact.generation,
    )

    assert result.text == "snapshot-old"


@pytest.mark.asyncio
async def test_artifact_entry_rejects_unpinned_digest_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = _custom_artifact(
        b"async def snapshot_echo():\n    return 'ok'\n"
    )
    invoked = False

    async def invoke_snapshot(*_args, **_kwargs):
        nonlocal invoked
        invoked = True
        return {"ok": True, "text": "unexpected", "images": []}

    runner = GeneratedToolRunner()
    monkeypatch.setattr(runner, "_invoke_snapshot", invoke_snapshot)
    with pytest.raises(ValueError, match="artifact digest"):
        await runner.execute_artifact(
            artifact,
            {},
            {},
            expected_artifact_digest="0" * 64,
            expected_bundle_digest=None,
            generation=artifact.generation,
        )
    assert invoked is False


@pytest.mark.asyncio
async def test_generated_artifact_rejects_wrong_bundle_digest_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = _generated_artifact(
        b"async def snapshot_echo():\n    return 'ok'\n"
    )
    invoked = False

    async def invoke_snapshot(*_args, **_kwargs):
        nonlocal invoked
        invoked = True
        return {"ok": True, "text": "unexpected", "images": []}

    runner = GeneratedToolRunner()
    monkeypatch.setattr(runner, "_invoke_snapshot", invoke_snapshot)
    with pytest.raises(ValueError, match="bundle digest"):
        await runner.execute_artifact(
            artifact,
            {},
            {},
            expected_artifact_digest=artifact.artifact_digest,
            expected_bundle_digest="0" * 64,
            generation=artifact.generation,
        )
    assert invoked is False


def test_workspace_scanner_enforces_files_depth_bytes_and_links(
    tmp_path: Path,
) -> None:
    runner = GeneratedToolRunner()
    byte_limits = WorkspaceLimits(
        total_bytes=8,
        max_files=10,
        max_depth=4,
        max_file_bytes=6,
    )
    (tmp_path / "first").write_bytes(b"12345")
    (tmp_path / "second").write_bytes(b"1234")
    with pytest.raises(RuntimeError, match="总容量"):
        runner._scan_workspace(tmp_path, byte_limits)

    (tmp_path / "second").unlink()
    (tmp_path / "large").write_bytes(b"1234567")
    with pytest.raises(RuntimeError, match="单文件"):
        runner._scan_workspace(tmp_path, byte_limits)

    (tmp_path / "large").unlink()
    nested = tmp_path / "one" / "two"
    nested.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="层级"):
        runner._scan_workspace(
            tmp_path,
            WorkspaceLimits(1024, 10, 1, 1024),
        )

    for path in sorted((tmp_path / "one").rglob("*"), reverse=True):
        path.rmdir()
    (tmp_path / "one").rmdir()
    (tmp_path / "third").write_bytes(b"1")
    (tmp_path / "fourth").write_bytes(b"1")
    with pytest.raises(RuntimeError, match="条目数量"):
        runner._scan_workspace(
            tmp_path,
            WorkspaceLimits(1024, 2, 4, 1024),
        )

    (tmp_path / "third").unlink()
    (tmp_path / "fourth").unlink()
    (tmp_path / "link").symlink_to(tmp_path / "first")
    with pytest.raises(RuntimeError, match="符号链接"):
        runner._scan_workspace(
            tmp_path,
            WorkspaceLimits(1024, 10, 4, 1024),
        )


@pytest.mark.asyncio
async def test_workspace_scan_does_not_block_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = GeneratedToolRunner()
    limits = WorkspaceLimits(1024, 10, 3, 1024)

    def slow_scan(_path: Path, _limits: WorkspaceLimits) -> tuple[int, int]:
        time.sleep(0.15)
        return 0, 0

    monkeypatch.setattr(runner, "_scan_workspace", slow_scan)
    scan = asyncio.create_task(runner._scan_workspace_async(tmp_path, limits))
    started = asyncio.get_running_loop().time()
    await asyncio.sleep(0.01)
    elapsed = asyncio.get_running_loop().time() - started
    assert elapsed < 0.08
    assert not scan.done()
    await scan


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.geteuid() != 0,
    reason="workspace capability probe needs a nobody worker",
)
async def test_workspace_false_omits_context_and_uses_readonly_cwd() -> None:
    artifact = _custom_artifact(
        b"async def snapshot_echo(_workspace=None):\n"
        b"    try:\n"
        b"        open('unexpected', 'w').write('x')\n"
        b"        writable = True\n"
        b"    except OSError:\n"
        b"        writable = False\n"
        b"    return f'{_workspace}:{writable}'\n",
        workspace=False,
    )

    result = await GeneratedToolRunner().execute_artifact(
        artifact,
        {},
        {},
        expected_artifact_digest=artifact.artifact_digest,
        expected_bundle_digest=None,
        generation=artifact.generation,
    )

    assert result.text == "None:False"


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.geteuid() != 0,
    reason="final workspace scan needs a nobody worker",
)
async def test_fast_process_is_rejected_by_final_workspace_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "too_many.py"
    source.write_text(
        "from pathlib import Path\n\n"
        "async def too_many(_workspace):\n"
        "    root = Path(_workspace)\n"
        "    for index in range(3):\n"
        "        (root / str(index)).write_text('x')\n"
        "    return 'would-have-succeeded'\n",
        encoding="utf-8",
    )
    runner = GeneratedToolRunner()

    async def delayed_watcher(*_args, **_kwargs) -> None:
        await asyncio.sleep(60)

    original_get = config_parser.get_config

    def configured(key: str, default=None):
        if key == "generated_tool_workspace_max_files":
            return 2
        return original_get(key, default)

    runner_module = importlib.import_module(
        "nonebot_plugin_moellmchats.generated_tool_runner"
    )
    monkeypatch.setattr(runner, "_watch_workspace", delayed_watcher)
    monkeypatch.setattr(runner_module.config_parser, "get_config", configured)

    with pytest.raises(RuntimeError, match="条目数量"):
        await runner.execute_custom(
            source,
            "too_many",
            {},
            {},
            allow_network=True,
            allow_process=False,
        )

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from nonebot_plugin_moellmchats.generated_tool_runner import generated_tool_runner
from nonebot_plugin_moellmchats.runtime_reload import (
    _WATCH_FAILURE_BACKOFF_INITIAL_SECONDS,
    _WATCH_FAILURE_BACKOFF_MAX_SECONDS,
    ReloadResult,
    RuntimeReloader,
    runtime_reloader,
)
from nonebot_plugin_moellmchats.runtime_snapshot import mutable_value, runtime_snapshots
from nonebot_plugin_moellmchats.tool_contracts import ToolResult
from nonebot_plugin_moellmchats.tool_manager import tool_manager


@pytest.mark.asyncio
async def test_invalid_config_retains_previous_generation() -> None:
    await runtime_reloader.reload("test-baseline")
    previous = runtime_snapshots.current()
    path = runtime_reloader.watched_paths()[0]
    original = path.read_text(encoding="utf-8")
    try:
        path.write_text('{"broken":', encoding="utf-8")
        with pytest.raises(Exception, match=r"Expected|JSON|value|object"):
            await runtime_reloader.reload("test-invalid-json")
        assert runtime_snapshots.current() is previous
    finally:
        path.write_text(original, encoding="utf-8")
        await runtime_reloader.reload("test-restore-json")


@pytest.mark.asyncio
async def test_invalid_tool_source_retains_previous_generation() -> None:
    await runtime_reloader.reload("test-tool-baseline")
    previous = runtime_snapshots.current()
    path = tool_manager.custom_tools_dir / "broken_reload_test.py"
    try:
        path.write_text("async def broken(:\n", encoding="utf-8")
        with pytest.raises(Exception, match="自定义工具"):
            await runtime_reloader.reload("test-invalid-tool")
        assert runtime_snapshots.current() is previous
    finally:
        path.unlink(missing_ok=True)
        await runtime_reloader.reload("test-restore-tool")


@pytest.mark.asyncio
async def test_runtime_generation_pins_custom_tool_source(
    monkeypatch,
) -> None:
    path = tool_manager.custom_tools_dir / "artifact_generation_test.py"
    original_source = (
        b"async def artifact_generation_test():\n"
        b"    return 'generation-old'\n"
    )
    calls = []

    async def execute_artifact(
        artifact,
        arguments,
        context,
        *,
        expected_artifact_digest,
        expected_bundle_digest,
        generation,
    ):
        calls.append(
            (
                artifact,
                arguments,
                context,
                expected_artifact_digest,
                expected_bundle_digest,
                generation,
            )
        )
        return ToolResult(text="generation-old")

    monkeypatch.setattr(
        generated_tool_runner,
        "execute_artifact",
        execute_artifact,
    )
    try:
        path.write_bytes(original_source)
        result = await runtime_reloader.reload("test-artifact-generation")
        snapshot = runtime_snapshots.current()
        assert snapshot is not None
        entry = snapshot.tool_snapshot.custom_tools["artifact_generation_test"]
        artifact = entry["tool_artifact"]
        assert snapshot.generation == result.generation
        assert artifact.generation == entry["generation"] == result.generation
        assert artifact.artifact_digest == entry["artifact_digest"]
        assert artifact.source == original_source

        path.write_text(
            "async def artifact_generation_test():\n"
            "    raise RuntimeError('live source must not be read')\n",
            encoding="utf-8",
        )
        tool_result = await entry["func"]()
        assert tool_result.text == "generation-old"
        assert calls == [
            (
                artifact,
                {},
                {},
                artifact.artifact_digest,
                None,
                result.generation,
            )
        ]
    finally:
        path.unlink(missing_ok=True)
        await runtime_reloader.reload("test-artifact-generation-cleanup")


def test_tool_manager_forwards_generation_to_both_artifact_loaders(
    monkeypatch,
) -> None:
    manager_module = importlib.import_module(
        "nonebot_plugin_moellmchats.tool_manager"
    )
    calls: list[tuple[str, int]] = []

    def load_files(_files, *, generation: int):
        calls.append(("custom_file", generation))
        return {}, {}

    def load_generated(*, generation: int):
        calls.append(("generated", generation))
        return {}, {}

    monkeypatch.setattr(manager_module, "load_file_tools", load_files)
    monkeypatch.setattr(
        manager_module.generated_tool_store,
        "load_active_tools",
        load_generated,
    )
    tool_manager.load_custom_tools(commit=False, generation=23)

    assert calls == [("custom_file", 23), ("generated", 23)]


@pytest.mark.asyncio
async def test_unreachable_mcp_retains_previous_generation(monkeypatch) -> None:
    await runtime_reloader.reload("test-mcp-baseline")
    previous = runtime_snapshots.current()

    from nonebot_plugin_moellmchats import mcp_manager as module

    monkeypatch.setattr(
        module.mcp_manager,
        "load_config_candidate",
        lambda: {
            "broken": {
                "enabled": True,
                "transport": "streamable_http",
                "url": "http://127.0.0.1:1/mcp",
                "discover_timeout": 1,
            }
        },
    )

    async def fail(*args, **kwargs):
        raise ConnectionError("unreachable")

    monkeypatch.setattr(module.mcp_manager, "_list_tools_from_server", fail)
    with pytest.raises(Exception, match="unreachable"):
        await runtime_reloader.reload("test-unreachable-mcp")
    assert runtime_snapshots.current() is previous


def test_watched_paths_include_all_runtime_resources() -> None:
    names = {Path(path).name for path in runtime_reloader.watched_paths()}
    assert {
        "config.json",
        "providers.toml",
        "model_config.json",
        "temperaments.json",
        "temperament_config.json",
        "replies.toml",
        "custom_plugin_info.json",
        "mcp_servers.toml",
    } <= names


@pytest.mark.asyncio
async def test_commit_failure_restores_all_manager_state(monkeypatch) -> None:
    from nonebot_plugin_moellmchats.config import config_parser
    from nonebot_plugin_moellmchats.mcp_manager import mcp_manager
    from nonebot_plugin_moellmchats.model_selector import model_selector
    from nonebot_plugin_moellmchats.temperament_manager import temperament_manager

    await runtime_reloader.reload("test-transaction-baseline")
    previous_snapshot = runtime_snapshots.current()
    previous = {
        "config": mutable_value(config_parser.config),
        "model": model_selector.capture_state(),
        "temperaments": dict(temperament_manager.temperaments),
        "assignments": dict(temperament_manager.temperament_dict),
        "plugins": tool_manager.plugin_info,
        "tools": tool_manager.custom_tools,
        "dependencies": tool_manager.tool_dependencies,
        "mcp_names": tool_manager.mcp_tool_names,
        "servers": mcp_manager.servers,
        "mapping": mcp_manager.tool_to_server,
    }
    candidate = await runtime_reloader._build_candidate(previous_snapshot.generation + 1)

    def fail_publish(snapshot, *, expected_current):
        assert expected_current is previous_snapshot
        raise RuntimeError("publish failed")

    monkeypatch.setattr(runtime_snapshots, "publish", fail_publish)
    with pytest.raises(RuntimeError, match="publish failed"):
        runtime_reloader._commit(candidate)
    assert runtime_snapshots.current() is previous_snapshot
    assert mutable_value(config_parser.config) == previous["config"]
    assert model_selector.capture_state() == previous["model"]
    assert temperament_manager.temperaments == previous["temperaments"]
    assert temperament_manager.temperament_dict == previous["assignments"]
    assert tool_manager.plugin_info is previous["plugins"]
    assert tool_manager.custom_tools is previous["tools"]
    assert tool_manager.tool_dependencies is previous["dependencies"]
    assert tool_manager.mcp_tool_names is previous["mcp_names"]
    assert mcp_manager.servers is previous["servers"]
    assert mcp_manager.tool_to_server is previous["mapping"]


@pytest.mark.asyncio
async def test_plugin_tool_collision_retains_previous_generation() -> None:
    await runtime_reloader.reload("test-collision-baseline")
    previous = runtime_snapshots.current()
    path = tool_manager.custom_tools_dir / "collision_test.py"
    try:
        path.write_text(
            "async def nonebot_plugin_localstore():\n"
            "    'collision'\n"
            "    return 'bad'\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="插件冲突"):
            await runtime_reloader.reload("test-plugin-collision")
        assert runtime_snapshots.current() is previous
    finally:
        path.unlink(missing_ok=True)
        await runtime_reloader.reload("test-collision-restore")


@pytest.mark.asyncio
async def test_mcp_tool_collision_retains_previous_generation(monkeypatch) -> None:
    from nonebot_plugin_moellmchats.mcp_manager import mcp_manager

    await runtime_reloader.reload("test-mcp-collision-baseline")
    previous = runtime_snapshots.current()

    async def collide(*args, **kwargs):
        return {
            "nonebot_plugin_localstore": {
                "name": "nonebot_plugin_localstore",
                "description": "collision",
                "parameters": {"type": "object", "properties": {}},
                "func": lambda: None,
            }
        }, {}

    monkeypatch.setattr(mcp_manager, "discover_tools", collide)
    with pytest.raises(ValueError, match="MCP 工具名"):
        await runtime_reloader.reload("test-mcp-collision")
    assert runtime_snapshots.current() is previous


@pytest.mark.asyncio
async def test_failed_watcher_reload_keeps_last_published_fingerprint_for_retry(
    monkeypatch,
) -> None:
    calls = 0
    monkeypatch.setattr(runtime_reloader, "_fingerprint", (("published",),))
    monkeypatch.setattr(runtime_reloader, "fingerprint", lambda: (("changed",),))

    from nonebot_plugin_moellmchats import runtime_reload as reload_module

    original = reload_module.config_parser.get_config

    def configured(key, default=None):
        if key == "runtime_watch_interval_seconds":
            return 1
        if key == "runtime_watch_enabled":
            return True
        return original(key, default)

    async def no_sleep(_delay: float) -> None:
        return None

    async def fail_reload(_reason: str) -> ReloadResult:
        nonlocal calls
        calls += 1
        raise RuntimeError("watch failed")

    monkeypatch.setattr(reload_module.config_parser, "get_config", configured)
    monkeypatch.setattr(reload_module.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(runtime_reloader, "reload", fail_reload)

    with pytest.raises(RuntimeError, match="watch failed"):
        await runtime_reloader._watch_once()
    with pytest.raises(RuntimeError, match="watch failed"):
        await runtime_reloader._watch_once()
    assert calls == 2
    assert runtime_reloader._fingerprint == (("published",),)


@pytest.mark.asyncio
async def test_watcher_fingerprint_does_not_block_event_loop(monkeypatch) -> None:
    reloader = RuntimeReloader()
    baseline = (("baseline",),)
    reloader._fingerprint = baseline

    async def no_sleep(_delay: float) -> None:
        await asyncio.sleep(0)

    def slow_fingerprint() -> tuple:
        time.sleep(0.05)
        return baseline

    monkeypatch.setattr(reloader, "_watch_sleep", no_sleep)
    monkeypatch.setattr(reloader, "fingerprint", slow_fingerprint)

    task = asyncio.create_task(reloader._watch_once())
    await asyncio.sleep(0.01)
    assert not task.done()
    await task


@pytest.mark.asyncio
async def test_fingerprint_failure_happens_before_runtime_publish(monkeypatch) -> None:
    previous_snapshot = runtime_snapshots.current()
    candidate = SimpleNamespace(
        snapshot=SimpleNamespace(tool_snapshot=SimpleNamespace(custom_tools={})),
        mcp_servers={},
        mcp_mapping={},
    )
    fingerprints = iter([(("source",),), RuntimeError("fingerprint failed")])
    committed = False

    async def build_candidate(_generation: int, **_kwargs):
        return candidate

    def fingerprint(**_kwargs):
        value = next(fingerprints)
        if isinstance(value, Exception):
            raise value
        return value

    def commit(_candidate, **_kwargs) -> None:
        nonlocal committed
        committed = True

    monkeypatch.setattr(runtime_reloader, "_build_candidate", build_candidate)
    monkeypatch.setattr(runtime_reloader, "fingerprint", fingerprint)
    monkeypatch.setattr(runtime_reloader, "_commit", commit)

    with pytest.raises(RuntimeError, match="fingerprint failed"):
        await runtime_reloader.reload("fingerprint-failure")
    assert not committed
    assert runtime_snapshots.current() is previous_snapshot


@pytest.mark.asyncio
async def test_watcher_recovers_from_initial_fingerprint_failure(
    monkeypatch,
) -> None:
    reloader = RuntimeReloader()
    fingerprint_calls = 0
    entered_normal_loop = asyncio.Event()
    never = asyncio.Event()
    errors: list[str] = []

    def fingerprint() -> tuple:
        nonlocal fingerprint_calls
        fingerprint_calls += 1
        if fingerprint_calls == 1:
            raise OSError("initial fingerprint failed")
        return (("baseline",),)

    async def controlled_sleep(_delay: float) -> None:
        if fingerprint_calls == 1:
            await asyncio.sleep(0)
            return
        entered_normal_loop.set()
        await never.wait()

    from nonebot_plugin_moellmchats import runtime_reload as reload_module

    monkeypatch.setattr(reloader, "fingerprint", fingerprint)
    monkeypatch.setattr(reloader, "_watch_sleep", controlled_sleep)
    monkeypatch.setattr(
        reload_module.logger,
        "exception",
        lambda message: errors.append(message),
    )

    task = asyncio.create_task(reloader.watch())
    try:
        await asyncio.wait_for(entered_normal_loop.wait(), timeout=1)
        assert not task.done()
        assert fingerprint_calls == 2
        assert reloader._fingerprint == (("baseline",),)
        assert len(errors) == 1
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failing_key",
    ["runtime_watch_interval_seconds", "runtime_watch_enabled"],
)
async def test_watcher_recovers_from_config_read_failure(
    monkeypatch,
    failing_key: str,
) -> None:
    reloader = RuntimeReloader()
    baseline = (("baseline",),)
    fingerprint_calls = 0
    failed = False
    recovered = asyncio.Event()
    blocked = asyncio.Event()
    never = asyncio.Event()
    errors: list[str] = []

    def fingerprint() -> tuple:
        nonlocal fingerprint_calls
        fingerprint_calls += 1
        if fingerprint_calls > 1:
            recovered.set()
        return baseline

    def get_config(key: str, default=None):
        nonlocal failed
        if key == failing_key and not failed:
            failed = True
            raise OSError(f"{key} unavailable")
        if key == "runtime_watch_interval_seconds":
            return 1
        if key == "runtime_watch_enabled":
            return True
        return default

    async def controlled_sleep(_delay: float) -> None:
        if recovered.is_set():
            blocked.set()
            await never.wait()
        await asyncio.sleep(0)

    from nonebot_plugin_moellmchats import runtime_reload as reload_module

    monkeypatch.setattr(reloader, "fingerprint", fingerprint)
    monkeypatch.setattr(reloader, "_watch_sleep", controlled_sleep)
    monkeypatch.setattr(reload_module.config_parser, "get_config", get_config)
    monkeypatch.setattr(
        reload_module.logger,
        "exception",
        lambda message: errors.append(message),
    )

    task = asyncio.create_task(reloader.watch())
    try:
        await asyncio.wait_for(blocked.wait(), timeout=1)
        assert not task.done()
        assert failed
        assert fingerprint_calls >= 2
        assert reloader._fingerprint == baseline
        assert len(errors) == 1
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_watcher_recovers_from_loop_fingerprint_failure(monkeypatch) -> None:
    reloader = RuntimeReloader()
    baseline = (("baseline",),)
    fingerprint_calls = 0
    recovered = asyncio.Event()
    blocked = asyncio.Event()
    never = asyncio.Event()
    errors: list[str] = []

    def fingerprint() -> tuple:
        nonlocal fingerprint_calls
        fingerprint_calls += 1
        if fingerprint_calls == 2:
            raise OSError("loop fingerprint failed")
        if fingerprint_calls > 2:
            recovered.set()
        return baseline

    def get_config(key: str, default=None):
        if key == "runtime_watch_interval_seconds":
            return 1
        if key == "runtime_watch_enabled":
            return True
        return default

    async def controlled_sleep(_delay: float) -> None:
        if recovered.is_set():
            blocked.set()
            await never.wait()
        await asyncio.sleep(0)

    from nonebot_plugin_moellmchats import runtime_reload as reload_module

    monkeypatch.setattr(reloader, "fingerprint", fingerprint)
    monkeypatch.setattr(reloader, "_watch_sleep", controlled_sleep)
    monkeypatch.setattr(reload_module.config_parser, "get_config", get_config)
    monkeypatch.setattr(
        reload_module.logger,
        "exception",
        lambda message: errors.append(message),
    )

    task = asyncio.create_task(reloader.watch())
    try:
        await asyncio.wait_for(blocked.wait(), timeout=1)
        assert not task.done()
        assert fingerprint_calls >= 3
        assert reloader._fingerprint == baseline
        assert len(errors) == 1
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_watcher_recovers_from_reload_failure(monkeypatch) -> None:
    reloader = RuntimeReloader()
    baseline = (("baseline",),)
    changed = (("changed",),)
    fingerprint_calls = 0
    reload_calls = 0
    recovered = asyncio.Event()
    blocked = asyncio.Event()
    never = asyncio.Event()
    errors: list[str] = []

    def fingerprint() -> tuple:
        nonlocal fingerprint_calls
        fingerprint_calls += 1
        return baseline if fingerprint_calls == 1 else changed

    def get_config(key: str, default=None):
        if key == "runtime_watch_interval_seconds":
            return 1
        if key == "runtime_watch_enabled":
            return True
        return default

    async def reload(_reason: str) -> ReloadResult:
        nonlocal reload_calls
        reload_calls += 1
        if reload_calls == 1:
            raise RuntimeError("reload failed")
        reloader._fingerprint = changed
        recovered.set()
        return ReloadResult(
            generation=1,
            changed=("file-watch",),
            custom_tools=0,
            mcp_tools=0,
        )

    async def controlled_sleep(_delay: float) -> None:
        if recovered.is_set():
            blocked.set()
            await never.wait()
        await asyncio.sleep(0)

    from nonebot_plugin_moellmchats import runtime_reload as reload_module

    monkeypatch.setattr(reloader, "fingerprint", fingerprint)
    monkeypatch.setattr(reloader, "reload", reload)
    monkeypatch.setattr(reloader, "_watch_sleep", controlled_sleep)
    monkeypatch.setattr(reload_module.config_parser, "get_config", get_config)
    monkeypatch.setattr(
        reload_module.logger,
        "exception",
        lambda message: errors.append(message),
    )

    task = asyncio.create_task(reloader.watch())
    try:
        await asyncio.wait_for(blocked.wait(), timeout=1)
        assert not task.done()
        assert reload_calls == 2
        assert reloader._fingerprint == changed
        assert len(errors) == 1
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_watcher_backoff_is_bounded_and_resets_after_success(
    monkeypatch,
) -> None:
    reloader = RuntimeReloader()
    outcomes: list[BaseException | None] = [
        *(RuntimeError(f"failure-{index}") for index in range(8)),
        None,
        RuntimeError("failure-after-success"),
        asyncio.CancelledError(),
    ]
    sleeps: list[float] = []
    errors: list[str] = []

    async def watch_once() -> None:
        outcome = outcomes.pop(0)
        if outcome is not None:
            raise outcome

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    from nonebot_plugin_moellmchats import runtime_reload as reload_module

    monkeypatch.setattr(reloader, "fingerprint", lambda: (("baseline",),))
    monkeypatch.setattr(reloader, "_watch_once", watch_once)
    monkeypatch.setattr(reloader, "_watch_sleep", record_sleep)
    monkeypatch.setattr(
        reload_module.logger,
        "exception",
        lambda message: errors.append(message),
    )

    with pytest.raises(asyncio.CancelledError):
        await reloader.watch()

    initial = _WATCH_FAILURE_BACKOFF_INITIAL_SECONDS
    maximum = _WATCH_FAILURE_BACKOFF_MAX_SECONDS
    assert sleeps == [
        min(initial * (2**exponent), maximum) for exponent in range(8)
    ] + [initial]
    assert len(errors) == 9
    assert outcomes == []


@pytest.mark.asyncio
async def test_watcher_cancellation_exits_without_logging_failure(
    monkeypatch,
) -> None:
    reloader = RuntimeReloader()
    errors: list[str] = []

    async def cancelled() -> None:
        raise asyncio.CancelledError

    from nonebot_plugin_moellmchats import runtime_reload as reload_module

    monkeypatch.setattr(reloader, "fingerprint", lambda: (("baseline",),))
    monkeypatch.setattr(reloader, "_watch_once", cancelled)
    monkeypatch.setattr(
        reload_module.logger,
        "exception",
        lambda message: errors.append(message),
    )

    with pytest.raises(asyncio.CancelledError):
        await reloader.watch()
    assert errors == []


@pytest.mark.asyncio
@pytest.mark.parametrize("fatal", [SystemExit("stop"), KeyboardInterrupt()])
async def test_watcher_does_not_swallow_process_control_exceptions(
    monkeypatch,
    fatal: BaseException,
) -> None:
    reloader = RuntimeReloader()
    errors: list[str] = []

    def fail() -> tuple:
        raise fatal

    from nonebot_plugin_moellmchats import runtime_reload as reload_module

    monkeypatch.setattr(reloader, "fingerprint", fail)
    monkeypatch.setattr(
        reload_module.logger,
        "exception",
        lambda message: errors.append(message),
    )

    with pytest.raises(type(fatal)):
        await reloader.watch()
    assert errors == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, RuntimeError("watch task failed")])
async def test_unexpected_watcher_task_completion_is_logged(
    monkeypatch,
    failure: RuntimeError | None,
) -> None:
    reloader = RuntimeReloader()
    errors: list[str] = []

    async def finish_unexpectedly() -> None:
        if failure is not None:
            raise failure

    from nonebot_plugin_moellmchats import runtime_reload as reload_module

    monkeypatch.setattr(reloader, "watch", finish_unexpectedly)
    monkeypatch.setattr(
        reload_module.logger,
        "error",
        lambda message: errors.append(message),
    )

    reloader.start_watcher()
    task = reloader._watch_task
    assert task is not None
    if failure is None:
        await task
    else:
        with pytest.raises(RuntimeError, match="watch task failed"):
            await task
    await asyncio.sleep(0)

    assert len(errors) == 1
    assert "watcher 任务" in errors[0]
    assert ("意外结束" if failure is None else "异常结束") in errors[0]


@pytest.mark.asyncio
async def test_broken_resource_retains_snapshot_and_watcher_keeps_retrying(
    monkeypatch,
) -> None:
    await runtime_reloader.reload("test-watcher-broken-resource-baseline")
    previous_snapshot = runtime_snapshots.current()
    path = runtime_reloader.watched_paths()[0]
    original = path.read_text(encoding="utf-8")
    reloader = RuntimeReloader()
    initialized = asyncio.Event()
    allow_change = asyncio.Event()
    retried = asyncio.Event()
    never = asyncio.Event()
    reload_attempts = 0
    original_reload = reloader.reload

    async def counted_reload(reason: str) -> ReloadResult:
        nonlocal reload_attempts
        reload_attempts += 1
        return await original_reload(reason)

    async def controlled_sleep(_delay: float) -> None:
        if not initialized.is_set():
            initialized.set()
            await allow_change.wait()
            return
        if reload_attempts >= 2:
            retried.set()
            await never.wait()
        await asyncio.sleep(0)

    monkeypatch.setattr(reloader, "reload", counted_reload)
    monkeypatch.setattr(reloader, "_watch_sleep", controlled_sleep)
    task = asyncio.create_task(reloader.watch())
    try:
        await asyncio.wait_for(initialized.wait(), timeout=1)
        published_fingerprint = reloader._fingerprint
        path.write_text('{"broken":', encoding="utf-8")
        allow_change.set()

        await asyncio.wait_for(retried.wait(), timeout=3)
        assert not task.done()
        assert reload_attempts == 2
        assert runtime_snapshots.current() is previous_snapshot
        assert reloader._fingerprint == published_fingerprint
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        path.write_text(original, encoding="utf-8")
        await runtime_reloader.reload("test-watcher-broken-resource-restore")

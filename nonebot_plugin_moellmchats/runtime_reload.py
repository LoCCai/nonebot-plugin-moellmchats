from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
import time

from nonebot.log import logger

from .config import config_parser, config_path
from .mcp_manager import mcp_manager
from .model_selector import model_selector
from .runtime_metrics import runtime_metrics
from .runtime_snapshot import RuntimeSnapshot, immutable_mapping, runtime_snapshots
from .temperament_manager import temperament_manager
from .tool_manager import ToolSnapshot, tool_manager
from .utils import (
    invalidate_resource_caches,
    load_emotions_candidate,
    load_replies_candidate,
)


@dataclass(frozen=True)
class ReloadResult:
    generation: int
    changed: tuple[str, ...]
    custom_tools: int
    mcp_tools: int


@dataclass(frozen=True)
class _RuntimeCandidate:
    snapshot: RuntimeSnapshot
    mcp_servers: dict
    mcp_mapping: dict


class RuntimeReloader:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._fingerprint: tuple = ()
        self._watch_task: asyncio.Task | None = None

    def watched_paths(self) -> list[Path]:
        paths = [
            config_parser.filepath,
            model_selector.providers_file,
            model_selector.models_file,
            model_selector.cache_file,
            model_selector.model_config_file,
            temperament_manager.temperaments_path,
            temperament_manager.temperament_config,
            config_path / "replies.toml",
            tool_manager.custom_info_file,
            mcp_manager.config_file,
        ]
        paths.extend(sorted(tool_manager.custom_tools_dir.glob("*.py")))
        emotions_dir = Path(str(config_parser.get_config("emotions_dir", "")))
        if emotions_dir.is_dir():
            paths.extend(sorted(emotions_dir.iterdir()))
        return paths

    def fingerprint(self) -> tuple:
        values = []
        for path in self.watched_paths():
            try:
                stat = path.stat()
                values.append((str(path), stat.st_mtime_ns, stat.st_size))
            except FileNotFoundError:
                values.append((str(path), None, None))
        return tuple(values)

    async def _build_candidate(self, generation: int) -> _RuntimeCandidate:
        (
            config_candidate,
            model_candidate,
            temperament_candidate,
            replies_candidate,
            plugin_info,
            custom_tool_pair,
            mcp_servers,
        ) = await asyncio.gather(
            asyncio.to_thread(config_parser.load_candidate),
            asyncio.to_thread(model_selector.build_candidate),
            asyncio.to_thread(temperament_manager.load_candidate),
            asyncio.to_thread(load_replies_candidate),
            asyncio.to_thread(tool_manager.build_plugin_info),
            asyncio.to_thread(tool_manager.load_custom_tools, commit=False),
            asyncio.to_thread(mcp_manager.load_config_candidate),
        )
        emotions = await asyncio.to_thread(
            load_emotions_candidate, config_candidate
        )
        custom_tools, dependencies = custom_tool_pair
        mcp_tools, mcp_mapping = await mcp_manager.discover_tools(
            commit=False,
            servers=mcp_servers,
            strict=True,
        )

        mcp_names: set[str] = set()
        for name, schema in mcp_tools.items():
            custom_tools[name] = schema
            mcp_names.add(name)
        tool_snapshot = ToolSnapshot(
            generation=generation,
            plugin_info=plugin_info,
            custom_tools=custom_tools,
            tool_dependencies=dependencies,
            mcp_tool_names=mcp_names,
        )
        temperaments, assignments = temperament_candidate
        snapshot = RuntimeSnapshot(
            generation=generation,
            config=immutable_mapping(config_candidate),
            model_state=model_candidate,
            temperaments=immutable_mapping(temperaments),
            temperament_assignments=immutable_mapping(assignments),
            replies=immutable_mapping(replies_candidate),
            tool_snapshot=tool_snapshot,
            emotions=emotions,
            reloaded_at=time.time(),
        )
        return _RuntimeCandidate(
            snapshot=snapshot,
            mcp_servers=mcp_servers,
            mcp_mapping=mcp_mapping,
        )

    @staticmethod
    def _commit(candidate: _RuntimeCandidate) -> None:
        snapshot = candidate.snapshot
        config_parser.commit_candidate(snapshot.config)
        model_selector.commit_candidate(snapshot.model_state)
        temperament_manager.commit_candidate(
            (dict(snapshot.temperaments), dict(snapshot.temperament_assignments))
        )
        tools = snapshot.tool_snapshot
        tool_manager.plugin_info = tools.plugin_info
        tool_manager.custom_tools = tools.custom_tools
        tool_manager.tool_dependencies = tools.tool_dependencies
        tool_manager.mcp_tool_names = tools.mcp_tool_names
        mcp_manager.servers = candidate.mcp_servers
        mcp_manager.tool_to_server = {
            name: mapping
            for name, mapping in candidate.mcp_mapping.items()
            if name in tools.mcp_tool_names
        }
        invalidate_resource_caches()
        # Publish last: readers see either the complete old or complete new generation.
        runtime_snapshots.publish(snapshot)

    async def reload(self, reason: str = "manual") -> ReloadResult:
        async with self._lock:
            generation = runtime_metrics.reload_generation + 1
            try:
                candidate = await self._build_candidate(generation)
                self._commit(candidate)
            except Exception as error:
                runtime_metrics.reload_failures += 1
                runtime_metrics.last_reload_at = time.time()
                runtime_metrics.last_reload_error = str(error)[:500]
                raise

            runtime_metrics.reload_generation = generation
            runtime_metrics.reload_successes += 1
            runtime_metrics.last_reload_at = time.time()
            runtime_metrics.last_reload_error = None
            self._fingerprint = self.fingerprint()
            tools = candidate.snapshot.tool_snapshot
            logger.info(
                f"LLM 运行资源已原子重载 generation={generation} reason={reason}"
            )
            return ReloadResult(
                generation=generation,
                changed=(reason,),
                custom_tools=len(tools.custom_tools) - len(tools.mcp_tool_names),
                mcp_tools=len(tools.mcp_tool_names),
            )

    async def watch(self) -> None:
        self._fingerprint = self.fingerprint()
        while True:
            interval = config_parser.get_config("runtime_watch_interval_seconds", 2)
            await asyncio.sleep(interval)
            if not config_parser.get_config("runtime_watch_enabled", True):
                continue
            current = self.fingerprint()
            if current == self._fingerprint:
                continue
            await asyncio.sleep(0.5)
            try:
                await self.reload("file-watch")
            except Exception:
                logger.exception("LLM 运行资源自动重载失败，继续使用旧快照")
                # A broken file should not cause a tight retry loop. Its next change
                # will produce another fingerprint and another validation attempt.
                self._fingerprint = self.fingerprint()

    def start_watcher(self) -> None:
        if self._watch_task is None or self._watch_task.done():
            self._watch_task = asyncio.create_task(self.watch())

    async def stop_watcher(self) -> None:
        if self._watch_task is not None:
            self._watch_task.cancel()
            try:
                await self._watch_task
            except asyncio.CancelledError:
                pass
            self._watch_task = None


runtime_reloader = RuntimeReloader()

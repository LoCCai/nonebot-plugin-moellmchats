from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
import time
from typing import TYPE_CHECKING, Any, cast

from nonebot.log import logger

from .builtin_tools import builtin_tool_specs
from .config import config_parser, config_path
from .generated_tools import PreparedLifecycleChange, generated_tool_store
from .mcp_manager import mcp_manager
from .model_selector import model_selector
from .nonebot_plugin_tools import build_nonebot_plugin_candidate
from .runtime_metrics import runtime_metrics
from .runtime_snapshot import (
    RuntimeSnapshot,
    immutable_mapping,
    mutable_value,
    runtime_snapshots,
)
from .temperament_manager import temperament_manager
from .tool_contracts import tool_registry
from .tool_manager import ToolSnapshot, tool_manager
from .tool_providers import (
    BuiltinToolResources,
    FileToolResources,
    GeneratedToolResources,
    MCPToolResources,
    NoneBotPluginToolResources,
    ProviderDiscoveryContext,
    ProviderDiscoveryPlan,
    RegisteredToolResources,
    builtin_tool_provider,
    file_tool_provider,
    generated_tool_provider,
    mcp_tool_provider,
    nonebot_plugin_provider,
    provider_registry,
    registered_tool_provider,
)
from .utils import (
    invalidate_resource_caches,
    load_emotions_candidate,
    load_replies_candidate,
)

if TYPE_CHECKING:
    from .generated_tool_lifecycle import LifecycleState


@dataclass(frozen=True)
class ReloadResult:
    generation: int
    changed: tuple[str, ...]
    custom_tools: int
    mcp_tools: int
    generated_state_revision: int = 0
    generated_state_digest: str = ""
    operation_id: str | None = None
    converged: bool = True


@dataclass(frozen=True)
class _RuntimeCandidate:
    snapshot: RuntimeSnapshot
    mcp_servers: dict
    mcp_mapping: dict


_WATCH_DEBOUNCE_SECONDS = 0.5
_WATCH_FAILURE_BACKOFF_INITIAL_SECONDS = 0.5
_WATCH_FAILURE_BACKOFF_MAX_SECONDS = 30.0
_EXPECTED_RUNTIME_UNSET = object()


class RuntimeReloader:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._fingerprint: tuple = ()
        self._watch_task: asyncio.Task | None = None

    def watched_paths(
        self,
        *,
        generated_state: LifecycleState | None = None,
    ) -> list[Path]:
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
        paths.extend(
            generated_tool_store.watched_paths(generated_state=generated_state)
        )
        emotions_dir = Path(str(config_parser.get_config("emotions_dir", "")))
        if emotions_dir.is_dir():
            paths.extend(sorted(emotions_dir.iterdir()))
        return paths

    def fingerprint(
        self,
        *,
        generated_state: LifecycleState | None = None,
    ) -> tuple:
        if generated_state is None:
            generated_state = generated_tool_store.read_lifecycle_state()
        values = [
            (
                "generated-lifecycle",
                generated_state.revision,
                generated_state.state_digest,
            )
        ]
        for path in self.watched_paths(generated_state=generated_state):
            try:
                stat = path.stat()
                values.append((str(path), stat.st_mtime_ns, stat.st_size))
            except FileNotFoundError:
                values.append((str(path), None, None))
        return tuple(values)

    async def _build_candidate(
        self,
        generation: int,
        *,
        generated_state: LifecycleState | None = None,
        generated_source_overrides=None,
    ) -> _RuntimeCandidate:
        if generated_state is None:
            generated_state = await asyncio.to_thread(
                generated_tool_store.read_lifecycle_state
            )
        registered_tools = tool_registry.snapshot()
        builtin_specs = builtin_tool_specs()
        (
            config_candidate,
            model_candidate,
            temperament_candidate,
            replies_candidate,
            plugin_info,
            file_tool_candidate,
            generated_tool_candidate,
            mcp_servers,
        ) = await asyncio.gather(
            asyncio.to_thread(config_parser.load_candidate),
            asyncio.to_thread(model_selector.build_candidate),
            asyncio.to_thread(temperament_manager.load_candidate),
            asyncio.to_thread(load_replies_candidate),
            asyncio.to_thread(tool_manager.build_plugin_info),
            asyncio.to_thread(
                tool_manager.load_file_tools_candidate,
                generation=generation,
            ),
            asyncio.to_thread(
                tool_manager.load_generated_tools_candidate,
                generation=generation,
                generated_state=generated_state,
                generated_source_overrides=generated_source_overrides,
            ),
            asyncio.to_thread(mcp_manager.load_config_candidate),
        )
        legacy_plugin_names = await asyncio.to_thread(
            tool_manager.loaded_plugin_names
        )
        plugin_info, nonebot_plugin_specs = build_nonebot_plugin_candidate(
            plugin_info
        )
        mcp_tools, mcp_mapping = await mcp_manager.discover_tools(
            commit=False,
            servers=mcp_servers,
            strict=True,
        )
        if not isinstance(mcp_tools, dict) or not isinstance(mcp_mapping, dict):
            raise TypeError("MCP discovery candidate 必须是 tools/mapping 字典")
        for name, schema in mcp_tools.items():
            if not isinstance(name, str) or not isinstance(schema, dict):
                raise TypeError("MCP discovery tools 结构非法")
            schema["source"] = "mcp"
        mcp_names = set(mcp_tools)
        if set(mcp_mapping) != mcp_names:
            raise ValueError("MCP discovery tools 与 route sidecar 集合不一致")
        for name, route in mcp_mapping.items():
            if (
                not isinstance(name, str)
                or not isinstance(route, dict)
                or set(route) != {"server", "tool"}
                or not all(
                    isinstance(route[field], str) and route[field]
                    for field in ("server", "tool")
                )
            ):
                raise ValueError("MCP discovery route sidecar 结构非法")
        provider_catalog = await provider_registry.discover(
            generation,
            (
                ProviderDiscoveryPlan(
                    provider=registered_tool_provider,
                    context=ProviderDiscoveryContext(
                        generation=generation,
                        resources=RegisteredToolResources(
                            tuple(registered_tools.values())
                        ),
                    ),
                ),
                ProviderDiscoveryPlan(
                    provider=file_tool_provider,
                    context=ProviderDiscoveryContext(
                        generation=generation,
                        resources=FileToolResources.from_legacy_tools(
                            file_tool_candidate[0]
                        ),
                    ),
                ),
                ProviderDiscoveryPlan(
                    provider=generated_tool_provider,
                    context=ProviderDiscoveryContext(
                        generation=generation,
                        resources=GeneratedToolResources.from_legacy_tools(
                            lifecycle_state=generated_state,
                            source_overrides=generated_source_overrides,
                            legacy_tools=generated_tool_candidate[0],
                        ),
                    ),
                ),
                ProviderDiscoveryPlan(
                    provider=mcp_tool_provider,
                    context=ProviderDiscoveryContext(
                        generation=generation,
                        resources=MCPToolResources.from_legacy_tools(
                            mcp_tools
                        ),
                    ),
                ),
                ProviderDiscoveryPlan(
                    provider=builtin_tool_provider,
                    context=ProviderDiscoveryContext(
                        generation=generation,
                        resources=BuiltinToolResources(builtin_specs),
                    ),
                ),
                ProviderDiscoveryPlan(
                    provider=nonebot_plugin_provider,
                    context=ProviderDiscoveryContext(
                        generation=generation,
                        resources=NoneBotPluginToolResources(
                            nonebot_plugin_specs
                        ),
                    ),
                ),
            ),
        )
        registered_discovery = provider_catalog.tools_for_provider("registered")
        file_discovery = provider_catalog.tools_for_provider("custom-file")
        generated_discovery = provider_catalog.tools_for_provider("generated")
        mcp_discovery = provider_catalog.tools_for_provider("mcp")
        builtin_discovery = provider_catalog.tools_for_provider("builtin")
        nonebot_plugin_discovery = provider_catalog.tools_for_provider(
            "nonebot-plugin"
        )
        custom_tool_pair = await asyncio.to_thread(
            tool_manager.load_custom_tools,
            commit=False,
            generation=generation,
            generated_state=generated_state,
            generated_source_overrides=generated_source_overrides,
            registered_tools=registered_tools,
            registered_discovery=registered_discovery,
            file_tool_candidate=file_tool_candidate,
            file_discovery=file_discovery,
            generated_tool_candidate=generated_tool_candidate,
            generated_discovery=generated_discovery,
        )
        emotions = await asyncio.to_thread(
            load_emotions_candidate, config_candidate
        )
        custom_tools, dependencies = custom_tool_pair
        builtin_tool_provider.validate_legacy_parity(
            builtin_discovery,
            builtin_specs,
            dependencies,
            generation=generation,
            allow_additional_dependencies=True,
        )
        nonebot_plugin_provider.validate_legacy_parity(
            nonebot_plugin_discovery,
            plugin_info,
            dependencies,
            generation=generation,
            allow_additional_dependencies=True,
        )
        mcp_tool_provider.validate_legacy_parity(
            mcp_discovery,
            mcp_tools,
            dependencies,
            mcp_names,
            generation=generation,
            allow_additional_dependencies=True,
        )

        for name, schema in mcp_tools.items():
            if name in custom_tools or name in plugin_info:
                raise ValueError(f"MCP 工具名与现有工具或插件冲突: {name}")
            custom_tools[name] = schema
        builtin_plugin_collisions = {
            spec.name for spec in builtin_specs
        } & set(plugin_info)
        if builtin_plugin_collisions:
            raise ValueError(
                "内置工具名与 NoneBot 插件冲突: "
                f"{sorted(builtin_plugin_collisions)}"
            )
        plugin_collisions = set(plugin_info) & set(custom_tools)
        if plugin_collisions:
            raise ValueError(
                f"工具名与 NoneBot 插件冲突: {sorted(plugin_collisions)}"
            )
        tool_manager.validate_tool_schemas(custom_tools)
        tool_manager.validate_dependencies(
            dependencies,
            set(plugin_info) | set(custom_tools),
        )
        tool_snapshot = ToolSnapshot(
            generation=generation,
            plugin_info=immutable_mapping(plugin_info),
            custom_tools=immutable_mapping(custom_tools),
            tool_dependencies=immutable_mapping(dependencies),
            mcp_tool_names=frozenset(mcp_names),
            provider_catalog=provider_catalog,
            legacy_plugin_names=(
                legacy_plugin_names
                | frozenset(cast("dict[str, object]", plugin_info))
            ),
            mcp_server_identifiers=(
                mcp_manager.configured_server_identifiers(
                    cast("dict[str, dict[str, Any]]", mcp_servers)
                )
            ),
            generated_state_revision=generated_state.revision,
            generated_state_digest=generated_state.state_digest,
            generated_active=generated_state.active,
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
            generated_state_revision=generated_state.revision,
            generated_state_digest=generated_state.state_digest,
            generated_active=generated_state.active,
        )
        return _RuntimeCandidate(
            snapshot=snapshot,
            mcp_servers=mcp_servers,
            mcp_mapping=mcp_mapping,
        )

    @staticmethod
    def _commit(
        candidate: _RuntimeCandidate,
        *,
        expected_current: RuntimeSnapshot | object | None = _EXPECTED_RUNTIME_UNSET,
    ) -> None:
        snapshot = candidate.snapshot
        if expected_current is _EXPECTED_RUNTIME_UNSET:
            expected_current = runtime_snapshots.current()
        previous = {
            "config": mutable_value(config_parser.config),
            "model": model_selector.capture_state(),
            "temperaments": dict(temperament_manager.temperaments),
            "assignments": dict(temperament_manager.temperament_dict),
            "plugin_info": tool_manager.plugin_info,
            "custom_tools": tool_manager.custom_tools,
            "dependencies": tool_manager.tool_dependencies,
            "mcp_names": tool_manager.mcp_tool_names,
            "mcp_servers": mcp_manager.servers,
            "mcp_mapping": mcp_manager.tool_to_server,
        }
        tools = snapshot.tool_snapshot
        try:
            config_parser.commit_candidate(mutable_value(snapshot.config))
            model_selector.commit_candidate(snapshot.model_state)
            temperament_manager.commit_candidate(
                (
                    mutable_value(snapshot.temperaments),
                    mutable_value(snapshot.temperament_assignments),
                )
            )
            tool_manager.plugin_info = mutable_value(tools.plugin_info)
            tool_manager.custom_tools = mutable_value(tools.custom_tools)
            tool_manager.tool_dependencies = mutable_value(tools.tool_dependencies)
            tool_manager.mcp_tool_names = set(tools.mcp_tool_names)
            mcp_manager.servers = mutable_value(candidate.mcp_servers)
            mcp_manager.tool_to_server = {
                name: mutable_value(mapping)
                for name, mapping in candidate.mcp_mapping.items()
                if name in tools.mcp_tool_names
            }
            invalidate_resource_caches()
            # Publish last: readers see either the complete old or complete new generation.
            runtime_snapshots.publish(
                snapshot,
                expected_current=expected_current,
            )
        except Exception:
            config_parser.commit_candidate(previous["config"])
            model_selector.commit_candidate(previous["model"])
            temperament_manager.commit_candidate(
                (previous["temperaments"], previous["assignments"])
            )
            tool_manager.plugin_info = previous["plugin_info"]
            tool_manager.custom_tools = previous["custom_tools"]
            tool_manager.tool_dependencies = previous["dependencies"]
            tool_manager.mcp_tool_names = previous["mcp_names"]
            mcp_manager.servers = previous["mcp_servers"]
            mcp_manager.tool_to_server = previous["mcp_mapping"]
            raise

    @staticmethod
    def _same_generated_state(
        left: LifecycleState,
        right: LifecycleState,
    ) -> bool:
        return (
            left.revision == right.revision
            and left.state_digest == right.state_digest
        )

    @staticmethod
    def _next_generation(current: RuntimeSnapshot | None) -> int:
        if current is not None:
            return current.generation + 1
        return runtime_metrics.reload_generation + 1

    @staticmethod
    def _record_reload_failure(error: BaseException) -> None:
        runtime_metrics.reload_failures += 1
        runtime_metrics.last_reload_at = time.time()
        runtime_metrics.last_reload_error = str(error)[:500]

    @staticmethod
    def _record_reload_success(snapshot: RuntimeSnapshot) -> None:
        runtime_metrics.reload_generation = snapshot.generation
        runtime_metrics.reload_successes += 1
        runtime_metrics.last_reload_at = time.time()
        runtime_metrics.last_reload_error = None

    @staticmethod
    def _result(
        candidate: _RuntimeCandidate,
        *,
        reason: str,
        operation_id: str | None = None,
        converged: bool = True,
    ) -> ReloadResult:
        snapshot = candidate.snapshot
        tools = snapshot.tool_snapshot
        return ReloadResult(
            generation=snapshot.generation,
            changed=(reason,),
            custom_tools=len(tools.custom_tools) - len(tools.mcp_tool_names),
            mcp_tools=len(tools.mcp_tool_names),
            generated_state_revision=snapshot.generated_state_revision,
            generated_state_digest=snapshot.generated_state_digest,
            operation_id=operation_id,
            converged=converged,
        )

    async def _reload_serialized(self, reason: str) -> ReloadResult:
        async with self._lock:
            previous_snapshot = runtime_snapshots.current()
            generation = self._next_generation(previous_snapshot)
            try:
                generated_state = await asyncio.to_thread(
                    generated_tool_store.read_lifecycle_state
                )
                source_fingerprint = await asyncio.to_thread(
                    self.fingerprint,
                    generated_state=generated_state,
                )
                candidate = await self._build_candidate(
                    generation,
                    generated_state=generated_state,
                )
                # Fingerprinting can read Generated Tool lifecycle files. Do it before
                # publishing so a late filesystem error can never turn a successful
                # publish into an apparent failure that callers then try to roll back.
                observed_state = await asyncio.to_thread(
                    generated_tool_store.read_lifecycle_state
                )
                published_fingerprint = await asyncio.to_thread(
                    self.fingerprint,
                    generated_state=observed_state,
                )
                if (
                    not self._same_generated_state(
                        generated_state,
                        observed_state,
                    )
                    or published_fingerprint != source_fingerprint
                ):
                    raise RuntimeError("LLM 运行资源在重载期间再次变化，请重试")
                self._commit(
                    candidate,
                    expected_current=previous_snapshot,
                )
            except Exception as error:
                self._record_reload_failure(error)
                raise

            self._record_reload_success(candidate.snapshot)
            self._fingerprint = published_fingerprint
            logger.info(
                "LLM 运行资源已发布 "
                f"generation={generation} reason={reason} "
                f"lifecycle={generated_state.revision}:"
                f"{generated_state.state_digest[:12]}"
            )
            return self._result(candidate, reason=reason)

    async def reload(self, reason: str = "manual") -> ReloadResult:
        """Build and publish one snapshot from one canonical lifecycle state."""

        return await self._reload_serialized(reason)

    def request_filesystem_retry(self) -> None:
        """Force the watcher to re-check canonical state and runtime convergence."""

        self._fingerprint = ()

    async def _finish_generated_change(
        self,
        *,
        reason: str,
        change: PreparedLifecycleChange,
        candidate: _RuntimeCandidate,
        previous_snapshot: RuntimeSnapshot | None,
    ) -> ReloadResult:
        """Run the durable CAS and local pointer publish as one shielded phase."""

        committed = await asyncio.to_thread(
            generated_tool_store._commit_prepared_internal,
            change,
        )
        expected = change.plan.after_state
        if not self._same_generated_state(committed, expected):
            raise RuntimeError(
                "Generated Tool 提交返回的 lifecycle state 与预构建候选不一致"
            )

        # There is deliberately no await between the durable state returning and
        # the local runtime pointer publish.  Other processes converge through the
        # watcher; no cross-process in-memory ACID guarantee is claimed.
        self._commit(
            candidate,
            expected_current=previous_snapshot,
        )
        self._record_reload_success(candidate.snapshot)

        converged = False
        try:
            observed = await asyncio.to_thread(
                generated_tool_store.read_lifecycle_state
            )
            converged = self._same_generated_state(observed, committed)
            if converged:
                try:
                    self._fingerprint = await asyncio.to_thread(
                        self.fingerprint,
                        generated_state=observed,
                    )
                except Exception as error:
                    # Canonical and runtime are already committed.  A trailing
                    # filesystem scan cannot retroactively turn the operation
                    # into a failure; force the watcher to retry instead.
                    self.request_filesystem_retry()
                    logger.warning(
                        "Generated Tool 已提交并发布，但文件指纹刷新失败，"
                        f"watcher 将重试: {error!r}"
                    )
            else:
                # Another worker advanced desired state after our CAS.  Never
                # mark that newer state as already applied by this older runtime.
                self.request_filesystem_retry()
        except Exception as error:
            # The durable state and local pointer are already published.  Keep
            # success semantics and let the watcher re-establish observation.
            self.request_filesystem_retry()
            logger.warning(
                "Generated Tool 已提交并发布，但收敛状态读取失败，"
                f"watcher 将重试: {error!r}"
            )
        logger.info(
            "Generated Tool lifecycle 已提交并发布 "
            f"operation={change.plan.operation_id[:12]} "
            f"revision={committed.revision} "
            f"generation={candidate.snapshot.generation} "
            f"converged={converged}"
        )
        return self._result(
            candidate,
            reason=reason,
            operation_id=change.plan.operation_id,
            converged=converged,
        )

    @staticmethod
    async def _settle_shielded(task: asyncio.Task):
        """Wait for finalization despite repeated cancellation, then re-raise it."""

        cancellation: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as error:
                if cancellation is None:
                    cancellation = error
        if cancellation is not None:
            try:
                task.result()
            except BaseException as error:
                logger.exception(
                    "Generated Tool 事务在调用者取消后收尾失败: "
                    f"{error!r}"
                )
            raise cancellation
        return task.result()

    async def apply_generated_change(
        self,
        reason: str,
        change: PreparedLifecycleChange,
    ) -> tuple[object, ReloadResult]:
        """Prebuild from ``plan.after_state``, CAS it, then publish that candidate.

        Filesystem durability, this process' runtime pointer, and other workers'
        eventual convergence are intentionally separate phases.  Once durable
        finalization starts it is settled even under repeated task cancellation;
        there is no blind before-image rollback and no background ``to_thread``
        write left running after this coroutine returns.
        """

        if not isinstance(change, PreparedLifecycleChange):
            raise TypeError("change 必须是 PreparedLifecycleChange")
        async with self._lock:
            previous_snapshot = runtime_snapshots.current()
            generation = self._next_generation(previous_snapshot)
            try:
                before = await asyncio.to_thread(
                    generated_tool_store.read_lifecycle_state
                )
                if (
                    before.revision != change.plan.expected_revision
                    or before.state_digest != change.plan.before_digest
                ):
                    raise RuntimeError(
                        "Generated Tool lifecycle plan 已过期，请重新执行管理指令"
                    )
                source_fingerprint = await asyncio.to_thread(
                    self.fingerprint,
                    generated_state=before,
                )
                candidate = await self._build_candidate(
                    generation,
                    generated_state=change.plan.after_state,
                    generated_source_overrides=(
                        change.generated_source_overrides
                    ),
                )
                observed_before = await asyncio.to_thread(
                    generated_tool_store.read_lifecycle_state
                )
                if (
                    not self._same_generated_state(before, observed_before)
                    or await asyncio.to_thread(
                        self.fingerprint,
                        generated_state=observed_before,
                    )
                    != source_fingerprint
                ):
                    raise RuntimeError(
                        "LLM 运行资源在 Generated Tool 候选构建期间变化，请重试"
                    )

                finalization = asyncio.create_task(
                    self._finish_generated_change(
                        reason=reason,
                        change=change,
                        candidate=candidate,
                        previous_snapshot=previous_snapshot,
                    ),
                    name=(
                        "moellm-generated-finalize-"
                        f"{change.plan.operation_id[:12]}"
                    ),
                )
                result = await self._settle_shielded(finalization)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.request_filesystem_retry()
                self._record_reload_failure(error)
                raise
            return change.result, result

    async def _watch_once(self) -> None:
        interval = config_parser.get_config("runtime_watch_interval_seconds", 2)
        await self._watch_sleep(interval)
        if not config_parser.get_config("runtime_watch_enabled", True):
            return
        current = await asyncio.to_thread(self.fingerprint)
        if current == self._fingerprint:
            return
        await self._watch_sleep(_WATCH_DEBOUNCE_SECONDS)
        await self.reload("file-watch")

    @staticmethod
    async def _watch_sleep(delay: float) -> None:
        await asyncio.sleep(delay)

    @staticmethod
    def _watch_failure_backoff(consecutive_failures: int) -> float:
        exponent = min(max(consecutive_failures - 1, 0), 16)
        return min(
            _WATCH_FAILURE_BACKOFF_INITIAL_SECONDS * (2**exponent),
            _WATCH_FAILURE_BACKOFF_MAX_SECONDS,
        )

    async def watch(self) -> None:
        fingerprint_initialized = False
        consecutive_failures = 0
        retry_delay = 0.0
        while True:
            try:
                if retry_delay:
                    await self._watch_sleep(retry_delay)
                    retry_delay = 0.0
                if not fingerprint_initialized:
                    self._fingerprint = await asyncio.to_thread(
                        self.fingerprint
                    )
                    fingerprint_initialized = True
                else:
                    await self._watch_once()
                consecutive_failures = 0
            except asyncio.CancelledError:
                raise
            except Exception:
                consecutive_failures += 1
                retry_delay = self._watch_failure_backoff(consecutive_failures)
                logger.exception(
                    f"LLM 运行资源 watcher 失败，{retry_delay:g} 秒后重试，继续使用旧快照"
                )

    @staticmethod
    def _watch_task_done(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            logger.error("LLM 运行资源 watcher 任务意外结束")
            return
        logger.error(f"LLM 运行资源 watcher 任务异常结束: {error!r}")

    def start_watcher(self) -> None:
        if self._watch_task is None or self._watch_task.done():
            self._watch_task = asyncio.create_task(
                self.watch(),
                name="moellmchats-runtime-watcher",
            )
            self._watch_task.add_done_callback(self._watch_task_done)

    async def stop_watcher(self) -> None:
        if self._watch_task is not None:
            self._watch_task.cancel()
            try:
                await self._watch_task
            except asyncio.CancelledError:
                pass
            self._watch_task = None


runtime_reloader = RuntimeReloader()

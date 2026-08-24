import asyncio
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
import math
import time
from typing import TYPE_CHECKING, Any

from nonebot.log import logger
import ujson as json

from .agent_runtime import AgentRunState, ToolCallStatus
from .compat import timeout as timeout_scope
from .config import config_parser
from .event_simulator import event_simulator
from .parallel_execution import (
    ReadOnlyParallelExecutionError,
    ReadOnlyParallelExecutionTimeout,
    ReadOnlyParallelToolExecutor,
)
from .pending_actions import PendingActionError, pending_action_store
from .runtime_metrics import runtime_metrics
from .tool_contracts import (
    ToolEffect,
    ToolResult,
    render_tool_result,
    validate_tool_arguments,
)
from .tool_execution import (
    ToolExecutionError,
    ToolExecutionTimeoutError,
    execute_custom_tool,
    validate_pending_custom_tool,
)
from .tool_graph import ToolGraph
from .tool_manager import LlmToolExecutionRoute, LlmToolExecutionView
from .tool_scheduler import ReadOnlyParallelToolScheduler, ToolSchedulingError
from .trusted_runner_pool import (
    TrustedRunnerEligibilityError,
    TrustedRunnerExecutionTimeout,
    TrustedRunnerPool,
    TrustedRunnerPoolError,
    TrustedRunnerPoolState,
)
from .utils import parse_emotion

if TYPE_CHECKING:
    from .agent_context_runtime import AgentRequestRuntime
    from .tool_manager import ToolSnapshot


@dataclass(frozen=True)
class _PreparedParallelToolCall:
    call: Mapping[str, Any]
    tool_name: str
    arguments: dict[str, Any]
    view: LlmToolExecutionView


@dataclass(frozen=True)
class _PreparedParallelToolBatch:
    calls: tuple[_PreparedParallelToolCall, ...]
    graph: ToolGraph
    runner: TrustedRunnerPool
    executor: ReadOnlyParallelToolExecutor


@dataclass(frozen=True)
class _ParallelToolOutcome:
    content: str
    images: tuple[str, ...] = ()


class _ParallelTracePersistenceError(RuntimeError):
    """A critical Agent tool trace could not be durably recorded."""


class LlmToolsMixin:
    if TYPE_CHECKING:
        bot: Any
        event: Any
        format_message_dict: dict[str, Any]
        tool_snapshot: ToolSnapshot
        messages_handler: Any
        agent_runtime: AgentRequestRuntime | None
        model_info: dict[str, Any]
        emotion_flag: bool
        is_superuser: bool
        _current_tool_usage: Counter[str]
        _pending_vision_images: list[str]

        async def send_emotion_message(self, content: str) -> str: ...

        def _sanitize_tool_calls_for_history(self, tool_calls: list) -> list: ...

        async def none_stream_llm_chat(self, *args: Any, **kwargs: Any) -> Any: ...

    @staticmethod
    def _validate_tool_arguments(
        arguments: object,
        parameters: Mapping[str, Any] | None,
    ) -> str | None:
        return validate_tool_arguments(arguments, parameters)

    def _tool_timeout_seconds(self) -> float:
        configured = config_parser.get_config("tool_timeout_seconds", 30)
        if (
            not isinstance(configured, (int, float))
            or isinstance(configured, bool)
            or not math.isfinite(configured)
            or configured <= 0
        ):
            configured = 30.0
        timeout = float(configured)
        runtime = getattr(self, "agent_runtime", None)
        if runtime is not None:
            timeout = min(timeout, runtime.deadline.remaining())
        if timeout <= 0:
            raise TimeoutError
        return timeout

    async def _record_agent_tool_outcome(
        self,
        *,
        call: Mapping[str, Any],
        tool_view: object | None,
        arguments: Mapping[str, Any],
        status: ToolCallStatus,
        created_at: float,
        started_monotonic: float,
        result_preview: str | None = None,
        confirmation_id: str | None = None,
        error_type: str | None = None,
    ) -> None:
        runtime = getattr(self, "agent_runtime", None)
        if runtime is None:
            return
        function = call.get("function")
        tool_name = (
            function.get("name", "unknown_tool")
            if isinstance(function, Mapping)
            else "unknown_tool"
        )
        source = getattr(tool_view, "source", None)
        await runtime.record_tool_outcome(
            tool_name=str(tool_name),
            source=source,
            bundle_id=getattr(tool_view, "bundle_id", None),
            bundle_digest=getattr(tool_view, "bundle_digest", None),
            arguments=arguments,
            status=status,
            created_at=created_at,
            started_monotonic=started_monotonic,
            result_preview=result_preview,
            confirmation_id=confirmation_id,
            error_type=error_type,
        )

    def _prepare_read_only_parallel_batch(
        self,
        tool_calls: list,
    ) -> _PreparedParallelToolBatch | None:
        runtime = getattr(self, "agent_runtime", None)
        if runtime is None or not 2 <= len(tool_calls):
            return None
        resources = runtime.coordinator.resources
        runner = resources.trusted_runner
        graph = resources.parallel_tool_graph
        if runner is None or graph is None:
            return None
        if runner.state is not TrustedRunnerPoolState.RUNNING:
            return None
        if (
            runtime.run.generation != resources.generation
            or runner.generation != resources.generation
            or getattr(self.tool_snapshot, "generation", None) != resources.generation
        ):
            return None

        runner_snapshot = runner.snapshot()
        if len(tool_calls) > runner_snapshot.worker_count:
            return None

        is_superuser = bool(getattr(self, "is_superuser", False))
        repeated_limit = config_parser.get_config("max_repeated_tool_calls", 2)
        if not isinstance(repeated_limit, int) or isinstance(repeated_limit, bool) or repeated_limit <= 0:
            return None

        prepared: list[_PreparedParallelToolCall] = []
        tool_names: list[str] = []
        call_ids: list[str] = []
        effects: dict[str, ToolEffect] = {}
        for call in tool_calls:
            if not isinstance(call, Mapping):
                return None
            call_id = call.get("id")
            function = call.get("function")
            if not isinstance(call_id, str) or not call_id or not isinstance(function, Mapping):
                return None
            tool_name = function.get("name")
            arguments_text = function.get("arguments")
            if not isinstance(tool_name, str) or not tool_name or not isinstance(arguments_text, str):
                return None
            if tool_name not in runner.eligible_tools or tool_name not in graph.tools:
                return None
            try:
                view = self.tool_snapshot.resolve_llm_tool_execution(
                    tool_name,
                    is_superuser=is_superuser,
                )
            except Exception:
                return None
            if (
                view is None
                or not view.provider_authoritative
                or view.generation != resources.generation
                or view.route is not LlmToolExecutionRoute.CUSTOM_TOOL
                or view.spec is None
                or view.legacy_entry is None
                or view.spec.effect is not ToolEffect.READ_ONLY
                or view.spec.policy is not None
            ):
                return None
            decision = view.trust_decision
            if (
                decision is None
                or not decision.allowed
                or decision.confirmation_required
                or graph.confirmation_required_for(tool_name)
                or graph.capabilities_required_for(tool_name)
            ):
                return None
            if set(view.spec.dependencies) != set(graph.dependencies_for(tool_name)):
                return None
            try:
                arguments = json.loads(arguments_text)
            except Exception:
                return None
            if not isinstance(arguments, dict):
                return None
            if self._validate_tool_arguments(arguments, view.spec.parameters):
                return None
            if self._current_tool_usage[tool_name] + 1 > repeated_limit:
                return None
            prepared.append(
                _PreparedParallelToolCall(
                    call=call,
                    tool_name=tool_name,
                    arguments=arguments,
                    view=view,
                )
            )
            tool_names.append(tool_name)
            call_ids.append(call_id)
            effects[tool_name] = view.spec.effect

        if len(set(tool_names)) != len(tool_names) or len(set(call_ids)) != len(call_ids):
            return None
        max_parallelism = runner_snapshot.worker_count
        try:
            schedule = ReadOnlyParallelToolScheduler(max_parallelism=max_parallelism).plan(
                graph=graph,
                selected_tools=tuple(tool_names),
                effects=effects,
            )
        except (ToolSchedulingError, ValueError):
            return None
        if not schedule.has_parallel_batches:
            return None
        return _PreparedParallelToolBatch(
            calls=tuple(prepared),
            graph=graph,
            runner=runner,
            executor=ReadOnlyParallelToolExecutor(max_parallelism=max_parallelism),
        )

    @staticmethod
    def _render_parallel_tool_outcome(
        prepared: _PreparedParallelToolCall,
        result: ToolResult,
    ) -> _ParallelToolOutcome:
        spec = prepared.view.spec
        assert spec is not None
        render_limit = (
            spec.result_limit if spec.result_limit is not None else config_parser.get_config("max_tool_result_chars", 6000)
        )
        rendered = render_tool_result(result, max_chars=render_limit)
        content = f"函数执行返回结果：\n{rendered}" if rendered else "函数执行成功，但未返回有效结果。"
        return _ParallelToolOutcome(content=content, images=tuple(result.images))

    def _append_tool_round_history(
        self,
        *,
        assistant_msg: Mapping[str, Any],
        tool_calls: list,
        send_message_list: list,
    ) -> None:
        history_tool_result_limit = 300
        history_tool_calls = self._sanitize_tool_calls_for_history(tool_calls)
        history_msgs = [
            {
                "role": "assistant",
                "content": assistant_msg["content"],
                "tool_calls": history_tool_calls,
            }
        ]
        for call in tool_calls:
            tool_result_content = next(
                (
                    message["content"]
                    for message in reversed(send_message_list)
                    if message.get("role") == "tool" and message.get("tool_call_id") == call["id"]
                ),
                "",
            )
            history_msgs.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": tool_result_content[:history_tool_result_limit],
                }
            )
        self.messages_handler.messages_entity.tool_messages.extend(history_msgs)

    async def _try_execute_read_only_parallel_tools(
        self,
        tool_calls: list,
        result_text: str,
        send_message_list: list,
        reasoning_content: str,
    ) -> list | None:
        batch = self._prepare_read_only_parallel_batch(tool_calls)
        if batch is None:
            return None
        runtime = self.agent_runtime
        assert runtime is not None

        content_for_history = str(result_text) if result_text else ""
        if self.emotion_flag and content_for_history:
            content_for_history, _ = parse_emotion(content_for_history)
        tool_names = tuple(prepared.tool_name for prepared in batch.calls)
        assistant_msg = {
            "role": "assistant",
            "content": content_for_history.strip() or f"（正在调用工具: {', '.join(tool_names)}）",
            "tool_calls": tool_calls,
        }
        if reasoning_content:
            assistant_msg["reasoning_content"] = reasoning_content
        send_message_list.append(assistant_msg)

        if result_text:
            await self.send_emotion_message(result_text)
        else:
            await self.bot.send(
                self.event,
                f"正在并行调用函数: {', '.join(tool_names)}...",
            )

        for prepared in batch.calls:
            decision = prepared.view.trust_decision
            if decision is not None and decision.audit_required:
                logger.info(f"工具 trust decision: {decision.audit_metadata()}")
            self._current_tool_usage[prepared.tool_name] += 1
            runtime_metrics.tool_steps += 1
            self.messages_handler.messages_entity.add_used_plugins({prepared.tool_name})

        outcomes: dict[str, _ParallelToolOutcome] = {}
        recorded: set[str] = set()
        trace_attempted: set[str] = set()
        trace_failures: set[str] = set()
        traces: dict[str, tuple[float, float]] = {}
        trace_lock = asyncio.Lock()

        async def record_once(
            prepared: _PreparedParallelToolCall,
            *,
            status: ToolCallStatus,
            result_preview: str | None = None,
            error_type: str | None = None,
        ) -> None:
            async with trace_lock:
                if prepared.tool_name in recorded:
                    return
                if prepared.tool_name in trace_attempted:
                    raise _ParallelTracePersistenceError("并行工具 trace 已尝试持久化，禁止未知结果重放") from None
                trace_attempted.add(prepared.tool_name)
                created_at, started_monotonic = traces.setdefault(
                    prepared.tool_name,
                    (time.time(), time.monotonic()),
                )
                try:
                    await self._record_agent_tool_outcome(
                        call=prepared.call,
                        tool_view=prepared.view,
                        arguments=prepared.arguments,
                        status=status,
                        created_at=created_at,
                        started_monotonic=started_monotonic,
                        result_preview=result_preview,
                        error_type=error_type,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    trace_failures.add(prepared.tool_name)
                    raise
                recorded.add(prepared.tool_name)

        def build_invocation(prepared: _PreparedParallelToolCall):
            async def handler(_dependencies: Mapping[str, Any]) -> _ParallelToolOutcome:
                legacy_entry = prepared.view.legacy_entry
                assert legacy_entry is not None
                try:
                    result = await execute_custom_tool(
                        prepared.tool_name,
                        legacy_entry,
                        prepared.arguments,
                        bot=self.bot,
                        event=self.event,
                    )
                    outcome = self._render_parallel_tool_outcome(
                        prepared,
                        result,
                    )
                except asyncio.CancelledError:
                    outcome = _ParallelToolOutcome("并行工具调用已安全取消")
                    await record_once(
                        prepared,
                        status=ToolCallStatus.CANCELLED,
                        error_type="CancelledError",
                    )
                    outcomes[prepared.tool_name] = outcome
                    raise
                except ToolExecutionTimeoutError:
                    outcome = _ParallelToolOutcome("函数执行超时，已安全终止")
                    await record_once(
                        prepared,
                        status=ToolCallStatus.TIMED_OUT,
                        error_type="TimeoutError",
                    )
                    outcomes[prepared.tool_name] = outcome
                    raise
                except ToolExecutionError:
                    outcome = _ParallelToolOutcome("函数执行被安全拒绝")
                    await record_once(
                        prepared,
                        status=ToolCallStatus.REJECTED,
                        error_type="ToolExecutionRejected",
                    )
                    outcomes[prepared.tool_name] = outcome
                    raise
                except Exception:
                    logger.error("并行自定义工具执行失败，异常详情已安全省略")
                    outcome = _ParallelToolOutcome("函数执行出错，异常详情已安全省略")
                    await record_once(
                        prepared,
                        status=ToolCallStatus.FAILED,
                        error_type="ToolExecutionError",
                    )
                    outcomes[prepared.tool_name] = outcome
                    raise
                await record_once(
                    prepared,
                    status=ToolCallStatus.COMPLETED,
                    result_preview=outcome.content,
                )
                outcomes[prepared.tool_name] = outcome
                return outcome

            async def invocation(
                dependencies: Mapping[str, Any],
            ) -> _ParallelToolOutcome:
                traces.setdefault(
                    prepared.tool_name,
                    (time.time(), time.monotonic()),
                )
                try:
                    report = await batch.runner.execute(
                        tool_name=prepared.tool_name,
                        invocation=handler,
                        dependencies=dependencies,
                        deadline=runtime.deadline,
                        is_superuser=bool(getattr(self, "is_superuser", False)),
                    )
                except asyncio.CancelledError:
                    if prepared.tool_name not in trace_attempted:
                        outcome = _ParallelToolOutcome("并行工具调用已安全取消")
                        await record_once(
                            prepared,
                            status=ToolCallStatus.CANCELLED,
                            error_type="CancelledError",
                        )
                        outcomes[prepared.tool_name] = outcome
                    raise
                except TrustedRunnerExecutionTimeout:
                    if prepared.tool_name not in trace_attempted:
                        runtime_metrics.tool_timeouts += 1
                        outcome = _ParallelToolOutcome("函数执行超时，已安全终止")
                        await record_once(
                            prepared,
                            status=ToolCallStatus.TIMED_OUT,
                            error_type="TimeoutError",
                        )
                        outcomes[prepared.tool_name] = outcome
                    raise
                except TrustedRunnerEligibilityError:
                    if prepared.tool_name not in trace_attempted:
                        outcome = _ParallelToolOutcome("函数执行被安全拒绝")
                        await record_once(
                            prepared,
                            status=ToolCallStatus.REJECTED,
                            error_type="TrustedRunnerRejected",
                        )
                        outcomes[prepared.tool_name] = outcome
                    raise
                except TrustedRunnerPoolError:
                    if prepared.tool_name not in trace_attempted:
                        outcome = _ParallelToolOutcome("函数执行出错，异常详情已安全省略")
                        await record_once(
                            prepared,
                            status=ToolCallStatus.FAILED,
                            error_type="TrustedRunnerExecutionError",
                        )
                        outcomes[prepared.tool_name] = outcome
                    raise
                if not isinstance(report.result, _ParallelToolOutcome):
                    raise RuntimeError("trusted runner 返回了非法工具结果")
                return report.result

            return invocation

        invocations = {prepared.tool_name: build_invocation(prepared) for prepared in batch.calls}
        execution_error: ReadOnlyParallelExecutionError | None = None
        report = None
        try:
            report = await batch.executor.execute(
                graph=batch.graph,
                selected_tools=tool_names,
                effects={prepared.tool_name: ToolEffect.READ_ONLY for prepared in batch.calls},
                invocations=invocations,
                deadline=runtime.deadline,
            )
        except asyncio.CancelledError:
            raise
        except ReadOnlyParallelExecutionError as error:
            execution_error = error

        if trace_failures:
            raise _ParallelTracePersistenceError("并行工具 trace 持久化失败，已拒绝伪造成功") from None
        if report is not None:
            for prepared in batch.calls:
                outcome = report.result_for(prepared.tool_name)
                if not isinstance(outcome, _ParallelToolOutcome):
                    raise RuntimeError("parallel executor 返回了非法工具结果")
                outcomes[prepared.tool_name] = outcome
        else:
            timed_out = isinstance(
                execution_error,
                ReadOnlyParallelExecutionTimeout,
            )
            for prepared in batch.calls:
                if prepared.tool_name in outcomes:
                    continue
                if timed_out:
                    status = ToolCallStatus.TIMED_OUT
                    error_type = "TimeoutError"
                    outcome = _ParallelToolOutcome("函数执行超时，已安全终止")
                else:
                    status = ToolCallStatus.REJECTED
                    error_type = "ParallelDependencyAborted"
                    outcome = _ParallelToolOutcome("并行工具调用已因同批次失败安全跳过")
                await record_once(
                    prepared,
                    status=status,
                    error_type=error_type,
                )
                outcomes[prepared.tool_name] = outcome

        for prepared in batch.calls:
            outcome = outcomes[prepared.tool_name]
            if outcome.images:
                self._pending_vision_images.extend(outcome.images)
            send_message_list.append(
                {
                    "role": "tool",
                    "tool_call_id": prepared.call["id"],
                    "content": outcome.content,
                }
            )
        image_limit = config_parser.get_config("max_tool_images", 4)
        if len(self._pending_vision_images) > image_limit:
            self._pending_vision_images = self._pending_vision_images[:image_limit]
        self._append_tool_round_history(
            assistant_msg=assistant_msg,
            tool_calls=tool_calls,
            send_message_list=send_message_list,
        )
        return send_message_list

    async def _execute_tools(
        self,
        tool_calls: list,
        result_text: str,
        send_message_list: list,
        reasoning_content: str,
    ) -> list:
        """执行工具调用，并更新消息列表"""
        for call in tool_calls:
            if (
                not call.get("function", {}).get("arguments")
                or not str(call["function"]["arguments"]).strip()
            ):
                call["function"]["arguments"] = "{}"

        parallel_result = await self._try_execute_read_only_parallel_tools(
            tool_calls,
            result_text,
            send_message_list,
            reasoning_content,
        )
        if parallel_result is not None:
            return parallel_result

        max_tool_calls_per_round = 1
        executable_tool_calls = tool_calls[:max_tool_calls_per_round]
        skipped_tool_calls = tool_calls[max_tool_calls_per_round:]
        if skipped_tool_calls:
            logger.warning(
                f"本轮工具调用数量为 {len(tool_calls)}，超过上限 "
                f"{max_tool_calls_per_round}，将跳过超出的调用"
            )

        content_for_history = str(result_text) if result_text else ""
        if self.emotion_flag and content_for_history:
            content_for_history, _ = parse_emotion(content_for_history)
        # 提取本次调用的所有工具名称
        called_func_names = [
            call.get("function", {}).get("name", "未知插件") for call in executable_tool_calls
        ]
        func_names_str = ", ".join(called_func_names)

        assistant_msg = {
            "role": "assistant",
            "content": content_for_history.strip()
            or f"（正在调用工具: {func_names_str}）",
            "tool_calls": tool_calls,
        }
        # 仅在有思维链且非空时附加
        if reasoning_content:
            assistant_msg["reasoning_content"] = reasoning_content
        send_message_list.append(assistant_msg)
        text_to_send = result_text  # 暂存大模型回复文本，防止多个插件时被重复发送
        is_superuser = bool(getattr(self, "is_superuser", False))
        for call in executable_tool_calls:
            trace_created_at = time.time()
            trace_started_monotonic = time.monotonic()
            result_limit = config_parser.get_config("max_tool_result_chars", 6000)
            func_name = call["function"]["name"]
            tool_view = self.tool_snapshot.resolve_llm_tool_execution(
                func_name,
                is_superuser=is_superuser,
            )
            if not hasattr(self, "_current_tool_usage"):
                self._current_tool_usage = Counter()
            self._current_tool_usage[func_name] += 1
            runtime_metrics.tool_steps += 1
            self.messages_handler.messages_entity.add_used_plugins({func_name})

            try:
                args = json.loads(call["function"]["arguments"])
            except Exception as error:
                args = None
                argument_error = f"工具参数不是有效 JSON: {error}"
            else:
                parameters = None
                if tool_view is not None and tool_view.spec is not None:
                    parameters = tool_view.spec.parameters
                elif (
                    tool_view is not None
                    and tool_view.route is LlmToolExecutionRoute.CUSTOM_TOOL
                    and tool_view.legacy_entry is not None
                ):
                    parameters = tool_view.legacy_entry.get("parameters")
                elif (
                    tool_view is not None
                    and tool_view.route
                    is LlmToolExecutionRoute.NONEBOT_PLUGIN
                ):
                    parameters = (
                        {
                            "properties": {"command": {"type": "string"}},
                            "required": ["command"],
                        }
                    )
                argument_error = self._validate_tool_arguments(args, parameters)
            if argument_error:
                await self._record_agent_tool_outcome(
                    call=call,
                    tool_view=tool_view,
                    arguments=args if isinstance(args, Mapping) else {},
                    status=ToolCallStatus.REJECTED,
                    created_at=trace_created_at,
                    started_monotonic=trace_started_monotonic,
                    error_type="ToolArgumentsRejected",
                )
                send_message_list.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": f"函数参数错误：{argument_error}。请修正后重新调用。",
                    }
                )
                continue
            assert isinstance(args, dict)
            logger.info(f"准备执行函数: {func_name}，参数字段: {sorted(args)}")

            repeated_limit = config_parser.get_config("max_repeated_tool_calls", 2)
            if self._current_tool_usage[func_name] > repeated_limit:
                tool_result = (
                    f"工具 {func_name} 已达到单任务重复调用上限 {repeated_limit}，"
                    "请基于已有结果完成回答。"
                )
                send_message_list.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": tool_result,
                    }
                )
                await self._record_agent_tool_outcome(
                    call=call,
                    tool_view=tool_view,
                    arguments=args,
                    status=ToolCallStatus.REJECTED,
                    created_at=trace_created_at,
                    started_monotonic=trace_started_monotonic,
                    error_type="ToolRepeatLimit",
                )
                continue

            if tool_view is None:
                if text_to_send:
                    await self.send_emotion_message(text_to_send)
                    text_to_send = ""
                send_message_list.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": (
                            f"工具 {func_name} 不在当前 generation 的工具目录中，"
                            "已拒绝执行。"
                        ),
                    }
                )
                await self._record_agent_tool_outcome(
                    call=call,
                    tool_view=None,
                    arguments=args,
                    status=ToolCallStatus.REJECTED,
                    created_at=trace_created_at,
                    started_monotonic=trace_started_monotonic,
                    error_type="UnknownTool",
                )
                continue

            decision = tool_view.trust_decision
            if decision is not None and decision.audit_required:
                logger.info(f"工具 trust decision: {decision.audit_metadata()}")
            pending_transition = (
                decision is not None
                and not decision.allowed
                and decision.confirmation_required
                and tool_view.route is LlmToolExecutionRoute.CUSTOM_TOOL
                and tool_view.spec is not None
                and tool_view.spec.effect is ToolEffect.MUTATING
                and (
                    tool_view.spec.permission != "superuser"
                    or is_superuser
                )
            )
            if decision is not None and not decision.allowed and not pending_transition:
                if text_to_send:
                    await self.send_emotion_message(text_to_send)
                    text_to_send = ""
                send_message_list.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": (
                            f"工具 {func_name} 未执行：{decision.reason}。"
                        ),
                    }
                )
                await self._record_agent_tool_outcome(
                    call=call,
                    tool_view=tool_view,
                    arguments=args,
                    status=ToolCallStatus.REJECTED,
                    created_at=trace_created_at,
                    started_monotonic=trace_started_monotonic,
                    error_type="ToolTrustRejected",
                )
                continue

            tool_result = "执行成功"
            trace_status = ToolCallStatus.COMPLETED
            trace_error_type: str | None = None
            if tool_view.route is LlmToolExecutionRoute.BUILTIN_SEARCH:
                query = args.get("query", "")
                if text_to_send:
                    await self.send_emotion_message(text_to_send)
                    text_to_send = ""  # 消耗掉，防止下一个插件重发
                else:
                    await self.bot.send(self.event, f"正在搜索: {query}...")
                try:
                    async with timeout_scope(self._tool_timeout_seconds()):
                        search_spec = tool_view.spec
                        assert search_spec is not None
                        search_res = await search_spec.handler(
                            query=query,
                            tool_snapshot=self.tool_snapshot,
                            is_superuser=is_superuser,
                        )
                except asyncio.CancelledError:
                    await self._record_agent_tool_outcome(
                        call=call,
                        tool_view=tool_view,
                        arguments=args,
                        status=ToolCallStatus.CANCELLED,
                        created_at=trace_created_at,
                        started_monotonic=trace_started_monotonic,
                        error_type="CancelledError",
                    )
                    raise
                except TimeoutError:
                    runtime_metrics.tool_timeouts += 1
                    search_res = "联网搜索超时"
                    trace_status = ToolCallStatus.TIMED_OUT
                    trace_error_type = "TimeoutError"
                except Exception:
                    logger.error("联网搜索工具执行失败，异常详情已安全省略")
                    search_res = "联网搜索执行失败，请稍后重试"
                    trace_status = ToolCallStatus.FAILED
                    trace_error_type = "ToolExecutionError"
                tool_result = search_res if search_res else "未找到相关结果"

            elif tool_view.route is LlmToolExecutionRoute.CUSTOM_TOOL:
                if text_to_send:
                    await self.send_emotion_message(text_to_send)
                    text_to_send = ""
                else:
                    await self.bot.send(self.event, f"正在调用函数: {func_name}...")
                try:
                    tool_entry = tool_view.legacy_entry
                    assert tool_entry is not None
                    spec = tool_view.spec
                    if spec is not None and spec.effect == ToolEffect.MUTATING:
                        # A model-provided flag or confirmation phrase in the original
                        # request is never authorization. Freeze the exact call and wait
                        # for a separate, user-bound nonce message instead.
                        args.pop("confirm", None)
                        try:
                            await validate_pending_custom_tool(
                                func_name,
                                tool_entry,
                                args,
                                bot=self.bot,
                                event=self.event,
                            )
                        except ToolExecutionError as error:
                            send_message_list.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": call["id"],
                                    "content": f"工具 {func_name} 未执行：{error}。",
                                }
                            )
                            await self._record_agent_tool_outcome(
                                call=call,
                                tool_view=tool_view,
                                arguments=args,
                                status=ToolCallStatus.REJECTED,
                                created_at=trace_created_at,
                                started_monotonic=trace_started_monotonic,
                                error_type="PendingToolValidationRejected",
                            )
                            continue
                        try:
                            action = await pending_action_store.create(
                                bot=self.bot,
                                event=self.event,
                                tool_name=func_name,
                                arguments=args,
                                generation=getattr(self.tool_snapshot, "generation", 0),
                                bundle_digest=tool_entry.get("bundle_digest"),
                            )
                        except PendingActionError as error:
                            tool_result = f"工具 {func_name} 未执行：{error}。"
                            pending_status = ToolCallStatus.FAILED
                            pending_error = "PendingActionError"
                            confirmation_id = None
                        else:
                            remaining = pending_action_store.remaining_ttl_seconds(
                                action
                            )
                            confirmation = (
                                f"工具 {func_name} 会修改外部状态，尚未执行。\n"
                                f"请在 {remaining} 秒内单独发送：确认执行 {action.nonce}"
                            )
                            await self.bot.send(self.event, confirmation)
                            tool_result = (
                                f"{confirmation}\n"
                                "[系统提示]：确认指令已直接发送给用户，"
                                "不得代替用户确认或声称操作已经完成。"
                            )
                            pending_status = ToolCallStatus.WAITING_CONFIRMATION
                            pending_error = None
                            confirmation_id = action.nonce
                        send_message_list.append(
                            {
                                "role": "tool",
                                "tool_call_id": call["id"],
                                "content": tool_result,
                            }
                        )
                        runtime = getattr(self, "agent_runtime", None)
                        if (
                            runtime is not None
                            and pending_status
                            is ToolCallStatus.WAITING_CONFIRMATION
                        ):
                            await runtime.advance(
                                AgentRunState.WAITING_CONFIRMATION
                            )
                        await self._record_agent_tool_outcome(
                            call=call,
                            tool_view=tool_view,
                            arguments=args,
                            status=pending_status,
                            created_at=trace_created_at,
                            started_monotonic=trace_started_monotonic,
                            result_preview=tool_result,
                            confirmation_id=confirmation_id,
                            error_type=pending_error,
                        )
                        if (
                            runtime is not None
                            and pending_status
                            is ToolCallStatus.WAITING_CONFIRMATION
                        ):
                            await runtime.advance(AgentRunState.EXECUTING)
                        continue
                    async with timeout_scope(self._tool_timeout_seconds()):
                        result = await execute_custom_tool(
                            func_name,
                            tool_entry,
                            args,
                            bot=self.bot,
                            event=self.event,
                        )
                    # The shared executor has already applied the ToolSpec-specific
                    # text and image contract. Do not override it with the global
                    # fallback below.
                    render_limit = (
                        spec.result_limit
                        if spec is not None and spec.result_limit is not None
                        else result_limit
                    )
                    result_limit = None
                    result_images = list(result.images)
                    if result_images:
                        self._pending_vision_images.extend(result_images)
                    rendered_result = render_tool_result(
                        result,
                        max_chars=render_limit,
                    )
                    if rendered_result:
                        tool_result = f"函数执行返回结果：\n{rendered_result}"
                    else:
                        tool_result = "函数执行成功，但未返回有效结果。"
                except asyncio.CancelledError:
                    await self._record_agent_tool_outcome(
                        call=call,
                        tool_view=tool_view,
                        arguments=args,
                        status=ToolCallStatus.CANCELLED,
                        created_at=trace_created_at,
                        started_monotonic=trace_started_monotonic,
                        error_type="CancelledError",
                    )
                    raise
                except TimeoutError:
                    runtime_metrics.tool_timeouts += 1
                    trace_status = ToolCallStatus.TIMED_OUT
                    trace_error_type = "TimeoutError"
                    tool_result = "函数执行超时，已安全终止"
                except ToolExecutionTimeoutError:
                    trace_status = ToolCallStatus.TIMED_OUT
                    trace_error_type = "TimeoutError"
                    tool_result = "函数执行超时，已安全终止"
                except ToolExecutionError as error:
                    trace_status = ToolCallStatus.REJECTED
                    trace_error_type = "ToolExecutionRejected"
                    tool_result = f"函数执行出错: {error}"
                except Exception:
                    logger.error("自定义工具执行失败，异常详情已安全省略")
                    trace_status = ToolCallStatus.FAILED
                    trace_error_type = "ToolExecutionError"
                    tool_result = "函数执行出错，异常详情已安全省略"
            else:
                assert tool_view.route is LlmToolExecutionRoute.NONEBOT_PLUGIN
                if text_to_send:
                    await self.send_emotion_message(text_to_send)
                    text_to_send = ""
                else:
                    await self.bot.send(self.event, f"正在执行指令: {func_name}...")
                command = args.get("command", "")
                try:
                    async with timeout_scope(self._tool_timeout_seconds()):
                        if tool_view.provider_authoritative:
                            plugin_spec = tool_view.spec
                            assert plugin_spec is not None
                            plugin_result = await plugin_spec.handler(
                                command=command,
                                _bot=self.bot,
                                _event=self.event,
                                _format_message_dict=self.format_message_dict,
                            )
                            if not isinstance(plugin_result, ToolResult):
                                raise TypeError(
                                    "NoneBot Provider handler 必须返回 ToolResult"
                                )
                        else:
                            plugin_text, plugin_images = (
                                await event_simulator.dispatch_event(
                                    self.bot,
                                    self.event,
                                    command,
                                    self.format_message_dict,
                                    plugin_name=func_name,
                                )
                            )
                            plugin_result = ToolResult(
                                text=plugin_text,
                                images=tuple(plugin_images),
                            )
                except asyncio.CancelledError:
                    await self._record_agent_tool_outcome(
                        call=call,
                        tool_view=tool_view,
                        arguments=args,
                        status=ToolCallStatus.CANCELLED,
                        created_at=trace_created_at,
                        started_monotonic=trace_started_monotonic,
                        error_type="CancelledError",
                    )
                    raise
                except TimeoutError:
                    runtime_metrics.tool_timeouts += 1
                    trace_status = ToolCallStatus.TIMED_OUT
                    trace_error_type = "TimeoutError"
                    plugin_result = None
                    tool_result = "插件执行超时，已安全终止"
                except Exception:
                    logger.error("NoneBot 插件工具执行失败，异常详情已安全省略")
                    trace_status = ToolCallStatus.FAILED
                    trace_error_type = "ToolExecutionError"
                    plugin_result = None
                    tool_result = "插件执行失败，异常详情已安全省略"
                if plugin_result is not None:
                    plugin_images = list(plugin_result.images)
                    _PLUGIN_SYSTEM_HINT = (
                        "\n\n[系统提示]：上述结果已对用户可见。注意：若执行不正确或者用户的原始请求需要多个步骤，"
                        "请务重试或者继续调用下一个工具！如果所有任务均已完成，请直接做一两句话的简短总结，"
                        "严禁重复上述已发送的结果。"
                    )
                    if plugin_images:
                        self._pending_vision_images.extend(plugin_images)
                    rendered_plugin_result = render_tool_result(plugin_result)
                    if rendered_plugin_result:
                        tool_result = (
                            f"插件执行返回结果：\n"
                            f"{rendered_plugin_result}{_PLUGIN_SYSTEM_HINT}"
                        )
                    else:
                        tool_result = "插件已执行，但未返回有效文本。[系统提示]：如果有后续操作，请继续调用下一个工具。"

            if result_limit is not None and len(tool_result) > result_limit:
                tool_result = tool_result[:result_limit] + "\n...[工具结果已截断]"
            image_limit = config_parser.get_config("max_tool_images", 4)
            if len(self._pending_vision_images) > image_limit:
                self._pending_vision_images = self._pending_vision_images[:image_limit]
            send_message_list.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": tool_result,
                }
            )
            await self._record_agent_tool_outcome(
                call=call,
                tool_view=tool_view,
                arguments=args,
                status=trace_status,
                created_at=trace_created_at,
                started_monotonic=trace_started_monotonic,
                result_preview=tool_result,
                error_type=trace_error_type,
            )

        for call in skipped_tool_calls:
            func_name = call.get("function", {}).get("name", "未知插件")
            skipped_created_at = time.time()
            skipped_started_monotonic = time.monotonic()
            try:
                skipped_view = self.tool_snapshot.resolve_llm_tool_execution(
                    func_name,
                    is_superuser=is_superuser,
                )
            except Exception:
                skipped_view = None
            try:
                skipped_arguments = json.loads(
                    call.get("function", {}).get("arguments") or "{}"
                )
            except Exception:
                skipped_arguments = {}
            if not isinstance(skipped_arguments, Mapping):
                skipped_arguments = {}
            send_message_list.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": (
                        f"本轮工具调用数量超过上限 {max_tool_calls_per_round}，"
                        f"工具 {func_name} 已跳过。请在下一轮根据需要重新调用。"
                    ),
                }
            )
            await self._record_agent_tool_outcome(
                call=call,
                tool_view=skipped_view,
                arguments=skipped_arguments,
                status=ToolCallStatus.REJECTED,
                created_at=skipped_created_at,
                started_monotonic=skipped_started_monotonic,
                error_type="ToolRoundCallLimit",
            )

        # 将本 round 的工具消息（截断结果）追加到历史记录 entity，供下轮对话使用
        self._append_tool_round_history(
            assistant_msg=assistant_msg,
            tool_calls=tool_calls,
            send_message_list=send_message_list,
        )
        return send_message_list

    def _build_tool_limit_summary_prompt(self) -> str:
        return (
            "系统提示：工具自动调用轮次已达当前上限。请根据前序步骤收集到的工具结果，"
            "给出初步结论或阶段性总结。不要继续调用工具；如果任务未彻底完成，请直接在回复末尾主动询问用户是否需要继续执行。"
        )

    def _build_empty_tool_summary_fallback(self) -> str:
        tool_messages = self.messages_handler.messages_entity.tool_messages
        last_tool_result = next(
            (
                message.get("content", "")
                for message in reversed(tool_messages)
                if message.get("role") == "tool"
            ),
            "",
        )
        if last_tool_result:
            summary = last_tool_result[:200]
            suffix = "..." if len(last_tool_result) > 200 else ""
            return f"工具已经执行完毕，但模型没有返回总结。最后一次工具结果摘要：{summary}{suffix}"
        return "工具已经执行完毕，但模型没有返回总结。"

    async def _request_tool_summary_retry(
        self,
        session,
        headers: dict,
        send_message_list: list,
        timeout,
    ) -> str:
        retry_messages = list(send_message_list)
        retry_messages.append(
            {
                "role": "user",
                "content": (
                    "系统提示：上一轮工具执行后你没有给出可见总结。请只基于已有工具结果，"
                    "用简短中文回复用户当前结论；不要继续调用工具。"
                ),
            }
        )
        retry_data = {
            "model": self.model_info["model"],
            "messages": retry_messages,
            "stream": False,
        }
        for key in ["max_tokens", "temperature", "top_p", "top_k"]:
            if self.model_info.get(key) is not None:
                retry_data[key] = self.model_info[key]
        if extra_payload := self.model_info.get("extra_payload"):
            if isinstance(extra_payload, dict):
                retry_data.update(extra_payload)

        success, summary_text, retry_tool_calls, _ = await self.none_stream_llm_chat(
            session,
            self.model_info["url"],
            headers,
            retry_data,
            self.model_info.get("proxy"),
            timeout,
        )
        if not success:
            return ""
        if retry_tool_calls:
            logger.warning("工具总结补救请求仍返回了 tool_calls，已忽略并使用兜底总结")
            return ""
        return (summary_text or "").strip()

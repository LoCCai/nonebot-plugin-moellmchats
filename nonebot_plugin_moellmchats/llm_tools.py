from collections import Counter
from collections.abc import Mapping
import traceback
from typing import Any

from nonebot.log import logger
import ujson as json

from .builtin_tools import WEB_SEARCH_TOOL_SPEC
from .compat import timeout as timeout_scope
from .config import config_parser
from .event_simulator import event_simulator
from .pending_actions import PendingActionError, pending_action_store
from .runtime_metrics import runtime_metrics
from .tool_contracts import (
    ToolEffect,
    validate_tool_arguments,
)
from .tool_execution import (
    ToolExecutionError,
    execute_custom_tool,
    validate_pending_custom_tool,
)
from .utils import parse_emotion


class LlmToolsMixin:
    @staticmethod
    def _validate_tool_arguments(
        arguments: object,
        parameters: Mapping[str, Any] | None,
    ) -> str | None:
        return validate_tool_arguments(arguments, parameters)

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
        for call in executable_tool_calls:
            result_limit = config_parser.get_config("max_tool_result_chars", 6000)
            func_name = call["function"]["name"]
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
                if func_name == WEB_SEARCH_TOOL_SPEC.name:
                    parameters = WEB_SEARCH_TOOL_SPEC.parameters
                elif func_name in self.tool_snapshot.custom_tools:
                    parameters = self.tool_snapshot.custom_tools[func_name].get(
                        "parameters"
                    )
                elif func_name in self.tool_snapshot.plugin_info:
                    parameters = {
                        "properties": {"command": {"type": "string"}},
                        "required": ["command"],
                    }
                argument_error = self._validate_tool_arguments(args, parameters)
            if argument_error:
                send_message_list.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": f"函数参数错误：{argument_error}。请修正后重新调用。",
                    }
                )
                continue
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
                continue

            tool_result = "执行成功"
            if func_name == WEB_SEARCH_TOOL_SPEC.name:
                query = args.get("query", "")
                if text_to_send:
                    await self.send_emotion_message(text_to_send)
                    text_to_send = ""  # 消耗掉，防止下一个插件重发
                else:
                    await self.bot.send(self.event, f"正在搜索: {query}...")
                try:
                    async with timeout_scope(
                        config_parser.get_config("tool_timeout_seconds", 30)
                    ):
                        search_res = await WEB_SEARCH_TOOL_SPEC.handler(
                            query=query,
                            tool_snapshot=self.tool_snapshot,
                        )
                except TimeoutError:
                    runtime_metrics.tool_timeouts += 1
                    search_res = "联网搜索超时"
                tool_result = search_res if search_res else "未找到相关结果"

            elif func_name in self.tool_snapshot.custom_tools:
                if text_to_send:
                    await self.send_emotion_message(text_to_send)
                    text_to_send = ""
                else:
                    await self.bot.send(self.event, f"正在调用函数: {func_name}...")
                try:
                    tool_entry = self.tool_snapshot.custom_tools[func_name]
                    spec = tool_entry.get("tool_spec")
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
                        send_message_list.append(
                            {
                                "role": "tool",
                                "tool_call_id": call["id"],
                                "content": tool_result,
                            }
                        )
                        continue
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
                    result_limit = None
                    result_text = result.text
                    result_images = list(result.images)
                    if result_images:
                        self._pending_vision_images.extend(result_images)
                        if result_text:
                            tool_result = (
                                f"函数执行返回结果：\n{result_text}\n\n"
                                f"[系统提示]：该函数还返回了 {len(result_images)} 张图片。"
                            )
                        else:
                            tool_result = (
                                f"函数执行完毕并返回了 {len(result_images)} 张图片。"
                            )
                    else:
                        tool_result = result_text
                except Exception as e:
                    logger.error(traceback.format_exc())
                    tool_result = f"函数执行出错: {e!s}"
            else:
                if text_to_send:
                    await self.send_emotion_message(text_to_send)
                    text_to_send = ""
                else:
                    await self.bot.send(self.event, f"正在执行指令: {func_name}...")
                command = args.get("command", "")
                plugin_text, plugin_images = await event_simulator.dispatch_event(
                    self.bot,
                    self.event,
                    command,
                    self.format_message_dict,
                    plugin_name=func_name,
                )
                _PLUGIN_SYSTEM_HINT = (
                    "\n\n[系统提示]：上述结果已对用户可见。注意：若执行不正确或者用户的原始请求需要多个步骤，"
                    "请务重试或者继续调用下一个工具！如果所有任务均已完成，请直接做一两句话的简短总结，"
                    "严禁重复上述已发送的结果。"
                )
                if plugin_images:
                    self._pending_vision_images.extend(plugin_images)
                    text_part = (
                        f"插件执行返回结果：\n{plugin_text}"
                        if plugin_text
                        else "插件执行完毕并返回了图片（见下方图片消息）"
                    )
                    tool_result = text_part + _PLUGIN_SYSTEM_HINT
                elif plugin_text:
                    tool_result = (
                        f"插件执行返回结果：\n{plugin_text}{_PLUGIN_SYSTEM_HINT}"
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

        for call in skipped_tool_calls:
            func_name = call.get("function", {}).get("name", "未知插件")
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

        # 将本 round 的工具消息（截断结果）追加到历史记录 entity，供下轮对话使用
        HISTORY_TOOL_RESULT_LIMIT = 300
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
                    m["content"]
                    for m in reversed(send_message_list)
                    if m.get("role") == "tool" and m.get("tool_call_id") == call["id"]
                ),
                "",
            )
            history_msgs.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": tool_result_content[:HISTORY_TOOL_RESULT_LIMIT],
                }
            )
        self.messages_handler.messages_entity.tool_messages.extend(history_msgs)
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

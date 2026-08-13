import asyncio
from collections import Counter
import inspect
import traceback

from nonebot.log import logger
import ujson as json

from .compat import timeout as timeout_scope
from .config import config_parser
from .event_simulator import event_simulator
from .network_safety import validate_url_arguments
from .request_manager import get_current_request_id
from .runtime_metrics import runtime_metrics
from .search import Search
from .tool_contracts import ToolContext, ToolEffect, ToolResult
from .tool_manager import tool_manager
from .utils import parse_emotion


class LlmToolsMixin:
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
            except Exception:
                args = {}
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
            if func_name == "web_search":
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
                        search_res = await Search(query).get_search()
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
                    func = tool_entry["func"]
                    spec = tool_entry.get("tool_spec")
                    if spec is not None and spec.permission == "superuser":
                        superusers = {
                            str(user_id)
                            for user_id in getattr(self.bot.config, "superusers", set())
                        }
                        if str(self.event.user_id) not in superusers:
                            tool_result = f"工具 {func_name} 仅允许超级用户执行。"
                            send_message_list.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": call["id"],
                                    "content": tool_result,
                                }
                            )
                            continue
                    confirmed = False
                    if (
                        spec is not None
                        and spec.effect == ToolEffect.MUTATING
                    ):
                        confirmed = bool(args.pop("confirm", False)) and (
                            "确认执行"
                            in "".join(self.format_message_dict.get("text") or [])
                        )
                        if not confirmed:
                            tool_result = (
                                f"工具 {func_name} 会修改外部状态，需要用户明确确认后才能执行。"
                            )
                            send_message_list.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": call["id"],
                                    "content": tool_result,
                                }
                            )
                            continue
                    # 依赖注入核心逻辑
                    sig = inspect.signature(func)
                    if not any(
                        param.kind == inspect.Parameter.VAR_KEYWORD
                        for param in sig.parameters.values()
                    ):
                        unexpected_args = [
                            key for key in args
                            if key not in sig.parameters
                        ]
                        if unexpected_args:
                            available_args = [
                                key for key, param in sig.parameters.items()
                                if not key.startswith("_")
                                and param.kind
                                in (
                                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                    inspect.Parameter.KEYWORD_ONLY,
                                )
                            ]
                            tool_result = (
                                f"函数参数错误：{func_name} 不支持参数 "
                                f"{', '.join(unexpected_args)}。"
                                f"可用参数：{', '.join(available_args) or '无'}。"
                                "请根据可用参数重新调用该工具。"
                            )
                            logger.warning(
                                f"函数 {func_name} 收到未声明参数: {unexpected_args}，"
                                f"可用参数: {available_args}"
                            )
                            send_message_list.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": call["id"],
                                    "content": tool_result,
                                }
                            )
                            continue
                    if "_tool_manager" in sig.parameters:
                        args["_tool_manager"] = tool_manager
                    # 注入 bot 和 event
                    if "_bot" in sig.parameters:
                        args["_bot"] = self.bot
                    if "_event" in sig.parameters:
                        args["_event"] = self.event
                    if "_tool_context" in sig.parameters:
                        args["_tool_context"] = ToolContext(
                            bot=self.bot,
                            event=self.event,
                            request_id=get_current_request_id(),
                            confirmed=confirmed,
                        )
                    timeout = (
                        spec.timeout_seconds
                        if spec is not None and spec.timeout_seconds
                        else config_parser.get_config("tool_timeout_seconds", 30)
                    )
                    try:
                        async with timeout_scope(timeout):
                            await validate_url_arguments(args)
                            res = (
                                await func(**args)
                                if inspect.iscoroutinefunction(func)
                                else await asyncio.to_thread(func, **args)
                            )
                    except TimeoutError:
                        runtime_metrics.tool_timeouts += 1
                        raise RuntimeError(f"工具执行超过 {timeout} 秒预算") from None

                    result_limit = (
                        spec.result_limit
                        if spec is not None and spec.result_limit
                        else config_parser.get_config("max_tool_result_chars", 6000)
                    )
                    if isinstance(res, ToolResult):
                        result_text = res.text
                        result_images = list(res.images)
                    elif isinstance(res, dict):
                        result_text = (
                            res.get("text")
                            or res.get("content")
                            or res.get("message")
                            or ""
                        )
                        result_images = (
                            res.get("images")
                            or res.get("image_urls")
                            or []
                        )

                    else:
                        tool_result = str(res)

                    if isinstance(res, (ToolResult, dict)):
                        if isinstance(result_images, str):
                            result_images = [result_images]
                        result_images = [
                            image
                            for image in result_images
                            if isinstance(image, str) and image.strip()
                        ]
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
                            tool_result = str(result_text) if result_text else str(res)
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

            if len(tool_result) > result_limit:
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

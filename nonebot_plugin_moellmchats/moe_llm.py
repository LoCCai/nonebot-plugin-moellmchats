import asyncio
from collections import Counter
from collections.abc import Awaitable, Callable
import datetime
import random
import time
from typing import Any

import aiohttp
from nonebot.exception import ActionFailed, ApiNotAvailable, NetworkError
from nonebot.log import logger

from .agent_context_runtime import (
    AgentContextRuntimeError,
    AgentRequestRuntime,
)
from .agent_runtime import AgentRunState, AgentStepStatus, AgentStepType
from .compat import TimeoutError
from .compat import timeout as timeout_scope
from .config import config_parser
from .llm_api import LlmApiMixin
from .llm_payload import LlmPayloadMixin
from .llm_state import context_dict, token_usage_history
from .llm_tools import LlmToolsMixin
from .messages_handler import MessagesHandler
from .model_capabilities import ModelCapability
from .model_selector import model_selector
from .onebot_facade import (
    event_group_id,
    event_sender_name,
    event_user_id,
    onebot_protocol,
)
from .runtime_snapshot import runtime_snapshots
from .temperament_manager import temperament_manager
from .tool_manager import tool_manager
from .utils import get_emotion, get_emotions_names, get_session, parse_emotion

__all__ = ["MoeLlm", "context_dict", "token_usage_history"]

_PROGRESS_NOTICE_TIMEOUT_SECONDS = 1.0


class MoeLlm(LlmApiMixin, LlmPayloadMixin, LlmToolsMixin):
    def __init__(
        self,
        bot,
        event,
        format_message_dict: dict,
        is_objective: bool = False,
        temperament="默认",
        *,
        agent_runtime: AgentRequestRuntime | None = None,
    ):
        self.bot = bot
        self.event = event
        self.format_message_dict = format_message_dict
        self.user_id = event_user_id(event)
        self.is_objective = is_objective
        self.temperament = temperament
        if agent_runtime is not None and not isinstance(
            agent_runtime,
            AgentRequestRuntime,
        ):
            raise TypeError("agent_runtime 必须是 AgentRequestRuntime 或 None")
        self.agent_runtime = agent_runtime
        self.model_info = {}
        self.emotion_flag = False  # 判断本次对话是否发送表情包
        self.prompt = temperament_manager.get_temperament_prompt(temperament)
        self.dynamic_context = ""
        self._pending_vision_images: list = []  # 本轮工具调用返回的待处理图片
        self._tool_schema_record = None
        self._active_llm_tool_names: frozenset[str] = frozenset()
        self._active_llm_tool_descriptions: dict[str, str] = {}
        self._active_llm_tool_generation: int | None = None
        self._current_tool_usage = Counter()
        self._current_tool_fingerprint_usage = Counter()
        self._tool_call_fingerprints: dict[tuple[int, str, str], str] = {}
        self._tool_retry_blocked_tools: set[str] = set()
        self.tool_selection_source = "classification_model"
        self.tool_intent_digest = ""
        self._last_api_error_non_retryable = False
        bot_config = getattr(bot, "config", None)
        superusers = {str(user_id) for user_id in getattr(bot_config, "superusers", set())}
        self.is_superuser = self.user_id in superusers
        runtime_snapshot = runtime_snapshots.active()
        self.tool_snapshot = runtime_snapshot.tool_snapshot if runtime_snapshot is not None else tool_manager.snapshot()

    async def _validate_runtime_model_config(self) -> str | None:
        if (
            model_selector.get_model_for_capabilities(
                "selected_model",
                ModelCapability(
                    text=True,
                    vision=False,
                    tools=False,
                    json_schema=False,
                    reasoning=False,
                    streaming=False,
                ),
            )
            is None
        ):
            return "当前没有可用聊天模型，请检查模型配置后重载 LLM。"
        return None

    def prompt_handler(self):
        """处理动态上下文（时间、状态、群聊记录、工具记忆）"""
        dynamic_context_parts = ["<meta_info>"]
        user_id = event_sender_name(self.event)
        # 仅当不是"ai助手"时，才注入性格设定、表情包和群聊/私聊环境上下文
        if self.temperament != "ai助手":
            emotion_prompt = ""
            if config_parser.get_config("emotions_enabled") and random.random() < config_parser.get_config("emotion_rate"):
                self.emotion_flag = True
                emotion_prompt = (
                    "回复时根据回答内容，发送表情包，每次回复最多发一个表情包，格式为中括号+表情包名字，"
                    f"如：[表情包名字]。可选表情有{get_emotions_names()}"
                )

            group_id = event_group_id(self.event)
            if group_id is not None:
                dynamic_context_parts.append(f"Environment: QQ Group.{emotion_prompt}")
                if context_dict[group_id]:
                    dynamic_context_parts.append("Recent_Chat_Log:")
                    context_items = list(context_dict[group_id])[:-1]
                    max_chars = config_parser.get_config("max_history_chars", 16_000)
                    max_tokens = config_parser.get_config("max_history_tokens", 4_000)
                    selected: list[str] = []
                    chars = 0
                    tokens = 0
                    for item in reversed(context_items):
                        item = item[:max_chars]
                        item_tokens = max(1, (len(item) + 2) // 3)
                        if selected and (chars + len(item) > max_chars or tokens + item_tokens > max_tokens):
                            break
                        selected.append(item)
                        chars += len(item)
                        tokens += item_tokens
                    dynamic_context_parts.append("\n".join(reversed(selected)))
            else:
                dynamic_context_parts.append(f"Environment: Private Chat.{emotion_prompt}")
        # 注入时间
        if config_parser.get_config("show_datetime"):
            now = datetime.datetime.now()
            time_str = now.strftime("%Y-%m-%d %H:%M:%S")
            dynamic_context_parts.append(f"Time: {time_str}")
        # 注入用户 ID
        dynamic_context_parts.append(f"current user: {user_id}")
        if self.format_message_dict.get("reply_user"):
            dynamic_context_parts.append(
                "When a message contains 引用消息, answer 当前提问者, "
                "not the quoted speaker, unless the user explicitly asks to reply to the quoted speaker."
            )
        dynamic_context_parts.append("</meta_info>\n")
        self.dynamic_context = "\n".join(dynamic_context_parts)

    async def send_emotion_message(self, content: str) -> str:
        """处理和发送表情包
        Returns: str: 替换表情之后的内容
        """
        if self.emotion_flag:  # 本次对话发送表情包
            content, emotion_names_list = parse_emotion(content)
            delivered = False
            if content:
                await self.bot.send(self.event, content)
                delivered = True
            for emotion_name in emotion_names_list:
                # 发送
                if emotion := get_emotion(
                    emotion_name,
                    protocol=onebot_protocol(self.bot, self.event) or "onebot_v11",
                ):
                    try:
                        await self.bot.send(self.event, emotion)
                        delivered = True
                    except (ActionFailed, NetworkError, ApiNotAvailable) as error:
                        # 正文或前一个表情已经成功时，不重发不确定结果，
                        # 只隔离这个可选附件，避免 OneBot 动作失败、传输超时或
                        # 连接刚失效拖垮整轮 Matcher。
                        if not delivered:
                            raise
                        info = getattr(error, "info", {})
                        retcode = info.get("retcode") if isinstance(info, dict) else None
                        logger.warning(
                            "正文已发送，附加表情发送失败并已跳过："
                            f"emotion={emotion_name[:80]!r}, "
                            f"error_type={type(error).__name__!r}, "
                            f"retcode={retcode!r}"
                        )
        else:  # 默认直接发送
            await self.bot.send(self.event, content)
        return content

    def _remaining_request_seconds(self) -> float:
        if self.agent_runtime is None:
            value = config_parser.get_config("request_timeout_seconds", 180)
            return float(value)
        remaining = self.agent_runtime.deadline.remaining()
        if remaining <= 0:
            raise TimeoutError
        return remaining

    def _llm_request_timeout(self) -> aiohttp.ClientTimeout:
        return aiohttp.ClientTimeout(total=self._remaining_request_seconds())

    async def _send_retry_notice(self, retry_times: int) -> None:
        try:
            async with timeout_scope(_PROGRESS_NOTICE_TIMEOUT_SECONDS):
                await self.bot.send(
                    self.event,
                    f"api又卡了呐！第 {retry_times + 1} 次尝试，请勿多次发送~",
                )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning(
                "重试通知发送失败，继续当前请求: error_type={}",
                type(error).__name__,
            )

    async def _call_tool_summary_safely(
        self,
        operation: Callable[[], Awaitable[Any]],
    ) -> str:
        try:
            result = await self._call_model_with_trace(
                operation,
                input_preview="tool summary retry",
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning(
                "工具总结请求失败，使用固定兜底: error_type={}",
                type(error).__name__,
            )
            return ""
        return str(result or "")

    async def _call_model_with_trace(
        self,
        operation,
        *,
        input_preview: str,
    ):
        runtime = self.agent_runtime
        if runtime is None:
            return await operation()
        started_at = time.time()
        started_monotonic = time.monotonic()
        model = self.model_info.get("model", "unknown")
        try:
            remaining = runtime.deadline.remaining()
            if remaining <= 0:
                raise TimeoutError
            async with timeout_scope(remaining):
                result = await operation()
        except asyncio.CancelledError:
            await runtime.record_model_step(
                model=model,
                status=AgentStepStatus.CANCELLED,
                started_at=started_at,
                started_monotonic=started_monotonic,
                input_preview=input_preview,
                error_type="CancelledError",
            )
            raise
        except TimeoutError:
            await runtime.record_model_step(
                model=model,
                status=AgentStepStatus.TIMED_OUT,
                started_at=started_at,
                started_monotonic=started_monotonic,
                input_preview=input_preview,
                error_type="TimeoutError",
            )
            raise
        except Exception as error:
            await runtime.record_model_step(
                model=model,
                status=AgentStepStatus.FAILED,
                started_at=started_at,
                started_monotonic=started_monotonic,
                input_preview=input_preview,
                error_type=type(error).__name__,
            )
            raise
        success = bool(result[0]) if isinstance(result, tuple) and result else True
        await runtime.record_model_step(
            model=model,
            status=(AgentStepStatus.COMPLETED if success else AgentStepStatus.FAILED),
            started_at=started_at,
            started_monotonic=started_monotonic,
            input_preview=input_preview,
            output_preview=("model response accepted" if success else "model response rejected"),
            error_type=None if success else "ModelResponseError",
        )
        return result

    async def get_llm_chat(self) -> str | bool:
        self.messages_handler = MessagesHandler(self.user_id)
        if self.agent_runtime is not None:
            history_exchanges = config_parser.get_config("max_user_history", 8)
            if not isinstance(history_exchanges, int) or isinstance(history_exchanges, bool):
                history_exchanges = 8
            committed = await self.agent_runtime.load_committed_context(
                history_limit=max(1, min(200, history_exchanges * 2)),
            )
            if self.agent_runtime.persistent:
                self.messages_handler.bind_committed_history(committed.history)
        plain = self.messages_handler.pre_process(self.format_message_dict)
        if self.agent_runtime is not None:
            await self.agent_runtime.persist_user_message(plain)
        if validate_error := await self._validate_runtime_model_config():
            return validate_error

        # 1. 预处理模型信息
        classification_started_at = time.time()
        classification_started_monotonic = time.monotonic()
        try:
            if self.agent_runtime is None:
                prep_result = await self._prepare_model_info(plain)
            else:
                remaining = self.agent_runtime.deadline.remaining()
                if remaining <= 0:
                    raise TimeoutError
                async with timeout_scope(remaining):
                    prep_result = await self._prepare_model_info(plain)
        except asyncio.CancelledError:
            if self.agent_runtime is not None:
                await self.agent_runtime.record_model_step(
                    model="classification",
                    status=AgentStepStatus.CANCELLED,
                    step_type=AgentStepType.CLASSIFICATION,
                    started_at=classification_started_at,
                    started_monotonic=classification_started_monotonic,
                    error_type="CancelledError",
                )
            raise
        except TimeoutError:
            if self.agent_runtime is not None:
                await self.agent_runtime.record_model_step(
                    model="classification",
                    status=AgentStepStatus.TIMED_OUT,
                    step_type=AgentStepType.CLASSIFICATION,
                    started_at=classification_started_at,
                    started_monotonic=classification_started_monotonic,
                    error_type="TimeoutError",
                )
            raise
        except Exception as error:
            if self.agent_runtime is not None:
                await self.agent_runtime.record_model_step(
                    model="classification",
                    status=AgentStepStatus.FAILED,
                    step_type=AgentStepType.CLASSIFICATION,
                    started_at=classification_started_at,
                    started_monotonic=classification_started_monotonic,
                    error_type=type(error).__name__,
                )
            raise
        if self.agent_runtime is not None:
            await self.agent_runtime.record_model_step(
                model="classification",
                status=(AgentStepStatus.SKIPPED if isinstance(prep_result, str) else AgentStepStatus.COMPLETED),
                step_type=AgentStepType.CLASSIFICATION,
                started_at=classification_started_at,
                started_monotonic=classification_started_monotonic,
                output_preview=(None if isinstance(prep_result, str) else "classification prepared"),
                error_type=("ClassificationRejected" if isinstance(prep_result, str) else None),
            )
        if isinstance(prep_result, str):
            return prep_result
        if self.agent_runtime is not None:
            await self.agent_runtime.advance(
                AgentRunState.PLANNING,
                model=self.model_info.get("model", "unknown"),
            )

        self.prompt_handler()
        supports_tools = model_selector.get_use_tools() and not self.model_info.get("no_tools", False)
        send_message_list = self.messages_handler.get_send_message_list(supports_tools=supports_tools)
        system_content = self.prompt
        if self.dynamic_context:
            system_content += "\n" + self.dynamic_context
        if self.agent_runtime is not None:
            untrusted_context = self.agent_runtime.prompt_context.render_untrusted_prompt()
            if untrusted_context:
                system_content += "\n" + untrusted_context
        # 将动态上下文追加到系统提示末尾，避免部分模型不支持多条 system 消息
        send_message_list.insert(0, {"role": "system", "content": system_content})
        # 2. 构建 Payload
        data, current_stream_flag = self._build_payload(send_message_list)
        # DEBUG
        logger.debug(
            f"LLM payload: model={data.get('model')} messages={len(data.get('messages', []))} "
            f"tools={len(data.get('tools', []))} stream={data.get('stream', False)}"
        )
        if self.agent_runtime is not None:
            await self.agent_runtime.advance(AgentRunState.EXECUTING)

        headers = {
            "Authorization": self.model_info["key"],
            "Content-Type": "application/json",
            "Accept-Encoding": "identity",
        }

        max_tool_rounds = min(
            config_parser.get_config("max_tool_rounds", 6),
            config_parser.get_config("max_agent_steps", 6),
        )
        max_retry_times = config_parser.get_config("max_retry_times") or 3

        session = get_session()
        # 修改：增加上限至 max_tool_rounds + 1，以容纳最后一次强制总结
        for tool_round in range(max_tool_rounds + 1):
            result_text = ""
            success = False
            tool_calls = None
            reasoning_content = ""

            # 达到最大轮次时，移除工具强制总结
            if tool_round == max_tool_rounds:
                data.pop("tools", None)
                current_stream_flag = data["stream"]
                send_message_list.append(
                    {
                        "role": "user",
                        "content": self._build_tool_limit_summary_prompt(),
                    }
                )

            # 网络请求重试逻辑
            for retry_times in range(max_retry_times):
                self._last_api_error_non_retryable = False
                if retry_times > 0:
                    await self._send_retry_notice(retry_times)
                    await asyncio.sleep(2 ** (retry_times + 1))
                try:
                    if current_stream_flag:

                        async def model_operation():
                            return await self.stream_llm_chat(
                                session,
                                self.model_info["url"],
                                headers,
                                data,
                                self.model_info.get("proxy"),
                                self.model_info.get("is_segment"),
                                self._llm_request_timeout(),
                            )

                        (
                            success,
                            result_text,
                            tool_calls,
                            reasoning_content,
                        ) = await self._call_model_with_trace(
                            model_operation,
                            input_preview=(f"chat model request with {len(data.get('messages', []))} messages"),
                        )
                    else:

                        async def model_operation():
                            return await self.none_stream_llm_chat(
                                session,
                                self.model_info["url"],
                                headers,
                                data,
                                self.model_info.get("proxy"),
                                self._llm_request_timeout(),
                            )

                        (
                            success,
                            result_text,
                            tool_calls,
                            reasoning_content,
                        ) = await self._call_model_with_trace(
                            model_operation,
                            input_preview=(f"chat model request with {len(data.get('messages', []))} messages"),
                        )
                    if self.agent_runtime is not None:
                        await self.agent_runtime.flush_usage()
                    if success:
                        break
                    if self._last_api_error_non_retryable:
                        break
                except AgentContextRuntimeError:
                    raise
                except TimeoutError:
                    result_text = "网络超时呐，多半是api反应太慢（"
                except Exception:
                    logger.warning("LLM 请求异常；消息内容已从日志中省略")
                    logger.error("LLM 请求失败，异常详情已安全省略")
                    continue

            if not success:
                return result_text or "api寄！"

            # 3. 执行工具调用（非总结轮次才执行）
            if tool_calls and tool_round < max_tool_rounds:
                send_message_list = await self._execute_tools(tool_calls, result_text, send_message_list, reasoning_content)

                # 若插件返回了图片，自动切换至视觉模型并注入图片消息
                if self._pending_vision_images:
                    vision_model_info = model_selector.get_model_for_capabilities(
                        "vision_model",
                        ModelCapability(
                            text=True,
                            vision=True,
                            tools=model_selector.get_use_tools(),
                            json_schema=False,
                            reasoning=False,
                            streaming=False,
                        ),
                    )
                    if vision_model_info:
                        logger.info(f"插件返回图片，自动切换至视觉模型: {vision_model_info['model']}")
                        self.model_info = vision_model_info
                        data["model"] = vision_model_info["model"]
                        headers["Authorization"] = vision_model_info["key"]
                        current_stream_flag = vision_model_info.get("stream", False)
                        data["stream"] = current_stream_flag
                        if current_stream_flag:
                            data["stream_options"] = {"include_usage": True}
                        else:
                            data.pop("stream_options", None)
                        if vision_model_info.get("no_tools"):
                            data.pop("tools", None)
                            self._bind_active_llm_tool_schema([])
                        # 以 user 消息注入图片，视觉模型在下一轮可直接看到
                        image_content: list[dict[str, Any]] = [
                            {
                                "type": "text",
                                "text": "插件返回了以下图片，请结合上下文进行分析：",
                            }
                        ]
                        for url in self._pending_vision_images:
                            image_content.append({"type": "image_url", "image_url": {"url": url}})
                        send_message_list.append({"role": "user", "content": image_content})
                    else:
                        logger.warning("插件返回了图片，但未配置视觉模型，无法自动切换")
                        self._pending_vision_images = []
                        return (
                            "插件返回了图片，但未配置视觉模型。请先使用「设置视觉模型 <模型名或编号>」"
                            "配置一个支持图片输入的模型。"
                        )
                    self._pending_vision_images = []

                data["messages"] = send_message_list
                continue

            # ===== 循环结束分支（无工具调用或已达到总结轮次） =====
            if tool_calls and tool_round >= max_tool_rounds:
                logger.warning("工具轮次已达上限，但模型仍返回了 tool_calls，已停止执行并进入总结兜底")
                result_text = ""

            result_text_sent = False
            has_tool_messages = bool(self.messages_handler.messages_entity.tool_messages)
            if has_tool_messages and not (result_text or "").strip():

                async def summary_operation():
                    return await self._request_tool_summary_retry(
                        session,
                        headers,
                        send_message_list,
                        self._llm_request_timeout(),
                    )

                result_text = await self._call_tool_summary_safely(summary_operation)
                if self.agent_runtime is not None:
                    await self.agent_runtime.flush_usage()
                if result_text:
                    result_text = await self.send_emotion_message(result_text)
                    result_text_sent = True
                else:
                    result_text = self._build_empty_tool_summary_fallback()
                    result_text = await self.send_emotion_message(result_text)
                    result_text_sent = True

            if not result_text_sent and not current_stream_flag and result_text:
                result_text = await self.send_emotion_message(str(result_text))

            # 统一并完整保存上下文，用户说"继续"时大模型能够回想起历史工具调用记录
            if not self.is_objective:
                tool_history = self.messages_handler.messages_entity.tool_messages or None
                self.messages_handler.post_process(
                    assistant_msg=str(result_text or ""),
                    tool_messages=tool_history,
                )
                if self.agent_runtime is not None:
                    await self.agent_runtime.persist_assistant_message(
                        str(result_text or ""),
                        tool_messages=tool_history,
                    )

            if self.agent_runtime is not None:
                await self.agent_runtime.finish_success()

            return True

        return "请求处理异常结束"

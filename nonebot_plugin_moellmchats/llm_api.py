import asyncio
from collections.abc import Mapping
import datetime
import hashlib
import re

from nonebot.log import logger
import ujson as json

from .llm_state import token_usage_history
from .model_error_policy import is_content_policy_error_info
from .request_manager import get_current_request_elapsed


class LlmApiMixin:
    @staticmethod
    def _usage_integer(value: object) -> int:
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not 0 <= value <= (1 << 63) - 1
        ):
            return 0
        return value

    def _record_token_usage(self, usage: object) -> None:
        """Normalize an untrusted API usage payload without breaking chat."""

        try:
            payload = usage if isinstance(usage, Mapping) else {}
            prompt_tokens = self._usage_integer(payload.get("prompt_tokens"))
            completion_tokens = self._usage_integer(
                payload.get("completion_tokens")
            )
            prompt_details = payload.get("prompt_tokens_details")
            if not isinstance(prompt_details, Mapping):
                prompt_details = {}
            completion_details = payload.get("completion_tokens_details")
            if not isinstance(completion_details, Mapping):
                completion_details = {}
            cached_tokens = self._usage_integer(
                prompt_details.get("cached_tokens")
            )
            if cached_tokens == 0:
                cached_tokens = self._usage_integer(
                    payload.get("prompt_cache_hit_tokens")
                )
            cached_tokens = min(cached_tokens, prompt_tokens)
            reasoning_tokens = min(
                self._usage_integer(completion_details.get("reasoning_tokens")),
                completion_tokens,
            )
            total_tokens = min(
                (1 << 63) - 1,
                prompt_tokens + completion_tokens,
            )
            try:
                elapsed = get_current_request_elapsed()
            except Exception:
                elapsed = None
            try:
                tools = dict(getattr(self, "_current_tool_usage", {}))
            except Exception:
                tools = {}
            model_info = getattr(self, "model_info", {})
            if not isinstance(model_info, Mapping):
                model_info = {}
            model = model_info.get("model")
            model_label = model if isinstance(model, str) and model else "unknown"
            token_usage_history.appendleft(
                {
                    "time": datetime.datetime.now().strftime("%m-%d %H:%M:%S"),
                    "model": model_label,
                    "prompt": prompt_tokens,
                    "cache": cached_tokens,
                    "completion": completion_tokens,
                    "total": total_tokens,
                    "elapsed": elapsed,
                    "tools": tools,
                }
            )
            runtime = getattr(self, "agent_runtime", None)
            capture_usage = getattr(runtime, "capture_usage", None)
            if callable(capture_usage):
                capture_usage(
                    provider=model_info.get("provider", "unknown"),
                    model=model_label,
                    input_tokens=prompt_tokens,
                    output_tokens=completion_tokens,
                    reasoning_tokens=reasoning_tokens,
                    cached_tokens=cached_tokens,
                )
        except Exception:
            # Usage is accounting data from an untrusted response.  A malformed
            # provider payload must never alter the model response lifecycle.
            return

    def _extract_api_error_info(self, error_text: str) -> dict[str, str]:
        """从 API 错误响应中提取常见结构化字段，解析失败时返回空字典。"""
        info = {"code": "", "message": "", "type": "", "param": ""}

        def set_if_empty(key: str, value):
            if info[key] or value is None:
                return
            if isinstance(value, (str, int, float)):
                info[key] = str(value)

        try:
            err_json = json.loads(error_text)
        except Exception:
            return {}

        if not isinstance(err_json, dict):
            return {}

        candidates = []
        if isinstance(err_json.get("error"), dict):
            candidates.append(err_json["error"])
        candidates.append(err_json)

        for candidate in candidates:
            set_if_empty("code", candidate.get("code") or candidate.get("error_code"))
            set_if_empty("message", candidate.get("message") or candidate.get("msg"))
            set_if_empty("type", candidate.get("type"))
            set_if_empty("param", candidate.get("param"))

        return {key: value for key, value in info.items() if value}

    def _extract_api_error_detail(self, error_text: str) -> str:
        """从 API 错误响应中提取简短原因，解析失败时返回空字符串。"""
        return self._extract_api_error_info(error_text).get("message", "")

    def _payload_contains_image(self, value) -> bool:
        if isinstance(value, dict):
            if value.get("type") == "image_url" or "image_url" in value:
                return True
            return any(self._payload_contains_image(v) for v in value.values())
        if isinstance(value, list):
            return any(self._payload_contains_image(item) for item in value)
        return False

    @staticmethod
    def _is_sse_done(line: bytes) -> bool:
        payload = line.strip()
        if payload.startswith(b"data:"):
            payload = payload[5:].strip()
        return payload == b"[DONE]"

    @staticmethod
    def _decode_sse_payload(line: bytes) -> dict | None:
        """Return one JSON object without exposing malformed provider data."""

        payload = line.strip()
        if payload.startswith(b"data:"):
            payload = payload[5:].strip()
        if not payload or payload.startswith(b":"):
            return None
        # SSE control fields and JSON arrays/scalars are not chat chunks.  In
        # particular, rejecting arrays before ``.get`` prevents a malformed
        # provider response from escaping into the retry path.
        if not payload.startswith(b"{"):
            return None
        try:
            decoded = payload.decode("utf-8")
            value = json.loads(decoded)
        except (UnicodeError, ValueError):
            logger.debug(
                "忽略无法解析的 SSE 数据行: payload_bytes={} payload_sha256={}",
                len(payload),
                hashlib.sha256(payload).hexdigest()[:16],
            )
            return None
        if not isinstance(value, dict):
            logger.debug(
                "忽略非对象 SSE 数据行: payload_bytes={} payload_sha256={}",
                len(payload),
                hashlib.sha256(payload).hexdigest()[:16],
            )
            return None
        return value

    async def _check_400_error(self, response, request_data=None) -> str | None:
        """检查是否为400错误及敏感内容拦截，返回错误提示或None"""
        if response.status == 400:
            error_content = await response.text()
            logger.warning(
                f"API 请求返回 400，响应正文已省略，长度 {len(error_content)}"
            )
            # 1. 尝试解析 API 返回的具体报错信息
            error_info = self._extract_api_error_info(error_content)
            detail_msg = error_info.get("message", "")
            # Only exact normalized structured code/type values prove a
            # content-policy rejection.  Free-form messages mentioning words
            # such as "audit" or "safety" are ordinary provider errors.
            if is_content_policy_error_info(error_info):
                return "图片或内容可能包含敏感信息"
            if request_data is not None:
                logger.warning(
                    "LLM 请求返回 400；为保护群聊内容和密钥，已省略完整 Payload。"
                )
            else:
                logger.warning("LLM 请求失败；对话内容已从日志中省略")

            # 4. 组装更详细的聊天回复文本
            error_reply = "API请求被拒绝 (400)"
            if request_data is not None and self._payload_contains_image(request_data):
                error_reply += "\n提示：本次请求包含图片，具体原因请参考下方错误信息或后台日志。"
            if code := error_info.get("code"):
                error_reply += f"\n错误码：{code}"
            if error_type := error_info.get("type"):
                error_reply += f"\n类型：{error_type}"
            if param := error_info.get("param"):
                error_reply += f"\n参数：{param}"
            if detail_msg:
                # 截取前150个字符，防止大段长英文代码刷屏，影响群聊体验
                truncated_msg = (
                    detail_msg[:150] + "..." if len(detail_msg) > 150 else detail_msg
                )
                error_reply += f"\n原因：{truncated_msg}"
            else:
                error_reply += "，请检查后台日志。"

            return error_reply

        return None

    async def stream_llm_chat(
        self, session, url, headers, data, proxy, is_segment=False, timeout=None
    ) -> tuple[bool, str, list, str]:
        # 流式响应内容
        buffer = []
        assistant_result = []  # 后处理助手回复
        punctuation_buffer = ""  # 存标点
        is_second_send = False  # 不是第一次发送
        current_content = ""  # 最近一次已发出的分段内容
        reasoning_buffer = []  # 思考内容
        is_thinking = False  # 状态机：是否在思考中
        tag_buffer = ""  # 用于精准匹配标签切片

        async with session.post(
            url, headers=headers, json=data, proxy=proxy, timeout=timeout
        ) as resp:
            if error_msg := await self._check_400_error(resp, request_data=data):
                self._last_api_error_non_retryable = True
                return False, error_msg, None, ""
            # 确保响应是成功的
            if resp.status == 200:
                # 异步迭代响应内容
                MAX_SEGMENTS = self.model_info.get("max_segments", 5)
                current_segment = 0
                jump_out = False  # 判断是否跳出循环
                async for line in resp.content:
                    if not line or jump_out or self._is_sse_done(line):
                        break  # 结束标记，退出循环

                    json_data = self._decode_sse_payload(line)
                    if json_data is None:
                        continue
                    if usage := json_data.get("usage"):
                        self._record_token_usage(usage)
                    content = ""
                    # 以此尝试获取完整消息或流式增量
                    choices = json_data.get("choices", [{}])
                    if not choices:  # 防止choices为空列表的情况
                        continue
                    reasoning_delta = ""
                    if message := json_data.get("choices", [{}])[0].get("message", {}):
                        content = message.get("content", "")
                        reasoning_delta = message.get("reasoning_content", "")
                    elif message := json_data.get("choices", [{}])[0].get("delta", {}):
                        content = message.get("content", "")
                        reasoning_delta = message.get("reasoning_content", "")

                    if reasoning_delta:
                        reasoning_buffer.append(reasoning_delta)

                    if content:
                        for char in content:
                            tag_buffer += char
                            if len(tag_buffer) > 15:
                                tag_buffer = tag_buffer[-15:]

                            # 拦截思考开始
                            if not is_thinking and (
                                tag_buffer.endswith("<thought>")
                                or tag_buffer.endswith("<think>")
                            ):
                                is_thinking = True
                                # 思考标签的字符已经漏进 buffer 了，给它揪出来删掉
                                tag_len = 9 if tag_buffer.endswith("<thought>") else 7
                                for _ in range(
                                    tag_len - 1
                                ):  # -1是因为当前char还没加进去
                                    if punctuation_buffer:
                                        punctuation_buffer = punctuation_buffer[:-1]
                                    elif buffer:
                                        buffer.pop()
                                continue

                            # 拦截思考结束
                            if is_thinking and (
                                tag_buffer.endswith("</thought>")
                                or tag_buffer.endswith("</think>")
                            ):
                                is_thinking = False
                                continue

                            # 如果处于思考状态，直接跳过当前字符，不加入 buffer
                            if is_thinking:
                                continue

                            # 如果是换行且前面刚好结束思考，忽略这个孤儿换行
                            if char == "\n" and tag_buffer.endswith(">"):
                                continue
                            if is_segment and self.temperament != "ai助手":  # 分段
                                if char in ["。", "？", "！", "—", "\n"]:
                                    punctuation_buffer += char
                                else:
                                    if punctuation_buffer:
                                        # 发送累积的标点内容
                                        current_content = (
                                            "".join(buffer) + punctuation_buffer
                                        ).strip()
                                        if current_content.strip():
                                            if current_segment >= MAX_SEGMENTS:
                                                TOO_LANG = "太长了，不发了"
                                                buffer = [TOO_LANG]
                                                jump_out = True
                                                break
                                            if (
                                                is_second_send
                                            ):  # 第二次开始，会等几秒再发送
                                                await asyncio.sleep(
                                                    2 + len(current_content) / 3
                                                )
                                            else:
                                                is_second_send = True
                                            # 处理表情包和发送
                                            current_content = (
                                                await self.send_emotion_message(
                                                    current_content
                                                )
                                            )
                                            current_segment += 1
                                            assistant_result.append(current_content)
                                        buffer = []
                                        punctuation_buffer = ""
                                    buffer.append(char)
                            else:
                                # 注意这里是 append(char) 不是 append(content)
                                buffer.append(char)

                # 最后的的句子或者没分段
                if jump_out:
                    result = "".join(buffer)
                else:
                    result = "".join(buffer) + punctuation_buffer

                if is_second_send:
                    await asyncio.sleep(2 + len(current_content) / 3)
                else:
                    is_second_send = True

                if result := result.strip():
                    result = await self.send_emotion_message(result)
                    return (
                        True,
                        "".join(assistant_result) + result,
                        None,
                        "".join(reasoning_buffer),
                    )
                elif is_second_send:
                    return (
                        True,
                        "".join(assistant_result),
                        None,
                        "".join(reasoning_buffer),
                    )
            else:
                error_text = await resp.text()
                logger.warning(
                    f"API返回非200状态码 {resp.status}，响应正文已省略，长度 {len(error_text)}"
                )
                detail = self._extract_api_error_detail(error_text)
                error_reply = f"API请求异常 (HTTP {resp.status})"
                if detail:
                    error_reply += (
                        f"\n原因：{detail[:150]}{'...' if len(detail) > 150 else ''}"
                    )
                return False, error_reply, None, ""
        return False, "API请求异常", None, ""

    async def none_stream_llm_chat(
        self, session, url, headers, data, proxy, timeout=None
    ) -> tuple[bool, str, list, str]:
        async with session.post(
            url=url, json=data, headers=headers, proxy=proxy, timeout=timeout
        ) as resp:
            if error_msg := await self._check_400_error(resp, request_data=data):
                self._last_api_error_non_retryable = True
                return False, error_msg, None, ""
            if resp.status != 200:
                error_text = await resp.text()
                logger.warning(
                    f"API返回非200状态码 {resp.status}，响应正文已省略，长度 {len(error_text)}"
                )
                detail = self._extract_api_error_detail(error_text)
                error_reply = f"API返回异常 (HTTP {resp.status})"
                if detail:
                    error_reply += (
                        f"\n原因：{detail[:150]}{'...' if len(detail) > 150 else ''}"
                    )
                return False, error_reply, None, ""
            response = await resp.json()
            if not response:
                logger.warning("API返回空响应")
                return False, "API返回异常：响应体为空", None, ""

        if choices := response.get("choices"):
            if usage := response.get("usage"):
                self._record_token_usage(usage)
            message = choices[0]["message"]
            # 获取并返回 reasoning_content
            reasoning_content = message.get("reasoning_content", "")
            content = message.get("content", "")
            # 清理思考内容
            if content:
                content = re.sub(
                    r"<(think|thought)>.*?</\1>", "", content, flags=re.DOTALL
                )
                content = content.strip()

            # 清洗完再判断并返回 tool_calls
            if tool_calls := message.get("tool_calls"):
                return True, content, tool_calls, reasoning_content

            return True, content, None, reasoning_content
        else:
            logger.warning("API 返回内容安全拦截，响应正文已省略")
            return False, "API解析异常", None, ""

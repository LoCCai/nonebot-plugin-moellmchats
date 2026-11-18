"""Bounded Chat Completions SSE assembly, shared by chat and internal readers."""

from __future__ import annotations

import json

import aiohttp

MAX_BYTES = 16 * 1024 * 1024


class StreamIncomplete(aiohttp.ClientPayloadError):
    """No complete, executable response was received. Never include payloads."""


def configure_stream(payload: dict, model: dict) -> bool:
    enabled = bool(model.get("stream", True))
    payload["stream"] = enabled
    if enabled:
        payload["stream_options"] = {"include_usage": True}
    else:
        payload.pop("stream_options", None)
    return enabled


async def events(response):
    # aiohttp's async iterator yields complete lines even across TCP/UTF-8 splits.
    # Multiple data lines make one SSE event; comments and controls carry no JSON.
    pending = []
    total = 0
    async for raw in response.content:
        total += len(raw)
        if total > MAX_BYTES:
            raise StreamIncomplete("stream_size_limit")
        line = raw.rstrip(b"\r\n")
        if not line:
            if pending:
                yield b"\n".join(pending)
                pending = []
        elif line.startswith(b"data:"):
            pending.append(line[5:].lstrip(b" "))
        elif line.startswith((b"{", b"[DONE]")):
            # A few compatible gateways emit bare JSON lines.
            if pending:
                yield b"\n".join(pending)
                pending = []
            yield line
    if pending:
        yield b"\n".join(pending)


class Completion:
    def __init__(self):
        self.content = []
        self.reasoning = []
        self.tools = {}
        self.usage = None
        self.finish_reason = None
        self.done = False
        self.size = 0
        self.model = None

    def feed(self, raw: bytes) -> str:
        self.size += len(raw)
        if self.size > MAX_BYTES:
            raise StreamIncomplete("stream_size_limit")
        if raw.strip() == b"[DONE]":
            self.done = True
            return ""
        try:
            chunk = json.loads(raw)
        except (ValueError, UnicodeError):
            raise StreamIncomplete("invalid_stream_json") from None
        if not isinstance(chunk, dict) or chunk.get("error"):
            raise StreamIncomplete("invalid_stream_event")
        if isinstance(chunk.get("usage"), dict):
            self.usage = chunk["usage"]  # cumulative, never sum usage chunks
        if isinstance(chunk.get("model"), str):
            self.model = chunk["model"]
        choices = chunk.get("choices") or []
        if not isinstance(choices, list):
            raise StreamIncomplete("invalid_stream_choices")
        selected = next((c for c in choices if isinstance(c, dict) and c.get("index", 0) == 0), None)
        if selected is None:
            return ""
        message = selected.get("delta", selected.get("message")) or {}
        if not isinstance(message, dict):
            raise StreamIncomplete("invalid_stream_delta")
        text = message.get("content") or ""
        reasoning = message.get("reasoning_content") or ""
        if not isinstance(text, str) or not isinstance(reasoning, str):
            raise StreamIncomplete("invalid_stream_text")
        if self.finish_reason and (text or reasoning or message.get("tool_calls")):
            raise StreamIncomplete("stream_delta_after_finish")
        self.content.append(text)
        self.reasoning.append(reasoning)
        calls = message.get("tool_calls") or []
        if not isinstance(calls, list):
            raise StreamIncomplete("invalid_stream_tools")
        for position, call in enumerate(calls):
            if not isinstance(call, dict):
                raise StreamIncomplete("invalid_stream_tool")
            index = call.get("index", position if "message" in selected else None)
            if type(index) is not int or not 0 <= index < 64:
                raise StreamIncomplete("invalid_tool_index")
            tool = self.tools.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
            if call.get("type", "function") != "function":
                raise StreamIncomplete("invalid_tool_type")
            identifier = call.get("id")
            if identifier:
                if not isinstance(identifier, str) or (tool["id"] and tool["id"] != identifier):
                    raise StreamIncomplete("conflicting_tool_id")
                tool["id"] = identifier
            function = call.get("function") or {}
            if not isinstance(function, dict):
                raise StreamIncomplete("invalid_tool_function")
            for field in ("name", "arguments"):
                part = function.get(field) or ""
                if not isinstance(part, str):
                    raise StreamIncomplete("invalid_tool_fragment")
                if field == "name" and part == tool["function"]["name"]:
                    continue
                tool["function"][field] += part
        reason = selected.get("finish_reason")
        if reason:
            if self.finish_reason and self.finish_reason != reason:
                raise StreamIncomplete("conflicting_finish_reason")
            self.finish_reason = reason
        return text

    def result(self) -> dict:
        if not self.done and not self.finish_reason:
            raise StreamIncomplete("stream_ended_without_finish")
        calls = [self.tools[i] for i in sorted(self.tools)]
        if calls:
            if self.finish_reason not in ("tool_calls", "stop"):
                raise StreamIncomplete("unfinished_tool_calls")
            seen = set()
            for call in calls:
                if not call["id"] or call["id"] in seen or not call["function"]["name"]:
                    raise StreamIncomplete("incomplete_tool_identity")
                seen.add(call["id"])
                try:
                    args = json.loads(call["function"]["arguments"])
                except ValueError:
                    raise StreamIncomplete("unfinished_tool_arguments") from None
                if not isinstance(args, dict):
                    raise StreamIncomplete("invalid_tool_arguments")
        elif self.finish_reason == "tool_calls":
            raise StreamIncomplete("missing_tool_calls")
        message = {"role": "assistant", "content": "".join(self.content), "reasoning_content": "".join(self.reasoning)}
        if calls:
            message["tool_calls"] = calls
        return {
            "model": self.model,
            "choices": [{"index": 0, "finish_reason": self.finish_reason, "message": message}],
            "usage": self.usage,
        }


async def read_completion(response) -> dict:
    """Internal classification/authoring/media callers consume a complete value."""
    content_type = (getattr(response, "headers", {}) or {}).get("Content-Type", "").lower()
    if "text/event-stream" not in content_type:
        # Explicit JSON is valid for providers which ignore stream=true.
        if hasattr(response, "json"):
            return await response.json()
        return json.loads(await response.text())
    completion = Completion()
    async for raw in events(response):
        completion.feed(raw)
        if completion.done:
            break
    return completion.result()

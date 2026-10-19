from copy import deepcopy
import json

import pytest
from test_moe_llm import _empty_reply_chat

from nonebot_plugin_moellmchats import moe_llm
from nonebot_plugin_moellmchats.math_response import solution_first_tools

TOOLS = [{"type":"function", "function":{"name":name}} for name in ["render_markdown", "plot_implicit", "send_message"]]


@pytest.mark.parametrize(("plain", "tools", "expected"), [
    ("已知椭圆，结合画图证明并求最大面积", TOOLS, True),
    ("只画 sin(x)", TOOLS, False),
    ("证明椭圆结论", TOOLS[1:], False),
    ("查玩家资料", TOOLS, False),
])
def test_only_solved_math_with_available_renderer_is_staged(plain, tools, expected):
    assert bool(solution_first_tools(tools, plain)) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("renderer", ["success", "unavailable", "uncertain"])
async def test_answer_delivered_before_any_plot_request(monkeypatch, renderer):
    solution = "## 证明\n由 $OA\\perp OB$，得定值。\n\n$$d=6/\\sqrt{13}$$"
    chat, entity = _empty_reply_chat(monkeypatch, [])
    timeline = []
    requests = []
    entity.tool_messages = []
    chat.messages_handler = None

    def build(history):
        return {"messages":history, "tools":deepcopy(TOOLS), "stream":True}, True

    chat._build_payload = build
    # The fixture's preprocessor uses a geometry/plot prompt; make it a solving
    # task without depending on classification/network behavior.
    original = moe_llm.MessagesHandler
    def handler(user):
        value = original(user)
        value.pre_process = lambda *args, **kwargs: "已知椭圆，请结合画图证明距离并求最大面积"
        return value
    monkeypatch.setattr(moe_llm, "MessagesHandler", handler)

    async def request(session, headers, data, stream):
        requests.append(deepcopy(data))
        if len(requests) == 1:
            assert not stream
            assert "tools" not in data
            timeline.append("solution_model")
            return True, solution, None, "", False
        assert "rendered" in timeline or "text_fallback" in timeline
        assert [t["function"]["name"] for t in data["tools"]] == ["plot_implicit"]
        timeline.append("plot_model")
        return True, "图示说明", None, "", False

    async def execute(calls, text, history, reasoning):
        assert calls[0]["function"]["name"] == "render_markdown"
        assert json.loads(calls[0]["function"]["arguments"])["markdown"] == solution
        timeline.append("render_attempt")
        outcome = {"success":"已完成渲染并把图片发送到当前会话", "unavailable":"渲染工具未启用",
                   "uncertain":"图片已生成但发送失败"}[renderer]
        if renderer == "success":
            timeline.append("rendered")
        return [*history, {"role":"assistant", "content":text, "tool_calls":calls},
                {"role":"tool", "content":outcome, "tool_call_id":calls[0]["id"]}]

    async def send(text):
        timeline.append("text_fallback" if text == solution else "final_text")
        return text

    chat._request_with_rate_limit_fallback = request
    chat._execute_tools = execute
    chat.send_emotion_message = send
    result = await chat.get_llm_chat()
    if renderer == "uncertain":
        assert "未确认" in result
        assert len(requests) == 1
        assert "text_fallback" not in timeline
    else:
        assert result is True
        assert timeline.index("plot_model") > timeline.index("render_attempt")
        assert ("text_fallback" in timeline) is (renderer == "unavailable")

from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_model_failover import _pair

from nonebot_plugin_moellmchats import moe_llm
from nonebot_plugin_moellmchats.llm_api import LlmApiMixin
from nonebot_plugin_moellmchats.moe_llm import MoeLlm


@pytest.mark.asyncio
@pytest.mark.parametrize(("code", "kind", "message", "expected"), [
    ("400001", "invalid_request_error", "rejected by an internal MaaS component", True),
    ("400001", "invalid_request_error", "other invalid field", False),
    ("content_filter", "invalid_request_error", "internal MaaS component", False),
    ("other", "invalid_request_error", "internal MaaS component", False),
])
async def test_only_specific_provider_rejection_can_fail_over(code, kind, message, expected):
    api = LlmApiMixin()
    response = SimpleNamespace(status=400,text=AsyncMock(return_value=json.dumps(
        {"error":{"code":code,"type":kind,"message":message}})))
    await api._check_400_error(response,{"messages":[{"type":"image_url"}]})
    assert bool(getattr(api,"_last_api_provider_rejected",False)) is expected


@pytest.mark.asyncio
async def test_image_format_rejection_preserves_images_tools_and_quota(tmp_path,monkeypatch):
    selector = _pair(tmp_path,monkeypatch)
    for model in selector.models.values():
        model["is_vision"] = True
    monkeypatch.setattr(moe_llm.model_selector,"get_rate_limit_fallback",selector.get_rate_limit_fallback)
    chat = object.__new__(MoeLlm)
    chat.agent_runtime, chat.model_info = None, selector.get_model("category_model")
    calls = []
    async def model(session,url,headers,data,*args):
        calls.append(deepcopy(data))
        chat._last_api_http_status = 400 if len(calls)==1 else 200
        chat._last_api_provider_rejected = len(calls)==1
        return len(calls)>1,"answer",None,""
    chat.none_stream_llm_chat = model
    history = [{"role":"user","content":[{"type":"image_url","image_url":{"url":"data:image/png;base64,AA=="}}]},
               {"role":"tool","content":"already completed","tool_call_id":"done"}]
    tools = [{"type":"function","function":{"name":"query"}}]
    result = await chat._request_with_rate_limit_fallback(object(),{}, {"messages":history,"tools":tools},False)
    assert result[0]
    assert len(calls)==2
    assert calls[1]["messages"] == history
    assert calls[1]["tools"] == tools
    assert not selector.is_model_exhausted("model-a (good)")

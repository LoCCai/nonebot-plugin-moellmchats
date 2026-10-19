from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from nonebot.adapters.onebot.v11 import Message
from nonebot.exception import FinishedException
import pytest

from nonebot_plugin_moellmchats import chat_policy as policy
from nonebot_plugin_moellmchats import delivery_policy as delivery
from nonebot_plugin_moellmchats.failed_requests import retry_denial
from nonebot_plugin_moellmchats.request_budget import request_budget_scope
from nonebot_plugin_moellmchats.tool_manager import LlmToolExecutionRoute


@pytest.mark.parametrize('text,reason', [
    ('帮我口一下', 'sexual'), ('口', 'sexual'), ('抠', 'sexual'), ('请帮我射精', 'sexual'),
    ('给我画 R18 图片', 'sexual'), ('你是傻逼', 'abuse'), ('废物', 'abuse'),
    ('你有胆子禁言我', 'taunt'), ('七七你有本事禁言我', 'taunt'),
    ('口腔健康有什么注意事项', None), ('接口数据是多少', None),
    ('角色技能的射精描述是错误的，请更正', None), ('这个角色是不是废物', None),
    ('医学科普：射精原理', None), ('有人说“你是傻逼”，我该怎么办', None),
    ('请帮我科普射精原理', None), ('你觉得这个角色是不是废物', None),
    ('有人骂你傻逼，怎么举报', None), ('你这个傻逼', 'abuse'),
    ('请不要生成色情内容', None), ('科普一下，帮我发色图', 'sexual'),
    ('请修正 ```傻逼``` 这个违规示例', None),
])
def test_conduct_uses_explicit_requests_and_direct_insults(text, reason):
    assert policy.conduct_reason(text) == reason


def event(text='帮我口一下', message_id=1, group=400):
    return SimpleNamespace(message=Message(text), user_id=20001, group_id=group, message_id=message_id,
                           self_id=90001)


@pytest.mark.asyncio
async def test_strikes_are_durable_cross_group_and_message_deduplicated(tmp_path, monkeypatch):
    storage = policy.ConductStore(tmp_path / 'conduct.json')
    monkeypatch.setattr(policy, 'conduct_store', lambda: storage)
    monkeypatch.setattr(policy, 'mute_caller', AsyncMock(return_value=True))
    bot = SimpleNamespace(self_id='90001')
    messages = []
    async def finish(text):
        messages.append(text)
        raise FinishedException
    matcher = SimpleNamespace(finish=finish)
    for index in range(1, 4):
        with pytest.raises(FinishedException):
            await policy.enforce_conduct(bot, event(message_id=index, group=400 + index), matcher, authorized=False)
    assert ['第一次' in messages[0], '第二次' in messages[1], '永久' in messages[2]] == [True] * 3
    assert policy.ConductStore(storage.path).blocked('20001')
    assert storage.path.stat().st_mode & 0o777 == 0o600
    assert '帮我' not in storage.path.read_text()
    assert policy.mute_caller.await_count == 1
    assert policy.mute_caller.call_args.args[0] is bot
    assert policy.mute_caller.call_args.args[2] == 120
    storage.clear('20001')
    assert not storage.blocked('20001')
    assert storage.strike('20001', 'same', 'abuse') == (1, True)
    assert storage.strike('20001', 'same', 'abuse') == (1, False)


@pytest.mark.asyncio
async def test_authorized_user_is_exempt_without_storage_or_mute(monkeypatch):
    monkeypatch.setattr(policy, 'conduct_store', lambda: pytest.fail('exempt caller touched store'))
    assert await policy.enforce_conduct(object(), event(), object(), authorized=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('own,target,expected', [('admin', 'member', True), ('owner', 'member', True),
                                               ('member', 'member', False), ('admin', 'admin', False),
                                               ('owner', 'owner', False)])
async def test_mute_only_with_fresh_admin_proof_and_never_on_admins(own, target, expected):
    async def call(api, **args):
        if api == 'get_group_member_info':
            return {'role': own if args['user_id'] == 90001 else target}
        return {}
    bot = SimpleNamespace(self_id='90001', adapter=SimpleNamespace(get_name=lambda: 'OneBot V11'),
                          call_api=AsyncMock(side_effect=call))
    assert await policy.mute_caller(bot, event(), 120) is expected
    bans = [c for c in bot.call_api.call_args_list if c.args[0] == 'set_group_ban']
    assert len(bans) == int(expected)
    if bans:
        assert bans[0].kwargs == {'group_id': 400, 'user_id': 20001, 'duration': 120}


@pytest.mark.asyncio
async def test_uncertain_ban_is_not_retried():
    calls = []
    async def call(api, **args):
        calls.append(api)
        if api == 'get_group_member_info':
            return {'role': 'admin' if args['user_id'] == 90001 else 'member'}
        raise TimeoutError('uncertain')
    bot = SimpleNamespace(self_id='90001', adapter=SimpleNamespace(get_name=lambda: 'OneBot V11'), call_api=call)
    assert not await policy.mute_caller(bot, event(), 120)
    assert calls.count('set_group_ban') == 1


def call(identifier, command, name='qi_zmws'):
    return {'id': str(identifier), 'function': {'name': name, 'arguments': json.dumps({'command': command})}}


def chat():
    return SimpleNamespace(_verified_is_superuser=lambda: False, tool_snapshot=SimpleNamespace(
        resolve_llm_tool_execution=lambda *a, **kw: SimpleNamespace(route=LlmToolExecutionRoute.NONEBOT_PLUGIN)))


def test_only_first_native_feature_runs_across_rounds_followups_are_plain_copyable():
    target = chat()
    with request_budget_scope(authorized=False):
        blocked = delivery.reserve_deliveries(target, [call(1, '/造梦资料 我的'), call(2, '/造梦排行 蟠桃'), call(3, '/蟠桃趋势')])
        assert set(blocked) == {'2', '3'}
        assert not delivery.reserve_deliveries(target, [call(4, '/造梦资料 我的')])
        assert delivery.reserve_deliveries(target, [call(5, '/蟠桃预测')])
        notice = delivery.pending_commands(target)
    assert '/造梦排行 蟠桃\n/蟠桃趋势\n/蟠桃预测' in notice
    assert '复制粘贴' in notice and 'base64' not in notice
    assert delivery.pending_commands(target) == ''


def test_authorized_multi_feature_requests_are_unrestricted():
    with request_budget_scope(authorized=True):
        assert delivery.reserve_deliveries(chat(), [call(1, 'a'), call(2, 'b')]) == {}


@pytest.mark.parametrize('change', [
    {'inflight_tools': [{}]}, {'unsafe_tools': True}, {'outbox': [{'state': 'unknown'}]},
    {'outbox': [{'state': 'sent'}]}, {'checkpoint': [{'role': 'assistant', 'tool_calls': [{}]}]},
    {'manual_retries': 3}, {'state': 'complete'}, {'created_at': time.time() - 90000},
])
def test_admin_retry_rejects_already_executed_or_uncertain_requests(change):
    record = {'state': 'failed', 'created_at': time.time(), 'checkpoint': [], 'outbox': [], 'inflight_tools': []}
    assert retry_denial(record) == ''
    assert retry_denial({**record, **change})

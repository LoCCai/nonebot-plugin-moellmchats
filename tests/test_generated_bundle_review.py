"""fix/generated-bundles-review 判例回归测试。

每项对应 2026-09 审查中通过复现或代码核实确认的问题：
1. 进度话术脱敏正则在空格处截断，泄露 Bearer 凭证体
2. 进度状态键在 tool_call 缺 id 时互相覆盖
3. api_read_recovered 把全部只读失败按任意已验证效果洗白
4. 安全 HTTP chunk size 接受非规范十六进制形式
5. 取消清理未 settle，二次取消泄漏冷却租约
6. 改写配置的超管命令 handler 必须带合成事件守卫
7. f3bb850 合并后：tool_call_id 原样保留 / 拒绝记账 / junction / AST 集合
8. 分类缓存 clear 与在飞发布的竞态由 clear-epoch 闭合
"""

from __future__ import annotations

import asyncio
import inspect
import uuid

import pytest
from nonebot.adapters.onebot.v11.exception import ActionFailed

import nonebot_plugin_moellmchats.chat_runtime as chat_runtime
import nonebot_plugin_moellmchats.event_simulator as simulator_module
from nonebot_plugin_moellmchats.agent_context_runtime import RuntimeResourceHost
from nonebot_plugin_moellmchats.agent_runtime import AgentRunState
from nonebot_plugin_moellmchats.cooldowns import CooldownClaim, CooldownLease
from nonebot_plugin_moellmchats.llm_tools import LlmToolsMixin
from nonebot_plugin_moellmchats.network_safety import SafeHttpError, _read_chunked_body

from test_chat_runtime import (
    FakeMatcher,
    _config,
    _publish_snapshot,
    _runtime_event,
)


# ---------- 1. 脱敏正则 ----------

@pytest.mark.parametrize(
    "raw",
    [
        "Authorization=Bearer secret123",
        "Authorization: Bearer abc987xyz",
        "token=Bearer sk-live-9f3k2",
    ],
)
def test_progress_preface_redacts_bearer_credentials(raw: str) -> None:
    sanitized = LlmToolsMixin._safe_progress_preface(raw)
    assert "secret123" not in sanitized
    assert "abc987xyz" not in sanitized
    assert "sk-live-9f3k2" not in sanitized
    assert "<redacted>" in sanitized


def test_progress_preface_keeps_plain_text_usable() -> None:
    sanitized = LlmToolsMixin._safe_progress_preface("正在搜索今天的天气并汇总")
    assert sanitized == "正在搜索今天的天气并汇总"


# ---------- 2. 进度状态键 ----------

def test_tool_progress_status_keys_distinct_without_call_id() -> None:
    holder = object().__new__(LlmToolsMixin)
    first = {"function": {"name": "web_search"}, "arguments": "{}"}
    second = {"function": {"name": "web_search"}, "arguments": "{}"}
    assert not (first.get("id") or second.get("id"))

    holder._set_tool_progress_status(first, "sent")
    holder._set_tool_progress_status(second, "timed_out")

    assert holder._tool_progress_status(first) == "sent"
    assert holder._tool_progress_status(second) == "timed_out"


def test_tool_progress_status_key_uses_id_when_present() -> None:
    holder = object().__new__(LlmToolsMixin)
    call = {"id": "call_1", "function": {"name": "web_search"}}
    holder._set_tool_progress_status(call, "failed")
    assert holder._tool_progress_status(call) == "failed"
    assert LlmToolsMixin._tool_progress_status_key(call) == "call_1"


# ---------- 3. api_read_recovered 派生 ----------

def test_read_failures_are_not_recovered_by_unrelated_verified_effect() -> None:
    # 变更型 API 成功 + 只读查询失败且从未重试成功：
    # 修复前会被全额记为 api_read_recovered
    context = simulator_module._empty_dispatch_context()
    context.update(
        {
            "matcher_matched": 1,
            "api_failed": 1,
            "api_read_failed": 1,
            "mutating_api_succeeded": 1,
        }
    )
    result = simulator_module._dispatch_result(context, started_monotonic=0)
    assert result.api_read_recovered == 0
    assert result.api_read_failed == 1


@pytest.mark.asyncio
async def test_read_failure_recovered_only_by_later_read_success() -> None:
    capture_id = uuid.uuid4().hex
    context = simulator_module._empty_dispatch_context()
    context.update({"original_id": 10, "fake_id": 20})
    simulator_module._captures[capture_id] = context
    token = simulator_module._capture_key.set(capture_id)
    failed_read: dict = {}
    fallback_read: dict = {}
    try:
        await simulator_module._capture_outgoing_api(
            object(), "get_group_member_info", failed_read
        )
        await simulator_module._confirm_outgoing_api(
            object(), ActionFailed("OneBot V11", "failed"),
            "get_group_member_info", failed_read, None,
        )
        await simulator_module._capture_outgoing_api(
            object(), "get_stranger_info", fallback_read
        )
        await simulator_module._confirm_outgoing_api(
            object(), None, "get_stranger_info", fallback_read,
            {"nickname": "fallback"},
        )
        result = simulator_module._dispatch_result(context, started_monotonic=0)
    finally:
        simulator_module._capture_key.reset(token)
        simulator_module._captures.pop(capture_id, None)

    assert result.api_read_failed == 1
    assert result.api_read_recovered == 1


# ---------- 4. chunk size 严格解析 ----------

@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", [b"+5", b" 5", b"1_0", b""])
async def test_chunk_size_rejects_non_canonical_hex(prefix: bytes) -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(prefix + b"\r\n")
    reader.feed_eof()
    deadline = asyncio.get_running_loop().time() + 1
    with pytest.raises(SafeHttpError, match="chunk size"):
        await _read_chunked_body(reader, deadline=deadline)


@pytest.mark.asyncio
async def test_chunk_size_accepts_canonical_hex() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(b"4\r\nWiki\r\n0\r\n\r\n")
    reader.feed_eof()
    deadline = asyncio.get_running_loop().time() + 1
    body = await _read_chunked_body(reader, deadline=deadline)
    assert body == b"Wiki"


# ---------- 5. 二次取消仍释放冷却 ----------


class SlowReleaseCooldownStore:
    def __init__(self, events: list[tuple[str, bool]]) -> None:
        self.events = events
        self.release_started = asyncio.Event()
        self.lease = CooldownLease(user_id=42, token="b" * 32, claimed_at=1_000.0)

    async def claim(self, *, user_id, event_time, cooldown_seconds):
        del user_id, event_time, cooldown_seconds
        self.events.append(("claim", True))
        return CooldownClaim(lease=self.lease, retry_after_seconds=0)

    async def release(self, lease):
        assert lease is self.lease
        self.events.append(("release.begin", True))
        self.release_started.set()
        await asyncio.sleep(0.05)
        self.events.append(("release.done", True))
        return True


@pytest.mark.asyncio
async def test_double_cancellation_still_releases_cooldown_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat_runtime.reset_all_runtime_state()
    _config(monkeypatch, cooldown=120, timeout=30)
    _publish_snapshot()
    host = RuntimeResourceHost()
    entered = asyncio.Event()
    captured = []
    events: list[tuple[str, bool]] = []

    class Chat:
        def __init__(self, _bot, _event, _message, *, temperament, agent_runtime):
            del temperament
            captured.append(agent_runtime)

        async def get_llm_chat(self) -> bool:
            runtime = captured[-1]
            await runtime.advance(AgentRunState.PLANNING, model="model")
            await runtime.advance(AgentRunState.EXECUTING)
            entered.set()
            await asyncio.Event().wait()
            return True

    monkeypatch.setattr(chat_runtime.llm, "MoeLlm", Chat)
    monkeypatch.setattr(
        chat_runtime.temperament_manager,
        "get_temperament",
        lambda _user_id: "测试性格",
    )
    store = SlowReleaseCooldownStore(events)
    matcher = FakeMatcher()
    task = asyncio.create_task(
        chat_runtime.handle_llm(
            object(),
            _runtime_event(),
            matcher,
            {"text": ["hello"]},
            cooldown_store=store,
            resource_host=host,
        )
    )
    await entered.wait()
    task.cancel()  # 管理员终止
    await store.release_started.wait()
    task.cancel()  # 释放进行中的叠加取消（超时/级联）
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await host.close()

    # 修复前：二次取消中断 release，租约泄漏直到 TTL 兜底
    assert ("release.done", True) in events
    assert chat_runtime.cd[42] == 0


# ---------- 6. 合成事件守卫存在性（静态核对） ----------

def test_superuser_config_handlers_reject_synthetic_events() -> None:
    import nonebot_plugin_moellmchats as plugin_root

    source = inspect.getsource(plugin_root)
    for command in (
        "@set_tool_progress_matcher.handle()",
        "@set_tool_progress_model_preface_matcher.handle()",
        "@set_private_chat_matcher.handle()",
        "@set_llm_cooldown_matcher.handle()",
    ):
        start = source.index(command)
        block = source[start : start + 400]
        assert "is_synthetic_event" in block, f"{command} 缺少合成事件守卫"


# ---------- 7. f3bb850 合并后的判例（tool_call_id 原样 / 拒绝记账 / junction） ----------

@pytest.mark.asyncio
async def test_schema_reject_preserves_raw_call_id_and_counts_usage() -> None:
    from test_llm_tools import Harness, _agent_request_runtime, _call

    harness = Harness({})
    harness.agent_runtime = await _agent_request_runtime()
    harness._active_llm_tool_names = frozenset()  # 任何名字都越界
    messages = await harness._execute_tools(
        [_call(7, "ghost_tool", '{"a": 1}')], "", [], ""
    )

    # tool_call_id 必须与其余 tool 消息写入一致：原样保留（不做 str 强转）
    assert messages[-1]["tool_call_id"] == "7"
    assert "不在本轮模型实际收到的工具 Schema" in messages[-1]["content"]
    # 越界幻觉调用同样计入指纹用量与按名计数，防止绕开重复调用限额
    digest = harness._canonical_arguments_digest({"a": 1})
    assert harness._tool_fingerprint_usage()[(1, "ghost_tool", digest)] == 1
    assert harness._current_tool_usage["ghost_tool"] == 1


def test_emotion_directory_link_helper_on_plain_directory(tmp_path) -> None:
    from nonebot_plugin_moellmchats.utils import _emotion_directory_is_link

    plain = tmp_path / "group"
    plain.mkdir()
    assert _emotion_directory_is_link(plain) is False
    # 无法 stat 的路径按链接处理（fail-closed）
    assert _emotion_directory_is_link(tmp_path / "missing") is True


def test_ast_policy_treats_safe_public_get_as_network_capability() -> None:
    from nonebot_plugin_moellmchats.ast_policy import _SAFE_HTTP_CALLS

    assert "safe_public_get" in _SAFE_HTTP_CALLS
    assert "safe_request" in _SAFE_HTTP_CALLS


# ---------- 8. 分类缓存 clear 与在飞发布的竞态 ----------


@pytest.mark.asyncio
async def test_classification_clear_blocks_inflight_publish() -> None:
    from test_classification_cache import _context, _record, _settings

    from nonebot_plugin_moellmchats.classification_cache import (
        MemoryClassificationCache,
    )

    cache = MemoryClassificationCache(settings=_settings())
    record = _record(context=_context(prompt="inflight"))
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_builder():
        started.set()
        await release.wait()
        return record

    task = asyncio.create_task(cache.resolve_exact(record.key, slow_builder))
    await started.wait()
    # builder 在飞期间 clear：代次推进，发布必须被前置拦截
    await cache.clear()
    release.set()

    settled = await task
    assert settled.key == record.key
    assert await cache.lookup(record.key) is None


@pytest.mark.asyncio
async def test_classification_discards_own_publish_that_landed_after_clear() -> None:
    from test_classification_cache import _context, _record, _settings

    from nonebot_plugin_moellmchats.classification_cache import (
        MemoryClassificationCache,
    )

    cache = MemoryClassificationCache(settings=_settings())
    record = _record(context=_context(prompt="raced"))

    # 模拟"前置检查通过后、publish 恰落在 clear 之后"的窗口：
    # 发布发生在新代次，构建方发布后核验应撤回自己的回写
    published = await cache.publish(record)
    cache._clear_epoch += 1
    await cache._discard_if_own_publish(record.key, published)
    assert await cache.lookup(record.key) is None

    # 对照：当前代次内发布的记录不被误删
    kept = _record(context=_context(prompt="kept"))
    await cache.publish(kept)
    await cache._discard_if_own_publish(kept.key, kept)
    assert await cache.lookup(kept.key) is kept

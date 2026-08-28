from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

from nonebot.adapters.onebot.v11 import Message
import pytest

from nonebot_plugin_moellmchats import protocol_broker as module
from nonebot_plugin_moellmchats.protocol_broker import (
    ProtocolBroker,
    ProtocolExecutionError,
    ProtocolInvocation,
    ProtocolInvocationStatus,
    ProtocolPendingActionStore,
    ProtocolRateLimiter,
    sanitize_protocol_result,
)
from nonebot_plugin_moellmchats.protocol_context import protocol_request_scope


class _Adapter:
    def __init__(self, name: str) -> None:
        self.name = name

    def get_name(self) -> str:
        return self.name


class _Bot:
    def __init__(
        self,
        *,
        protocol: str = "OneBot V11",
        self_id: str = "bot-1",
        app_name: str = "go-cqhttp",
        supported_actions: list[str] | None = None,
    ) -> None:
        self.adapter = _Adapter(protocol)
        self.self_id = self_id
        self.app_name = app_name
        self.supported_actions = list(supported_actions or [])
        self.impl = "fake-v12"
        self.version = "12.0"
        self.calls: list[tuple[str, dict]] = []
        self.outcomes: dict[str, object] = {}

    async def call_api(self, api: str, **data):
        self.calls.append((api, data))
        if api == "get_version_info":
            return {
                "app_name": self.app_name,
                "app_version": "4.18.19",
                "protocol_version": "11",
            }
        if api == "get_supported_actions":
            return list(self.supported_actions)
        outcome = self.outcomes.get(api, {"ok": True, "action": api})
        if callable(outcome):
            outcome = outcome(api, data)
            if asyncio.iscoroutine(outcome):
                outcome = await outcome
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _event(
    *,
    user_id: int | str = 123,
    group_id: int | str | None = 456,
    message_id: int | str = 789,
    reply_message_id: int | str | None = 321,
):
    values = {
        "time": 1_725_000_000,
        "user_id": user_id,
        "message_id": message_id,
        "message": Message("测试"),
        "sender": SimpleNamespace(
            user_id=user_id,
            card="测试用户",
            nickname="测试用户",
        ),
        "reply": (None if reply_message_id is None else SimpleNamespace(message_id=reply_message_id)),
    }
    if group_id is not None:
        values["group_id"] = group_id
    return SimpleNamespace(**values)


@pytest.fixture
def protocol_config(monkeypatch: pytest.MonkeyPatch):
    original = module.config_parser.get_config
    values = {
        "protocol_tools_enabled": True,
        "protocol_tools_napcat_extensions_enabled": True,
        "protocol_tools_low_risk_direct_enabled": True,
        "pending_action_ttl_seconds": 120,
        "pending_action_max_entries": 256,
        "tool_timeout_seconds": 1,
    }
    monkeypatch.setattr(
        module.config_parser,
        "get_config",
        lambda key, default=None: values.get(key, original(key, default)),
    )
    return values


def _broker(*, clock=None) -> ProtocolBroker:
    pending = ProtocolPendingActionStore() if clock is None else ProtocolPendingActionStore(clock=clock)
    limiter = ProtocolRateLimiter() if clock is None else ProtocolRateLimiter(clock=clock)
    return ProtocolBroker(pending=pending, limiter=limiter)


@pytest.mark.asyncio
async def test_like_wrapper_injects_current_actor_and_calls_send_like_once(
    protocol_config,
) -> None:
    bot = _Bot()
    event = _event(user_id=123)
    broker = _broker()

    async with protocol_request_scope(
        bot,
        event,
        generation=1,
        is_superuser=False,
    ):
        with pytest.raises(ProtocolExecutionError, match="参数错误"):
            await broker.invoke(
                "qq__like_me",
                {"times": 1, "user_id": 999},
            )
        invocation = await broker.invoke("qq__like_me", {"times": 2})

    assert invocation.status is ProtocolInvocationStatus.COMPLETED
    assert bot.calls == [
        ("get_version_info", {}),
        ("send_like", {"user_id": 123, "times": 2}),
    ]
    assert len(broker.audits()) == 1
    assert broker.audits()[0].action == "send_like"


@pytest.mark.asyncio
async def test_current_group_and_user_read_targets_cannot_be_overridden(
    protocol_config,
) -> None:
    bot = _Bot()
    event = _event(user_id=123, group_id=456)
    broker = _broker()

    async with protocol_request_scope(
        bot,
        event,
        generation=2,
        is_superuser=False,
    ):
        with pytest.raises(ProtocolExecutionError, match="参数错误"):
            await broker.invoke(
                "onebot_v11__get_group_member_info",
                {"group_id": 999},
            )
        result = await broker.invoke(
            "onebot_v11__get_group_member_info",
            {"no_cache": False},
        )

    assert result.status is ProtocolInvocationStatus.COMPLETED
    assert bot.calls[-1] == (
        "get_group_member_info",
        {"no_cache": False, "group_id": 456, "user_id": 123},
    )


@pytest.mark.asyncio
async def test_protocol_argument_json_has_a_hard_64_kib_limit(
    protocol_config,
) -> None:
    bot = _Bot(
        protocol="OneBot V12",
        supported_actions=["set_group_name"],
    )
    event = _event(user_id="user-123", group_id="group-456")
    broker = _broker()

    async with protocol_request_scope(
        bot,
        event,
        generation=2,
        is_superuser=True,
    ):
        with pytest.raises(ProtocolExecutionError, match="64 KiB"):
            await broker.invoke(
                "onebot_v12__set_group_name",
                {
                    "group_id": "group-456",
                    "group_name": "x" * (70 * 1024),
                },
            )

    assert bot.calls == [("get_supported_actions", {})]


@pytest.mark.asyncio
async def test_low_risk_direct_can_be_disabled_and_confirmed_in_second_phase(
    protocol_config,
) -> None:
    protocol_config["protocol_tools_low_risk_direct_enabled"] = False
    bot = _Bot()
    event = _event()
    broker = _broker()

    async with protocol_request_scope(
        bot,
        event,
        generation=3,
        is_superuser=False,
    ):
        waiting = await broker.invoke("qq__like_me", {"times": 1})

    assert waiting.status is ProtocolInvocationStatus.WAITING_CONFIRMATION
    assert waiting.confirmation_nonce is not None
    assert [api for api, _ in bot.calls] == ["get_version_info"]

    completed = await broker.confirm(
        waiting.confirmation_nonce,
        bot=bot,
        event=event,
        generation=3,
        is_superuser=False,
    )
    assert completed.status is ProtocolInvocationStatus.COMPLETED
    assert [api for api, _ in bot.calls] == [
        "get_version_info",
        "get_version_info",
        "send_like",
    ]
    with pytest.raises(ProtocolExecutionError, match="不存在、已过期或已使用"):
        await broker.confirm(
            waiting.confirmation_nonce,
            bot=bot,
            event=event,
            generation=3,
            is_superuser=False,
        )


@pytest.mark.asyncio
async def test_confirmation_expires_and_generation_or_session_change_fails_closed(
    protocol_config,
) -> None:
    protocol_config["protocol_tools_low_risk_direct_enabled"] = False
    now = [10.0]
    bot = _Bot()
    event = _event(group_id=456)
    broker = _broker(clock=lambda: now[0])

    async with protocol_request_scope(
        bot,
        event,
        generation=4,
        is_superuser=False,
    ):
        waiting = await broker.invoke("qq__like_me", {})
    assert waiting.confirmation_nonce is not None

    with pytest.raises(ProtocolExecutionError, match="会话不匹配"):
        await broker.confirm(
            waiting.confirmation_nonce,
            bot=bot,
            event=_event(group_id=999),
            generation=4,
            is_superuser=False,
        )

    with pytest.raises(ProtocolExecutionError, match="generation 已变化"):
        await broker.confirm(
            waiting.confirmation_nonce,
            bot=bot,
            event=event,
            generation=5,
            is_superuser=False,
        )

    async with protocol_request_scope(
        bot,
        event,
        generation=4,
        is_superuser=False,
    ):
        expiring = await broker.invoke("qq__like_me", {})
    assert expiring.confirmation_nonce is not None
    now[0] = 131.0
    with pytest.raises(ProtocolExecutionError, match="不存在、已过期或已使用"):
        await broker.confirm(
            expiring.confirmation_nonce,
            bot=bot,
            event=event,
            generation=4,
            is_superuser=False,
        )
    assert [api for api, _ in bot.calls].count("send_like") == 0


@pytest.mark.asyncio
async def test_confirmation_reprobes_v12_actions_and_rejects_capability_drift(
    protocol_config,
) -> None:
    bot = _Bot(
        protocol="OneBot V12",
        supported_actions=["set_group_name"],
    )
    event = _event(
        user_id="actor-1",
        group_id="group-1",
        message_id="message-1",
    )
    broker = _broker()

    async with protocol_request_scope(
        bot,
        event,
        generation=6,
        is_superuser=True,
    ):
        waiting = await broker.invoke(
            "onebot_v12__set_group_name",
            {"group_id": "group-1", "group_name": "新群名"},
        )
    assert waiting.status is ProtocolInvocationStatus.WAITING_CONFIRMATION
    assert waiting.confirmation_nonce is not None

    bot.supported_actions = []
    with pytest.raises(ProtocolExecutionError, match="能力、Bot、会话"):
        await broker.confirm(
            waiting.confirmation_nonce,
            bot=bot,
            event=event,
            generation=6,
            is_superuser=True,
        )
    assert [api for api, _ in bot.calls].count("set_group_name") == 0


@pytest.mark.asyncio
async def test_v11_text_message_profile_forces_auto_escape_and_calls_once(
    protocol_config,
) -> None:
    bot = _Bot()
    event = _event()
    broker = _broker()

    async with protocol_request_scope(
        bot,
        event,
        generation=61,
        is_superuser=True,
    ):
        with pytest.raises(ProtocolExecutionError, match="参数错误"):
            await broker.invoke(
                "onebot_v11__send_group_msg",
                {
                    "group_id": 999,
                    "message": [
                        {
                            "type": "image",
                            "data": {"file": "file:///etc/passwd"},
                        }
                    ],
                },
            )
        with pytest.raises(ProtocolExecutionError, match="参数错误"):
            await broker.invoke(
                "onebot_v11__send_group_msg",
                {
                    "group_id": 999,
                    "message": "正文",
                    "auto_escape": False,
                },
            )
        waiting = await broker.invoke(
            "onebot_v11__send_group_msg",
            {"group_id": 999, "message": "[CQ:image,file=file:///etc/passwd]"},
        )

    assert waiting.status is ProtocolInvocationStatus.WAITING_CONFIRMATION
    assert waiting.confirmation_nonce is not None
    pending = broker.pending._items[waiting.confirmation_nonce]
    assert pending.parameters() == {
        "auto_escape": True,
        "group_id": 999,
        "message": "[CQ:image,file=file:///etc/passwd]",
    }

    completed = await broker.confirm(
        waiting.confirmation_nonce,
        bot=bot,
        event=event,
        generation=61,
        is_superuser=True,
    )
    assert completed.status is ProtocolInvocationStatus.COMPLETED
    assert [item for item in bot.calls if item[0] == "send_group_msg"] == [
        (
            "send_group_msg",
            {
                "auto_escape": True,
                "group_id": 999,
                "message": "[CQ:image,file=file:///etc/passwd]",
            },
        )
    ]


@pytest.mark.asyncio
async def test_v12_text_message_profile_rejects_conflicting_targets(
    protocol_config,
) -> None:
    bot = _Bot(
        protocol="OneBot V12",
        supported_actions=["send_message"],
    )
    event = _event(user_id="actor", group_id="current-group")
    broker = _broker()

    async with protocol_request_scope(
        bot,
        event,
        generation=62,
        is_superuser=True,
    ):
        with pytest.raises(ProtocolExecutionError, match="相互冲突"):
            await broker.invoke(
                "onebot_v12__send_message",
                {
                    "detail_type": "private",
                    "user_id": "target-user",
                    "group_id": "other-group",
                    "message": "正文",
                },
            )
        waiting = await broker.invoke(
            "onebot_v12__send_message",
            {
                "detail_type": "private",
                "user_id": "target-user",
                "message": "正文",
            },
        )

    assert waiting.confirmation_nonce is not None
    await broker.confirm(
        waiting.confirmation_nonce,
        bot=bot,
        event=event,
        generation=62,
        is_superuser=True,
    )
    assert [item for item in bot.calls if item[0] == "send_message"] == [
        (
            "send_message",
            {
                "detail_type": "private",
                "message": "正文",
                "user_id": "target-user",
            },
        )
    ]


@pytest.mark.asyncio
async def test_confirmation_rejects_stored_policy_digest_drift(
    protocol_config,
) -> None:
    protocol_config["protocol_tools_low_risk_direct_enabled"] = False
    bot = _Bot()
    event = _event()
    broker = _broker()

    async with protocol_request_scope(
        bot,
        event,
        generation=7,
        is_superuser=False,
    ):
        waiting = await broker.invoke("qq__like_me", {})
    assert waiting.confirmation_nonce is not None
    pending = broker.pending._items[waiting.confirmation_nonce]
    broker.pending._items[waiting.confirmation_nonce] = replace(
        pending,
        policy_digest="0" * 64,
    )

    with pytest.raises(ProtocolExecutionError, match="审核策略已变化"):
        await broker.confirm(
            waiting.confirmation_nonce,
            bot=bot,
            event=event,
            generation=7,
            is_superuser=False,
        )
    assert [api for api, _ in bot.calls].count("send_like") == 0


@pytest.mark.asyncio
async def test_uncertain_side_effect_is_not_retried_and_keeps_rate_claim(
    protocol_config,
) -> None:
    bot = _Bot()
    bot.outcomes["send_like"] = TimeoutError()
    broker = _broker()

    async with protocol_request_scope(
        bot,
        _event(),
        generation=8,
        is_superuser=False,
    ):
        unknown = await broker.invoke("qq__like_me", {"times": 1})
        with pytest.raises(ProtocolExecutionError, match="结果仍不确定"):
            await broker.invoke("qq__like_me", {"times": 1})

    assert unknown.status is ProtocolInvocationStatus.RESULT_UNKNOWN
    assert [api for api, _ in bot.calls].count("send_like") == 1
    assert broker.audits()[-1].status == "result_unknown"


@pytest.mark.asyncio
async def test_like_daily_limit_counts_requested_times_conservatively(
    protocol_config,
) -> None:
    bot = _Bot()
    broker = _broker()

    async with protocol_request_scope(
        bot,
        _event(),
        generation=9,
        is_superuser=False,
    ):
        await broker.invoke("qq__like_me", {"times": 10})
        with pytest.raises(ProtocolExecutionError, match="达到限额"):
            await broker.invoke("qq__like_me", {"times": 1})

    assert [api for api, _ in bot.calls].count("send_like") == 1


@pytest.mark.asyncio
async def test_read_rate_limit_claim_is_atomic_under_concurrency(
    protocol_config,
) -> None:
    bot = _Bot()

    async def read_outcome(_api: str, _data: dict):
        await asyncio.sleep(0)
        return {"group_name": "测试群"}

    bot.outcomes["get_group_info"] = read_outcome
    broker = _broker()

    async with protocol_request_scope(
        bot,
        _event(),
        generation=10,
        is_superuser=False,
    ):
        outcomes = await asyncio.gather(
            *(broker.invoke("onebot_v11__get_group_info", {}) for _ in range(21)),
            return_exceptions=True,
        )

    assert sum(isinstance(item, ProtocolInvocation) for item in outcomes) == 20
    failures = [item for item in outcomes if isinstance(item, BaseException)]
    assert len(failures) == 1
    assert isinstance(failures[0], ProtocolExecutionError)
    assert "达到限额" in str(failures[0])
    assert [api for api, _ in bot.calls].count("get_group_info") == 20


def test_protocol_results_redact_secrets_paths_and_large_binary_values() -> None:
    sanitized = sanitize_protocol_result(
        {
            "token": "top-secret",
            "nested": {
                "Authorization": "Bearer hidden",
                "cookie_value": "secret-cookie",
                "path_value": "/srv/private/file",
                "safe": "可见",
            },
            "blob": "A" * 512,
            "absolute": "/etc/passwd",
        },
        ("cookie_value",),
    )

    assert sanitized["token"] == "[redacted]"
    assert sanitized["nested"]["Authorization"] == "[redacted]"
    assert sanitized["nested"]["cookie_value"] == "[redacted]"
    assert sanitized["nested"]["path_value"] == "[redacted]"
    assert sanitized["nested"]["safe"] == "可见"
    assert sanitized["blob"] == "[binary redacted]"
    assert sanitized["absolute"] == "[path redacted]"

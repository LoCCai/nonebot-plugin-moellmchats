from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any

import pytest

from nonebot_plugin_moellmchats.chat_history import MessageRecord
from nonebot_plugin_moellmchats.session_summary import (
    DEFAULT_SUMMARY_KEEP_RECENT_MESSAGES,
    DEFAULT_SUMMARY_TRIGGER_MESSAGES,
    SESSION_SUMMARY_POLICY_VERSION,
    SessionSummaryPlan,
    SessionSummaryPolicy,
    SessionSummaryRecord,
    SessionSummarySourceTooLargeError,
)

_NOW = datetime(2026, 8, 22, 21, 30, tzinfo=timezone.utc)


def _message(
    message_id: int | None,
    *,
    conversation_id: str = "conversation-1",
    content: str | None = None,
    created_offset: int | None = None,
) -> MessageRecord:
    offset = (message_id or 0) if created_offset is None else created_offset
    return MessageRecord(
        message_id=message_id,
        conversation_id=conversation_id,
        platform_message_id=None if message_id is None else f"platform-{message_id}",
        role="user" if (message_id or 0) % 2 else "assistant",
        sender_id="user-1",
        content=content if content is not None else f"message-{message_id}",
        structured_content={"ordinal": message_id},
        created_at=_NOW + timedelta(seconds=offset),
    )


def _messages(start: int, count: int, **kwargs: Any) -> tuple[MessageRecord, ...]:
    return tuple(_message(message_id, **kwargs) for message_id in range(start, start + count))


def _first_summary() -> SessionSummaryRecord:
    plan = SessionSummaryPolicy().plan(
        "conversation-1",
        previous_summary=None,
        messages=_messages(1, 50),
    )
    assert plan is not None
    return plan.complete(
        summary_id="summary-1",
        model_provider="deepseek",
        model="deepseek-chat",
        content="Earlier conversation summary.",
        created_at=_NOW + timedelta(minutes=2),
    )


def test_default_policy_builds_canonical_50_to_10_compaction_plan() -> None:
    messages = _messages(1, DEFAULT_SUMMARY_TRIGGER_MESSAGES)
    plan = SessionSummaryPolicy().plan(
        "conversation-1",
        previous_summary=None,
        messages=messages,
    )

    assert isinstance(plan, SessionSummaryPlan)
    assert tuple(message.message_id for message in plan.source_messages) == tuple(range(1, 41))
    assert tuple(message.message_id for message in plan.retained_messages) == tuple(range(41, 51))
    assert len(plan.retained_messages) == DEFAULT_SUMMARY_KEEP_RECENT_MESSAGES
    assert plan.generation == 1
    assert plan.covered_from_message_id == 1
    assert plan.covered_through_message_id == 40
    assert plan.covered_message_count == 40
    assert plan.source_digest == hashlib.sha256(plan.model_input.encode("utf-8")).hexdigest()

    payload = json.loads(plan.model_input)
    assert payload["schema"] == SESSION_SUMMARY_POLICY_VERSION
    assert payload["previous_summary"] is None
    assert payload["policy"] == {
        "keep_recent_message_count": 10,
        "trigger_message_count": 50,
    }
    assert [item["message_id"] for item in payload["messages"]] == list(range(1, 41))
    assert "conversation-1" not in plan.model_input

    record = plan.complete(
        summary_id="summary-1",
        model_provider="deepseek",
        model="deepseek-chat",
        content="Messages one through forty establish the session context.",
        created_at=_NOW + timedelta(minutes=2),
    )
    assert record.generation == 1
    assert record.previous_summary_id is None
    assert record.covered_message_count == 40
    assert record.source_message_count == 40
    assert record.source_digest == plan.source_digest
    assert record.trigger_message_count == 50
    assert record.keep_recent_message_count == 10
    assert record.max_source_chars == 64_000
    assert record.source_char_count == len(plan.model_input)
    assert record.created_at.tzinfo is timezone.utc
    assert record.as_dict()["content"] == record.content

    with pytest.raises(FrozenInstanceError):
        record.content = "changed"  # type: ignore[misc]


def test_incremental_plan_binds_previous_summary_and_advances_linear_watermark() -> None:
    previous = _first_summary()
    messages = _messages(41, 50, created_offset=180)

    plan = SessionSummaryPolicy().plan(
        "conversation-1",
        previous_summary=previous,
        messages=messages,
    )

    assert plan is not None
    assert plan.generation == 2
    assert plan.covered_from_message_id == 41
    assert plan.covered_through_message_id == 80
    assert plan.covered_message_count == 80
    assert tuple(message.message_id for message in plan.retained_messages) == tuple(range(81, 91))
    payload = json.loads(plan.model_input)
    assert payload["previous_summary"] == {
        "content": previous.content,
        "covered_message_count": 40,
        "covered_through_message_id": 40,
        "generation": 1,
        "source_digest": previous.source_digest,
        "summary_id": "summary-1",
    }

    completed = plan.complete(
        summary_id="summary-2",
        model_provider="deepseek",
        model="deepseek-chat",
        content="Updated summary through message eighty.",
        created_at=_NOW + timedelta(minutes=4),
    )
    assert completed.previous_summary_id == previous.summary_id
    assert completed.generation == previous.generation + 1
    assert completed.covered_message_count == previous.covered_message_count + 40


def test_policy_waits_below_threshold_without_advancing_any_watermark() -> None:
    previous = _first_summary()
    policy = SessionSummaryPolicy()

    assert (
        policy.plan(
            "conversation-1",
            previous_summary=previous,
            messages=_messages(41, 49, created_offset=180),
        )
        is None
    )
    assert (
        policy.plan(
            "conversation-1",
            previous_summary=None,
            messages=(),
        )
        is None
    )


def test_bounded_policy_reduces_only_the_complete_source_prefix_without_dropping_messages() -> None:
    policy = SessionSummaryPolicy(
        trigger_message_count=4,
        keep_recent_message_count=1,
        max_source_chars=1_024,
    )
    messages = _messages(1, 4, content="x" * 300)

    plan = policy.plan(
        "conversation-1",
        previous_summary=None,
        messages=messages,
    )

    assert plan is not None
    assert 1 <= len(plan.source_messages) < 3
    assert len(plan.model_input) <= 1_024
    assert plan.source_messages + plan.retained_messages == messages
    assert len(plan.retained_messages) >= policy.keep_recent_message_count
    assert plan.covered_through_message_id < plan.retained_messages[0].message_id  # type: ignore[operator]


def test_oversized_next_complete_message_fails_without_a_partial_digest() -> None:
    policy = SessionSummaryPolicy(
        trigger_message_count=3,
        keep_recent_message_count=1,
        max_source_chars=1_024,
    )
    messages = (
        _message(1, content="x" * 5_000),
        _message(2),
        _message(3),
    )

    with pytest.raises(SessionSummarySourceTooLargeError, match="覆盖水位未推进"):
        policy.plan(
            "conversation-1",
            previous_summary=None,
            messages=messages,
        )


@pytest.mark.parametrize(
    "messages",
    [
        (_message(None),),
        (_message(2), _message(1)),
        (_message(1), _message(1)),
        (_message(1), _message(2, conversation_id="conversation-2")),
    ],
)
def test_policy_rejects_draft_cross_conversation_or_non_monotonic_sources(
    messages: tuple[MessageRecord, ...],
) -> None:
    with pytest.raises(ValueError, match=r"持久化|跨会话|严格递增"):
        SessionSummaryPolicy(
            trigger_message_count=2,
            keep_recent_message_count=1,
        ).plan(
            "conversation-1",
            previous_summary=None,
            messages=messages,
        )


def test_policy_rejects_stale_or_unbounded_candidate_windows() -> None:
    previous = _first_summary()
    policy = SessionSummaryPolicy()

    with pytest.raises(ValueError, match="覆盖水位"):
        policy.plan(
            "conversation-1",
            previous_summary=previous,
            messages=_messages(40, 50, created_offset=180),
        )
    with pytest.raises(ValueError, match="超过确定性触发窗口"):
        policy.plan(
            "conversation-1",
            previous_summary=None,
            messages=_messages(1, 51),
        )
    with pytest.raises(ValueError, match="不属于当前会话"):
        policy.plan(
            "conversation-2",
            previous_summary=previous,
            messages=(),
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"summary_id": ""},
        {"generation": 0},
        {"generation": 1, "previous_summary_id": "summary-0"},
        {"generation": 2, "previous_summary_id": None},
        {"covered_from_message_id": 41, "covered_through_message_id": 40},
        {"covered_message_count": 1, "source_message_count": 2},
        {"source_digest": "not-a-digest"},
        {"policy_version": "future-policy"},
        {"trigger_message_count": 1},
        {"keep_recent_message_count": 0},
        {"max_source_chars": 1_000},
        {"source_char_count": 0},
        {"model_provider": " deepseek"},
        {"model": ""},
        {"content": " summary"},
        {"content": "bad\x00summary"},
        {"created_at": datetime(2026, 8, 22, 21, 30)},
    ],
)
def test_summary_record_rejects_invalid_durable_values(changes: dict[str, Any]) -> None:
    record = _first_summary()

    with pytest.raises(
        ValueError,
        match=r"SessionSummaryRecord|摘要|policy_version|message_count|max_source_chars",
    ):
        replace(record, **changes)


def test_completion_rejects_output_before_source_or_previous_summary() -> None:
    previous = _first_summary()
    plan = SessionSummaryPolicy().plan(
        "conversation-1",
        previous_summary=previous,
        messages=_messages(41, 50, created_offset=180),
    )
    assert plan is not None

    with pytest.raises(ValueError, match="完成时间"):
        plan.complete(
            summary_id="summary-2",
            model_provider="deepseek",
            model="deepseek-chat",
            content="too early",
            created_at=_NOW,
        )

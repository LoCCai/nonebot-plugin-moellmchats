from __future__ import annotations

from dataclasses import FrozenInstanceError
import math

import pytest

from nonebot_plugin_moellmchats.agent_runtime import AgentRun, AgentRunState


def _run(**overrides: object) -> AgentRun:
    values: dict[str, object] = {
        "run_id": "run_01HZX7Y95Z8QW8D4WTV4VCZZY2",
        "request_id": 17,
        "user_id": "qq:10001",
        "group_id": "qq-group:20002",
        "generation": 9,
        "state": AgentRunState.CREATED,
        "started_at": 100.25,
        "finished_at": None,
    }
    values.update(overrides)
    return AgentRun(**values)  # type: ignore[arg-type]


def test_agent_run_is_frozen_generation_bound_and_serializable() -> None:
    run = _run()

    assert run.generation == 9
    assert run.state is AgentRunState.CREATED
    assert run.is_terminal is False
    assert run.elapsed is None
    assert run.as_dict() == {
        "run_id": "run_01HZX7Y95Z8QW8D4WTV4VCZZY2",
        "request_id": 17,
        "user_id": "qq:10001",
        "group_id": "qq-group:20002",
        "generation": 9,
        "state": "created",
        "started_at": 100.25,
        "finished_at": None,
    }
    with pytest.raises(FrozenInstanceError):
        run.generation = 10  # type: ignore[misc]


def test_agent_run_supports_private_requests_and_normalizes_timestamps() -> None:
    run = _run(group_id=None, started_at=100)

    assert run.group_id is None
    assert run.started_at == 100.0
    assert isinstance(run.started_at, float)


@pytest.mark.parametrize(
    "state",
    [
        AgentRunState.COMPLETED,
        AgentRunState.FAILED,
        AgentRunState.CANCELLED,
        AgentRunState.TIMED_OUT,
        AgentRunState.REJECTED,
    ],
)
def test_agent_run_terminal_states_require_a_finish_timestamp(
    state: AgentRunState,
) -> None:
    run = _run(state=state, finished_at=103.75)

    assert run.is_terminal is True
    assert run.elapsed == 3.5
    assert run.as_dict()["state"] == state.value


@pytest.mark.parametrize(
    "state",
    [
        AgentRunState.CREATED,
        AgentRunState.ADMITTED,
        AgentRunState.CLASSIFYING,
        AgentRunState.PLANNING,
        AgentRunState.EXECUTING,
        AgentRunState.WAITING_CONFIRMATION,
        AgentRunState.SUMMARIZING,
    ],
)
def test_agent_run_nonterminal_states_reject_a_finish_timestamp(
    state: AgentRunState,
) -> None:
    with pytest.raises(ValueError, match="非终态"):
        _run(state=state, finished_at=101.0)


@pytest.mark.parametrize(
    "state",
    [
        AgentRunState.COMPLETED,
        AgentRunState.FAILED,
        AgentRunState.CANCELLED,
        AgentRunState.TIMED_OUT,
        AgentRunState.REJECTED,
    ],
)
def test_agent_run_terminal_states_reject_missing_finish_timestamp(
    state: AgentRunState,
) -> None:
    with pytest.raises(ValueError, match="终态"):
        _run(state=state)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("run_id", "", "run_id"),
        ("run_id", "run id", "run_id"),
        ("run_id", "x" * 129, "run_id"),
        ("request_id", 0, "request_id"),
        ("request_id", True, "request_id"),
        ("user_id", "", "user_id"),
        ("user_id", " user", "user_id"),
        ("user_id", "user\nname", "user_id"),
        ("group_id", "", "group_id"),
        ("group_id", 123, "group_id"),
        ("generation", -1, "generation"),
        ("generation", True, "generation"),
        ("state", "created", "state"),
    ],
)
def test_agent_run_rejects_invalid_identity_and_state_fields(
    field: str,
    value: object,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _run(**{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("started_at", -1.0),
        ("started_at", math.inf),
        ("started_at", math.nan),
        ("started_at", True),
        ("finished_at", math.inf),
        ("finished_at", math.nan),
        ("finished_at", True),
    ],
)
def test_agent_run_rejects_invalid_timestamps(field: str, value: object) -> None:
    state = AgentRunState.COMPLETED if field == "finished_at" else AgentRunState.CREATED
    finished_at = 101.0 if state is AgentRunState.COMPLETED else None
    overrides = {"state": state, "finished_at": finished_at, field: value}

    with pytest.raises(ValueError, match=field):
        _run(**overrides)


def test_agent_run_rejects_finish_before_start() -> None:
    with pytest.raises(ValueError, match="不能早于"):
        _run(
            state=AgentRunState.COMPLETED,
            started_at=100.0,
            finished_at=99.0,
        )

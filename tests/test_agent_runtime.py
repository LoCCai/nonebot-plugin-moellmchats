from __future__ import annotations

from collections.abc import Mapping
from dataclasses import FrozenInstanceError
import math

import pytest

from nonebot_plugin_moellmchats.agent_runtime import (
    AgentRun,
    AgentRunState,
    AgentStep,
    AgentStepStatus,
    AgentStepType,
    ToolCall,
    ToolCallStatus,
)


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


def _step(**overrides: object) -> AgentStep:
    values: dict[str, object] = {
        "step_id": "step_0001",
        "run_id": "run_01HZX7Y95Z8QW8D4WTV4VCZZY2",
        "index": 0,
        "type": AgentStepType.CLASSIFICATION,
        "status": AgentStepStatus.PENDING,
        "model": None,
        "tool": None,
        "input": None,
        "output": None,
        "started_at": None,
        "finished_at": None,
    }
    values.update(overrides)
    return AgentStep(**values)  # type: ignore[arg-type]


def _tool_call(**overrides: object) -> ToolCall:
    values: dict[str, object] = {
        "tool_call_id": "call_0001",
        "run_id": "run_01HZX7Y95Z8QW8D4WTV4VCZZY2",
        "step_id": "step_0001",
        "tool_name": "weather_lookup",
        "bundle_digest": None,
        "arguments": {"city": "Shanghai"},
        "status": ToolCallStatus.PENDING,
        "confirmed": False,
        "result": None,
        "elapsed": None,
    }
    values.update(overrides)
    return ToolCall(**values)  # type: ignore[arg-type]


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


def test_agent_step_is_frozen_and_serializes_detached_structured_data() -> None:
    step_input = {
        "messages": [{"role": "user", "content": "hello"}],
        "weights": [1, 2.5],
    }
    step = _step(input=step_input)

    step_input["messages"][0]["content"] = "tampered"
    step_input["weights"].append(3)
    assert isinstance(step.input, Mapping)
    messages = step.input["messages"]
    assert isinstance(messages, tuple)
    assert isinstance(messages[0], Mapping)
    assert messages[0]["content"] == "hello"
    assert step.input["weights"] == (1, 2.5)
    with pytest.raises(TypeError):
        step.input["new"] = "value"  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        step.index = 1  # type: ignore[misc]

    serialized = step.as_dict()
    assert serialized == {
        "step_id": "step_0001",
        "run_id": "run_01HZX7Y95Z8QW8D4WTV4VCZZY2",
        "index": 0,
        "type": "classification",
        "model": None,
        "tool": None,
        "status": "pending",
        "input": {
            "messages": [{"role": "user", "content": "hello"}],
            "weights": [1, 2.5],
        },
        "output": None,
        "started_at": None,
        "finished_at": None,
    }
    serialized["input"]["messages"][0]["content"] = "changed"
    assert messages[0]["content"] == "hello"


@pytest.mark.parametrize("step_type", list(AgentStepType))
def test_agent_step_supports_every_planned_step_type(
    step_type: AgentStepType,
) -> None:
    identity = {}
    if step_type is AgentStepType.MODEL:
        identity["model"] = "gpt-5"
    elif step_type is AgentStepType.TOOL:
        identity["tool"] = "weather_lookup"
    step = _step(type=step_type, **identity)

    assert step.type is step_type
    assert step.as_dict()["type"] == step_type.value


def test_agent_step_running_state_requires_only_start_timestamp() -> None:
    step = _step(
        type=AgentStepType.MODEL,
        status=AgentStepStatus.RUNNING,
        model="gpt-5",
        started_at=20,
    )

    assert step.started_at == 20.0
    assert step.finished_at is None
    assert step.elapsed is None
    assert step.is_terminal is False


@pytest.mark.parametrize(
    "status",
    [
        AgentStepStatus.COMPLETED,
        AgentStepStatus.FAILED,
        AgentStepStatus.CANCELLED,
        AgentStepStatus.TIMED_OUT,
        AgentStepStatus.SKIPPED,
    ],
)
def test_agent_step_terminal_states_require_complete_timestamps(
    status: AgentStepStatus,
) -> None:
    step = _step(
        type=AgentStepType.TOOL,
        status=status,
        tool="weather_lookup",
        output={"ok": status is AgentStepStatus.COMPLETED},
        started_at=20.0,
        finished_at=21.25,
    )

    assert step.is_terminal is True
    assert step.elapsed == 1.25
    assert step.as_dict()["status"] == status.value


@pytest.mark.parametrize(
    ("status", "started_at", "finished_at", "message"),
    [
        (AgentStepStatus.PENDING, 1.0, None, "pending"),
        (AgentStepStatus.PENDING, None, 2.0, "pending"),
        (AgentStepStatus.RUNNING, None, None, "running"),
        (AgentStepStatus.RUNNING, 1.0, 2.0, "running"),
        (AgentStepStatus.COMPLETED, None, 2.0, "终态"),
        (AgentStepStatus.FAILED, 1.0, None, "终态"),
    ],
)
def test_agent_step_rejects_incoherent_status_timestamps(
    status: AgentStepStatus,
    started_at: float | None,
    finished_at: float | None,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _step(
            status=status,
            started_at=started_at,
            finished_at=finished_at,
        )


@pytest.mark.parametrize(
    ("step_type", "message"),
    [
        (AgentStepType.MODEL, "model"),
        (AgentStepType.TOOL, "tool"),
    ],
)
def test_agent_step_requires_type_specific_identity(
    step_type: AgentStepType,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _step(type=step_type)


@pytest.mark.parametrize(
    ("status", "started_at"),
    [
        (AgentStepStatus.PENDING, None),
        (AgentStepStatus.RUNNING, 1.0),
    ],
)
def test_agent_step_nonterminal_states_reject_output(
    status: AgentStepStatus,
    started_at: float | None,
) -> None:
    with pytest.raises(ValueError, match="output"):
        _step(status=status, started_at=started_at, output={"partial": True})


def test_agent_step_rejects_finish_before_start() -> None:
    with pytest.raises(ValueError, match="不能早于"):
        _step(
            status=AgentStepStatus.COMPLETED,
            started_at=2.0,
            finished_at=1.0,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("step_id", "bad step", "step_id"),
        ("run_id", "", "run_id"),
        ("index", -1, "index"),
        ("index", True, "index"),
        ("type", "model", "type"),
        ("status", "pending", "status"),
        ("model", " model", "model"),
        ("model", "x" * 129, "model"),
        ("tool", "tool\nname", "tool"),
        ("tool", 1, "tool"),
    ],
)
def test_agent_step_rejects_invalid_identity_and_enum_fields(
    field: str,
    value: object,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _step(**{field: value})


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
def test_agent_step_rejects_invalid_timestamps(field: str, value: object) -> None:
    overrides = {
        "status": AgentStepStatus.COMPLETED,
        "started_at": 1.0,
        "finished_at": 2.0,
        field: value,
    }

    with pytest.raises(ValueError, match=field):
        _step(**overrides)


@pytest.mark.parametrize(
    "value",
    [
        object(),
        {1: "non-string key"},
        {"value": math.inf},
        {"value": math.nan},
    ],
)
def test_agent_step_rejects_non_json_payloads(value: object) -> None:
    with pytest.raises(ValueError, match="JSON"):
        _step(input=value)


def test_agent_step_rejects_cyclic_json_payloads() -> None:
    cyclic: list[object] = []
    cyclic.append(cyclic)

    with pytest.raises(ValueError, match="循环"):
        _step(input=cyclic)


def test_agent_step_rejects_excessively_deep_json_payloads() -> None:
    nested: object = "leaf"
    for _ in range(34):
        nested = [nested]

    with pytest.raises(ValueError, match="嵌套"):
        _step(output=nested)


def test_tool_call_is_frozen_and_detaches_arguments() -> None:
    arguments = {
        "city": "Shanghai",
        "options": {"units": "metric", "days": [1, 2]},
    }
    call = _tool_call(arguments=arguments)

    arguments["city"] = "tampered"
    arguments["options"]["days"].append(3)
    assert call.arguments["city"] == "Shanghai"
    assert isinstance(call.arguments["options"], Mapping)
    assert call.arguments["options"]["days"] == (1, 2)
    with pytest.raises(TypeError):
        call.arguments["city"] = "changed"  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        call.status = ToolCallStatus.RUNNING  # type: ignore[misc]

    assert call.as_dict() == {
        "tool_call_id": "call_0001",
        "run_id": "run_01HZX7Y95Z8QW8D4WTV4VCZZY2",
        "step_id": "step_0001",
        "tool_name": "weather_lookup",
        "bundle_digest": None,
        "arguments": {
            "city": "Shanghai",
            "options": {"units": "metric", "days": [1, 2]},
        },
        "status": "pending",
        "confirmed": False,
        "result": None,
        "elapsed": None,
    }


def test_completed_tool_call_requires_and_detaches_result() -> None:
    result = {
        "text": "sunny",
        "images": [],
        "metadata": {"source": "provider"},
    }
    call = _tool_call(
        bundle_digest="a" * 64,
        status=ToolCallStatus.COMPLETED,
        confirmed=True,
        result=result,
        elapsed=0.75,
    )

    result["metadata"]["source"] = "tampered"
    assert call.is_terminal is True
    assert call.elapsed == 0.75
    assert isinstance(call.result, Mapping)
    metadata = call.result["metadata"]
    assert isinstance(metadata, Mapping)
    assert metadata["source"] == "provider"
    assert call.as_dict()["bundle_digest"] == "a" * 64


@pytest.mark.parametrize(
    "status",
    [
        ToolCallStatus.FAILED,
        ToolCallStatus.CANCELLED,
        ToolCallStatus.TIMED_OUT,
        ToolCallStatus.REJECTED,
    ],
)
def test_noncompleted_tool_call_terminal_states_allow_empty_result(
    status: ToolCallStatus,
) -> None:
    call = _tool_call(status=status, elapsed=0)

    assert call.is_terminal is True
    assert call.elapsed == 0.0
    assert call.result is None


@pytest.mark.parametrize(
    "status",
    [
        ToolCallStatus.PENDING,
        ToolCallStatus.WAITING_CONFIRMATION,
        ToolCallStatus.RUNNING,
    ],
)
def test_tool_call_nonterminal_states_reject_result_and_elapsed(
    status: ToolCallStatus,
) -> None:
    with pytest.raises(ValueError, match="非终态"):
        _tool_call(status=status, result={"text": "early"})
    with pytest.raises(ValueError, match="非终态"):
        _tool_call(status=status, elapsed=0.1)


def test_tool_call_confirmation_state_is_fail_closed() -> None:
    waiting = _tool_call(status=ToolCallStatus.WAITING_CONFIRMATION)

    assert waiting.confirmed is False
    assert waiting.is_terminal is False
    with pytest.raises(ValueError, match="confirmed"):
        _tool_call(
            status=ToolCallStatus.WAITING_CONFIRMATION,
            confirmed=True,
        )


def test_completed_tool_call_requires_result_and_all_terminals_require_elapsed() -> None:
    with pytest.raises(ValueError, match="result"):
        _tool_call(status=ToolCallStatus.COMPLETED, elapsed=1.0)
    with pytest.raises(ValueError, match="elapsed"):
        _tool_call(status=ToolCallStatus.FAILED)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("tool_call_id", "bad call", "tool_call_id"),
        ("run_id", "", "run_id"),
        ("step_id", "bad step", "step_id"),
        ("tool_name", "bad tool", "tool_name"),
        ("tool_name", "x" * 65, "tool_name"),
        ("bundle_digest", "A" * 64, "bundle_digest"),
        ("bundle_digest", "a" * 63, "bundle_digest"),
        ("status", "pending", "status"),
        ("confirmed", 1, "confirmed"),
    ],
)
def test_tool_call_rejects_invalid_identity_and_state_fields(
    field: str,
    value: object,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _tool_call(**{field: value})


@pytest.mark.parametrize(
    "value",
    [
        -1.0,
        math.inf,
        math.nan,
        True,
        "1.0",
    ],
)
def test_tool_call_rejects_invalid_elapsed(value: object) -> None:
    with pytest.raises(ValueError, match="elapsed"):
        _tool_call(status=ToolCallStatus.FAILED, elapsed=value)


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        "city=Shanghai",
        {1: "non-string key"},
        {"temperature": math.inf},
    ],
)
def test_tool_call_rejects_invalid_arguments(arguments: object) -> None:
    with pytest.raises(ValueError, match="arguments"):
        _tool_call(arguments=arguments)


def test_tool_call_rejects_cyclic_result() -> None:
    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic

    with pytest.raises(ValueError, match="循环"):
        _tool_call(
            status=ToolCallStatus.COMPLETED,
            result=cyclic,
            elapsed=0.1,
        )

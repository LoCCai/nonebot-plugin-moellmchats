from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError, replace
import importlib
import json
from typing import Any

import pytest

from nonebot_plugin_moellmchats.agent_run_api import (
    AGENT_RUN_API_READ_SCOPE,
    AGENT_RUN_API_WRITE_SCOPE,
    AgentRunApiEndpoint,
    AgentRunApiService,
    AgentRunCancellationConflictError,
    AgentRunCancellationNotFoundError,
    AgentRunCancellationResultUnknownError,
    AgentRunCancellationUnavailableError,
    AgentRunReadRequest,
    CancelAgentRunCommand,
    CancelAgentRunResult,
)
from nonebot_plugin_moellmchats.agent_runtime import AgentRun, AgentRunState
from nonebot_plugin_moellmchats.runtime_api import (
    RuntimeApiASGIApp,
    RuntimeApiConfigurationError,
    RuntimeApiCredential,
    RuntimeApiPrincipal,
    RuntimeApiRequest,
    RuntimeApiResponse,
    StaticBearerRuntimeApiAuthenticator,
)

_TOKEN = "r" * 32
_WRONG_TOKEN = "w" * 32
_OPERATION_ID = "f" * 64
_MAX_BIGINT = (1 << 63) - 1
_UNSET = object()


def _run(**overrides: object) -> AgentRun:
    values: dict[str, object] = {
        "run_id": "run_0001",
        "request_id": 17,
        "user_id": "sensitive-user:10001",
        "group_id": "sensitive-group:20002",
        "generation": 7,
        "state": AgentRunState.EXECUTING,
        "started_at": 100.25,
        "finished_at": None,
    }
    values.update(overrides)
    state = values["state"]
    if (
        isinstance(state, AgentRunState)
        and state
        in {
            AgentRunState.COMPLETED,
            AgentRunState.FAILED,
            AgentRunState.CANCELLED,
            AgentRunState.TIMED_OUT,
            AgentRunState.REJECTED,
        }
        and "finished_at" not in overrides
    ):
        started_at = values["started_at"]
        assert isinstance(started_at, (int, float))
        assert not isinstance(started_at, bool)
        values["finished_at"] = float(started_at) + 1.0
    return AgentRun(**values)  # type: ignore[arg-type]


def _records(count: int) -> tuple[AgentRun, ...]:
    return tuple(
        _run(
            run_id=f"run_{index:04d}",
            request_id=index + 1,
            started_at=1_000.0 - index,
        )
        for index in range(count)
    )


def _credential(token: str = _TOKEN) -> RuntimeApiCredential:
    value = RuntimeApiCredential.from_authorization_header(f"Bearer {token}".encode("ascii"))
    assert value is not None
    return value


def _principal(*scopes: str) -> RuntimeApiPrincipal:
    return RuntimeApiPrincipal(
        subject="run-admin",
        scopes=frozenset(scopes),
    )


class _Runs:
    def __init__(self, records: tuple[AgentRun, ...]) -> None:
        self.records = records
        self.list_calls: list[AgentRunReadRequest] = []
        self.get_calls: list[str] = []
        self.list_error: BaseException | None = None
        self.get_error: BaseException | None = None
        self.list_result: object = _UNSET
        self.get_result: object = _UNSET

    async def list_runs(
        self,
        request: AgentRunReadRequest,
    ) -> tuple[AgentRun, ...]:
        self.list_calls.append(request)
        if self.list_error is not None:
            raise self.list_error
        if self.list_result is not _UNSET:
            return self.list_result  # type: ignore[return-value]
        records = self.records
        if request.before_started_at is not None:
            assert request.before_run_id is not None
            anchor = (request.before_started_at, request.before_run_id)
            records = tuple(run for run in records if (run.started_at, run.run_id) < anchor)
        return records[: request.limit]

    async def get_run(self, run_id: str) -> AgentRun | None:
        self.get_calls.append(run_id)
        if self.get_error is not None:
            raise self.get_error
        if self.get_result is not _UNSET:
            return self.get_result  # type: ignore[return-value]
        return next((run for run in self.records if run.run_id == run_id), None)


class _Cancellations:
    def __init__(self, runs: _Runs) -> None:
        self.runs = runs
        self.calls: list[CancelAgentRunCommand] = []
        self.error: BaseException | None = None
        self.result: object = _UNSET

    async def cancel_run(
        self,
        command: CancelAgentRunCommand,
    ) -> CancelAgentRunResult:
        self.calls.append(command)
        if self.error is not None:
            raise self.error
        if self.result is not _UNSET:
            return self.result  # type: ignore[return-value]
        current = next(run for run in self.runs.records if run.run_id == command.run_id)
        cancelled = replace(
            current,
            state=AgentRunState.CANCELLED,
            finished_at=current.started_at + 1.5,
        )
        return CancelAgentRunResult(
            operation_id=_OPERATION_ID,
            previous_state=current.state,
            run=cancelled,
            cancellation_settled=True,
            audit_recorded=True,
        )


class _FixedAuthenticator:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls = 0
        self.error: BaseException | None = None

    async def authenticate(
        self,
        request: RuntimeApiRequest,
    ) -> RuntimeApiPrincipal | None:
        assert isinstance(request, RuntimeApiRequest)
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result  # type: ignore[return-value]


def _service(
    records: tuple[AgentRun, ...] | None = None,
    *,
    principal: RuntimeApiPrincipal | None = None,
    authenticator: object | None = None,
) -> tuple[AgentRunApiService, _Runs, _Cancellations]:
    runs = _Runs((_run(),) if records is None else records)
    cancellations = _Cancellations(runs)
    auth = (
        StaticBearerRuntimeApiAuthenticator(
            token=_TOKEN,
            principal=principal
            or _principal(
                AGENT_RUN_API_READ_SCOPE,
                AGENT_RUN_API_WRITE_SCOPE,
            ),
        )
        if authenticator is None
        else authenticator
    )
    return (
        AgentRunApiService(
            runs=runs,
            authenticator=auth,  # type: ignore[arg-type]
            cancellations=cancellations,
        ),
        runs,
        cancellations,
    )


def _request(
    path: str = "/agent-runs",
    *,
    method: str = "GET",
    token: str | None = _TOKEN,
    query_string: bytes = b"",
    content_type: str | None = None,
    body: bytes = b"",
) -> RuntimeApiRequest:
    return RuntimeApiRequest(
        method=method,
        path=path,
        query_string=query_string,
        credential=None if token is None else _credential(token),
        content_type=content_type,
        body=body,
    )


def _cancel_body(
    *,
    state: AgentRunState = AgentRunState.EXECUTING,
    generation: int = 7,
) -> bytes:
    return json.dumps(
        {
            "expected_generation": generation,
            "expected_state": state.value,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _payload(response: RuntimeApiResponse) -> dict[str, Any]:
    return json.loads(response.body)


async def _call_asgi(
    app: RuntimeApiASGIApp,
    *,
    method: str,
    path: str,
    body: bytes = b"",
    content_type: bytes | None = None,
) -> tuple[int, dict[bytes, bytes], dict[str, Any]]:
    sent: list[dict[str, Any]] = []
    headers = [(b"authorization", f"Bearer {_TOKEN}".encode("ascii"))]
    if content_type is not None:
        headers.append((b"content-type", content_type))
    if body:
        headers.append((b"content-length", str(len(body)).encode("ascii")))

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(
        {
            "type": "http",
            "method": method,
            "path": path,
            "query_string": b"",
            "headers": headers,
        },
        receive,
        send,
    )
    assert len(sent) == 2
    return (
        sent[0]["status"],
        dict(sent[0]["headers"]),
        json.loads(sent[1]["body"]),
    )


def test_endpoint_contract_is_exact_frozen_and_separates_scopes() -> None:
    service, _, _ = _service()

    assert service.endpoints == (
        AgentRunApiEndpoint(
            "GET",
            "/agent-runs",
            AGENT_RUN_API_READ_SCOPE,
        ),
        AgentRunApiEndpoint(
            "GET",
            "/agent-runs/{id}",
            AGENT_RUN_API_READ_SCOPE,
        ),
        AgentRunApiEndpoint(
            "POST",
            "/agent-runs/{id}/cancel",
            AGENT_RUN_API_WRITE_SCOPE,
        ),
    )
    with pytest.raises(FrozenInstanceError):
        service.endpoints[0].method = "POST"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("method", "path", "scope"),
    [
        ("DELETE", "/agent-runs", AGENT_RUN_API_READ_SCOPE),
        ("GET", "/agent-runs/other", AGENT_RUN_API_WRITE_SCOPE),
        ("POST", "/agent-runs/{id}", AGENT_RUN_API_WRITE_SCOPE),
    ],
)
def test_endpoint_contract_rejects_expansion_or_wrong_scope(
    method: str,
    path: str,
    scope: str,
) -> None:
    with pytest.raises(RuntimeApiConfigurationError):
        AgentRunApiEndpoint(method, path, scope)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"limit": 0},
        {"limit": 22},
        {"limit": True},
        {"limit": 1, "before_started_at": 1.0},
        {"limit": 1, "before_run_id": "run_0001"},
        {
            "limit": 1,
            "before_started_at": -1.0,
            "before_run_id": "run_0001",
        },
        {
            "limit": 1,
            "before_started_at": float("inf"),
            "before_run_id": "run_0001",
        },
        {
            "limit": 1,
            "before_started_at": 1.0,
            "before_run_id": "bad run",
        },
    ],
)
def test_read_request_rejects_unbounded_or_incomplete_keysets(
    kwargs: dict[str, object],
) -> None:
    with pytest.raises(RuntimeApiConfigurationError):
        AgentRunReadRequest(**kwargs)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_list_is_minimal_bounded_and_newest_first() -> None:
    service, runs, cancellations = _service(_records(3))

    response = await service.handle(_request())

    assert response.status_code == 200
    payload = _payload(response)
    assert [item["run_id"] for item in payload["items"]] == [
        "run_0000",
        "run_0001",
        "run_0002",
    ]
    assert set(payload["items"][0]) == {
        "cancellable",
        "elapsed",
        "finished_at",
        "generation",
        "request_id",
        "run_id",
        "started_at",
        "state",
    }
    assert payload["next_cursor"] is None
    assert "sensitive-user" not in response.body.decode("utf-8")
    assert "sensitive-group" not in response.body.decode("utf-8")
    assert runs.list_calls == [AgentRunReadRequest(limit=21)]
    assert not runs.get_calls
    assert not cancellations.calls


@pytest.mark.asyncio
async def test_detail_exposes_only_bounded_run_metadata() -> None:
    service, runs, _ = _service()

    response = await service.handle(_request("/agent-runs/run_0001"))

    assert response.status_code == 200
    assert _payload(response) == {
        "api_version": 1,
        "run": {
            "cancellable": True,
            "elapsed": None,
            "finished_at": None,
            "generation": 7,
            "group_id": "sensitive-group:20002",
            "request_id": 17,
            "run_id": "run_0001",
            "started_at": 100.25,
            "state": "executing",
            "user_id": "sensitive-user:10001",
        },
    }
    assert b"input" not in response.body
    assert b"output" not in response.body
    assert b"arguments" not in response.body
    assert runs.get_calls == ["run_0001"]


@pytest.mark.asyncio
async def test_detail_supports_private_terminal_run() -> None:
    record = _run(
        group_id=None,
        state=AgentRunState.COMPLETED,
        started_at=20.0,
        finished_at=22.5,
    )
    service, _, _ = _service((record,))

    response = await service.handle(_request("/agent-runs/run_0001"))

    detail = _payload(response)["run"]
    assert detail["group_id"] is None
    assert detail["cancellable"] is False
    assert detail["elapsed"] == 2.5


@pytest.mark.asyncio
async def test_unknown_detail_returns_not_found() -> None:
    service, runs, _ = _service(())

    response = await service.handle(_request("/agent-runs/run_missing"))

    assert response.status_code == 404
    assert _payload(response)["error"] == "not_found"
    assert runs.get_calls == ["run_missing"]


@pytest.mark.asyncio
async def test_list_uses_canonical_stable_keyset_cursor() -> None:
    service, runs, _ = _service(_records(13))

    first = await service.handle(_request(query_string=b"limit=5"))
    first_payload = _payload(first)
    cursor = first_payload["next_cursor"]
    second = await service.handle(
        _request(
            query_string=f"cursor={cursor}&limit=5".encode("ascii"),
        )
    )
    second_payload = _payload(second)

    assert [item["run_id"] for item in first_payload["items"]] == [f"run_{index:04d}" for index in range(5)]
    assert [item["run_id"] for item in second_payload["items"]] == [f"run_{index:04d}" for index in range(5, 10)]
    assert runs.list_calls[0] == AgentRunReadRequest(limit=6)
    assert runs.list_calls[1] == AgentRunReadRequest(
        limit=6,
        before_started_at=996.0,
        before_run_id="run_0004",
    )
    assert isinstance(cursor, str)
    assert "=" not in cursor


@pytest.mark.asyncio
async def test_default_page_is_capped_at_twenty() -> None:
    service, runs, _ = _service(_records(25))

    response = await service.handle(_request())

    payload = _payload(response)
    assert len(payload["items"]) == 20
    assert payload["next_cursor"] is not None
    assert runs.list_calls == [AgentRunReadRequest(limit=21)]


@pytest.mark.asyncio
async def test_keyset_handles_equal_timestamps_by_descending_run_id() -> None:
    records = (
        _run(run_id="run_c", request_id=3, started_at=10.0),
        _run(run_id="run_b", request_id=2, started_at=10.0),
        _run(run_id="run_a", request_id=1, started_at=10.0),
    )
    service, _, _ = _service(records)

    first = await service.handle(_request(query_string=b"limit=2"))
    cursor = _payload(first)["next_cursor"]
    second = await service.handle(_request(query_string=f"limit=2&cursor={cursor}".encode("ascii")))

    assert [item["run_id"] for item in _payload(first)["items"]] == [
        "run_c",
        "run_b",
    ]
    assert [item["run_id"] for item in _payload(second)["items"]] == ["run_a"]


@pytest.mark.parametrize(
    "query",
    [
        b"limit=0",
        b"limit=21",
        b"limit=01",
        b"limit=+1",
        b"limit=1&limit=2",
        b"unknown=1",
        b"cursor=",
        b"cursor=%41",
        b"cursor=not-a-canonical-cursor",
        b"limit=1&cursor=a&extra=b",
        b"limit",
        b"limit=1=2",
        "limit=一".encode(),
    ],
)
@pytest.mark.asyncio
async def test_invalid_list_query_fails_before_reader(query: bytes) -> None:
    service, runs, _ = _service()

    response = await service.handle(_request(query_string=query))

    assert response.status_code == 400
    assert _payload(response)["error"] == "invalid_query"
    assert not runs.list_calls
    assert not runs.get_calls


@pytest.mark.parametrize(
    "invalid_result",
    [
        [],
        (_run(),) * 22,
        (object(),),
        (
            _run(run_id="run_old", request_id=1, started_at=10.0),
            _run(run_id="run_new", request_id=2, started_at=11.0),
        ),
        (
            _run(
                request_id=_MAX_BIGINT + 1,
            ),
        ),
        (
            _run(
                generation=_MAX_BIGINT + 1,
            ),
        ),
    ],
)
@pytest.mark.asyncio
async def test_reader_page_contract_fails_closed(invalid_result: object) -> None:
    service, runs, _ = _service()
    runs.list_result = invalid_result

    response = await service.handle(_request())

    assert response.status_code == 503
    assert _payload(response)["error"] == "agent_runs_unavailable"


@pytest.mark.asyncio
async def test_reader_must_honor_cursor_anchor() -> None:
    service, runs, _ = _service(_records(3))
    first = await service.handle(_request(query_string=b"limit=1"))
    cursor = _payload(first)["next_cursor"]
    runs.list_result = (_records(3)[0],)

    second = await service.handle(_request(query_string=f"limit=1&cursor={cursor}".encode("ascii")))

    assert second.status_code == 503
    assert _payload(second)["error"] == "agent_runs_unavailable"


@pytest.mark.parametrize(
    "invalid_result",
    [
        object(),
        _run(run_id="run_other"),
        _run(request_id=_MAX_BIGINT + 1),
        _run(generation=_MAX_BIGINT + 1),
    ],
)
@pytest.mark.asyncio
async def test_detail_reader_contract_fails_closed(
    invalid_result: object,
) -> None:
    service, runs, _ = _service()
    runs.get_result = invalid_result

    response = await service.handle(_request("/agent-runs/run_0001"))

    assert response.status_code == 503
    assert _payload(response)["error"] == "agent_runs_unavailable"


@pytest.mark.asyncio
async def test_reader_failure_is_sanitized() -> None:
    service, runs, _ = _service()
    runs.list_error = RuntimeError("postgres://secret-host/private")

    response = await service.handle(_request())

    assert response.status_code == 503
    assert _payload(response)["error"] == "agent_runs_unavailable"
    assert b"secret-host" not in response.body


@pytest.mark.parametrize("token", [None, _WRONG_TOKEN])
@pytest.mark.asyncio
async def test_missing_or_wrong_token_never_reads_state(
    token: str | None,
) -> None:
    service, runs, cancellations = _service()

    response = await service.handle(_request(token=token))

    assert response.status_code == 401
    assert response.extra_headers == ((b"www-authenticate", b'Bearer realm="moellm-runtime"'),)
    assert not runs.list_calls
    assert not runs.get_calls
    assert not cancellations.calls


@pytest.mark.asyncio
async def test_read_and_write_scopes_do_not_imply_each_other() -> None:
    read_service, read_runs, read_cancellations = _service(principal=_principal(AGENT_RUN_API_READ_SCOPE))
    write_service, write_runs, write_cancellations = _service(principal=_principal(AGENT_RUN_API_WRITE_SCOPE))

    denied_write = await read_service.handle(
        _request(
            "/agent-runs/run_0001/cancel",
            method="POST",
            content_type="application/json",
            body=_cancel_body(),
        )
    )
    denied_read = await write_service.handle(_request())

    assert denied_write.status_code == 403
    assert denied_read.status_code == 403
    assert not read_runs.get_calls
    assert not read_cancellations.calls
    assert not write_runs.list_calls
    assert not write_cancellations.calls


@pytest.mark.asyncio
async def test_authenticator_failure_and_invalid_principal_fail_closed() -> None:
    broken = _FixedAuthenticator(_principal(AGENT_RUN_API_READ_SCOPE))
    broken.error = RuntimeError("credential backend secret")
    broken_service, broken_runs, _ = _service(authenticator=broken)
    invalid = _FixedAuthenticator(object())
    invalid_service, invalid_runs, _ = _service(authenticator=invalid)

    broken_response = await broken_service.handle(_request())
    invalid_response = await invalid_service.handle(_request())

    assert broken_response.status_code == 503
    assert invalid_response.status_code == 503
    assert _payload(broken_response)["error"] == "authentication_unavailable"
    assert _payload(invalid_response)["error"] == "authentication_unavailable"
    assert not broken_runs.list_calls
    assert not invalid_runs.list_calls


@pytest.mark.parametrize(
    ("api_request", "status", "error"),
    [
        (_request("/agent-run"), 404, "not_found"),
        (_request(method="POST"), 405, "method_not_allowed"),
        (
            _request("/agent-runs/run_0001", query_string=b"limit=1"),
            400,
            "query_not_supported",
        ),
        (
            _request(content_type="application/json"),
            400,
            "body_not_supported",
        ),
        (
            _request(body=b"{}"),
            400,
            "body_not_supported",
        ),
        (
            _request(
                "/agent-runs/run_0001/cancel",
                method="POST",
                query_string=b"x=1",
                content_type="application/json",
                body=_cancel_body(),
            ),
            400,
            "query_not_supported",
        ),
        (
            _request(
                "/agent-runs/run_0001/cancel",
                method="POST",
                body=_cancel_body(),
            ),
            415,
            "unsupported_media_type",
        ),
        (
            _request(
                "/agent-runs/run_0001/cancel",
                method="POST",
                content_type="text/plain",
                body=_cancel_body(),
            ),
            415,
            "unsupported_media_type",
        ),
    ],
)
@pytest.mark.asyncio
async def test_endpoint_and_transport_validation_precede_state_reads(
    api_request: RuntimeApiRequest,
    status: int,
    error: str,
) -> None:
    service, runs, cancellations = _service()

    response = await service.handle(api_request)

    assert response.status_code == status
    assert _payload(response)["error"] == error
    assert not runs.list_calls
    assert not runs.get_calls
    assert not cancellations.calls


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"[]",
        b"{}",
        b'{"expected_generation":7,"expected_state":"executing","x":1}',
        b'{"expected_generation":7,"expected_generation":7,"expected_state":"executing"}',
        b'{"expected_generation":true,"expected_state":"executing"}',
        b'{"expected_generation":-1,"expected_state":"executing"}',
        b'{"expected_generation":9223372036854775808,"expected_state":"executing"}',
        b'{"expected_generation":7,"expected_state":"unknown"}',
        b'{"expected_generation":7,"expected_state":"completed"}',
        b'{"expected_generation":7,"expected_state":NaN}',
        b"{" + b" " * 2_048 + b"}",
        b"\xff",
    ],
)
@pytest.mark.asyncio
async def test_invalid_cancel_body_fails_before_state_read(body: bytes) -> None:
    service, runs, cancellations = _service()

    response = await service.handle(
        _request(
            "/agent-runs/run_0001/cancel",
            method="POST",
            content_type="application/json",
            body=body,
        )
    )

    assert response.status_code == 400
    assert _payload(response)["error"] == "invalid_request"
    assert not runs.get_calls
    assert not cancellations.calls


@pytest.mark.asyncio
async def test_cancel_uses_authenticated_actor_and_exact_state_generation_cas() -> None:
    current = _run(state=AgentRunState.WAITING_CONFIRMATION, generation=9)
    service, runs, cancellations = _service((current,))

    response = await service.handle(
        _request(
            "/agent-runs/run_0001/cancel",
            method="POST",
            content_type="application/json",
            body=_cancel_body(
                state=AgentRunState.WAITING_CONFIRMATION,
                generation=9,
            ),
        )
    )

    assert response.status_code == 200
    assert cancellations.calls == [
        CancelAgentRunCommand(
            actor_subject="run-admin",
            run_id="run_0001",
            expected_state=AgentRunState.WAITING_CONFIRMATION,
            expected_generation=9,
        )
    ]
    assert runs.get_calls == ["run_0001"]
    assert _payload(response) == {
        "api_version": 1,
        "audit_recorded": True,
        "cancellation_settled": True,
        "operation": "cancel_agent_run",
        "operation_id": _OPERATION_ID,
        "run": {
            "cancellable": False,
            "elapsed": 1.5,
            "finished_at": 101.75,
            "generation": 9,
            "group_id": "sensitive-group:20002",
            "request_id": 17,
            "run_id": "run_0001",
            "started_at": 100.25,
            "state": "cancelled",
            "user_id": "sensitive-user:10001",
        },
    }


@pytest.mark.parametrize(
    ("current", "body"),
    [
        (_run(state=AgentRunState.EXECUTING), _cancel_body(state=AgentRunState.PLANNING)),
        (_run(generation=8), _cancel_body(generation=7)),
        (
            _run(state=AgentRunState.COMPLETED),
            _cancel_body(state=AgentRunState.EXECUTING),
        ),
    ],
)
@pytest.mark.asyncio
async def test_cancel_rejects_stale_or_terminal_run_without_calling_port(
    current: AgentRun,
    body: bytes,
) -> None:
    service, _, cancellations = _service((current,))

    response = await service.handle(
        _request(
            "/agent-runs/run_0001/cancel",
            method="POST",
            content_type="application/json",
            body=body,
        )
    )

    assert response.status_code == 409
    assert _payload(response)["error"] == "mutation_precondition_failed"
    assert not cancellations.calls


@pytest.mark.asyncio
async def test_cancel_unknown_run_returns_not_found_without_port_call() -> None:
    service, runs, cancellations = _service(())

    response = await service.handle(
        _request(
            "/agent-runs/run_missing/cancel",
            method="POST",
            content_type="application/json",
            body=_cancel_body(),
        )
    )

    assert response.status_code == 404
    assert runs.get_calls == ["run_missing"]
    assert not cancellations.calls


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (AgentRunCancellationNotFoundError(), 404, "not_found"),
        (
            AgentRunCancellationConflictError(),
            409,
            "mutation_precondition_failed",
        ),
        (
            AgentRunCancellationUnavailableError(),
            503,
            "mutation_unavailable",
        ),
        (RuntimeError("secret cancellation backend"), 503, "mutation_unavailable"),
    ],
)
@pytest.mark.asyncio
async def test_cancel_maps_port_failures_without_leaking_details(
    error: BaseException,
    status: int,
    code: str,
) -> None:
    service, _, cancellations = _service()
    cancellations.error = error

    response = await service.handle(
        _request(
            "/agent-runs/run_0001/cancel",
            method="POST",
            content_type="application/json",
            body=_cancel_body(),
        )
    )

    assert response.status_code == status
    assert _payload(response)["error"] == code
    assert b"secret" not in response.body
    assert len(cancellations.calls) == 1


@pytest.mark.asyncio
async def test_unknown_cancel_result_is_non_retryable_and_not_replayed() -> None:
    service, _, cancellations = _service()
    cancellations.error = AgentRunCancellationResultUnknownError("commit acknowledgement lost")

    response = await service.handle(
        _request(
            "/agent-runs/run_0001/cancel",
            method="POST",
            content_type="application/json",
            body=_cancel_body(),
        )
    )

    assert response.status_code == 409
    assert _payload(response) == {
        "api_version": 1,
        "error": "mutation_result_unknown",
        "retryable": False,
    }
    assert len(cancellations.calls) == 1


def _cancelled_from(current: AgentRun, **overrides: object) -> AgentRun:
    values: dict[str, object] = {
        "run_id": current.run_id,
        "request_id": current.request_id,
        "user_id": current.user_id,
        "group_id": current.group_id,
        "generation": current.generation,
        "state": AgentRunState.CANCELLED,
        "started_at": current.started_at,
        "finished_at": current.started_at + 1.0,
    }
    values.update(overrides)
    return AgentRun(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "result_factory",
    [
        lambda current: object(),
        lambda current: CancelAgentRunResult(
            _OPERATION_ID,
            AgentRunState.PLANNING,
            _cancelled_from(current),
            True,
            True,
        ),
        lambda current: CancelAgentRunResult(
            _OPERATION_ID,
            current.state,
            _cancelled_from(current, run_id="run_other"),
            True,
            True,
        ),
        lambda current: CancelAgentRunResult(
            _OPERATION_ID,
            current.state,
            _cancelled_from(current, request_id=99),
            True,
            True,
        ),
        lambda current: CancelAgentRunResult(
            _OPERATION_ID,
            current.state,
            _cancelled_from(current, user_id="other-user"),
            True,
            True,
        ),
        lambda current: CancelAgentRunResult(
            _OPERATION_ID,
            current.state,
            _cancelled_from(current, group_id="other-group"),
            True,
            True,
        ),
        lambda current: CancelAgentRunResult(
            _OPERATION_ID,
            current.state,
            _cancelled_from(current, generation=8),
            True,
            True,
        ),
        lambda current: CancelAgentRunResult(
            _OPERATION_ID,
            current.state,
            _cancelled_from(current, started_at=99.0, finished_at=100.0),
            True,
            True,
        ),
    ],
)
@pytest.mark.asyncio
async def test_cancel_rejects_invalid_or_identity_drifting_result(
    result_factory: Any,
) -> None:
    current = _run()
    service, _, cancellations = _service((current,))
    cancellations.result = result_factory(current)

    response = await service.handle(
        _request(
            "/agent-runs/run_0001/cancel",
            method="POST",
            content_type="application/json",
            body=_cancel_body(),
        )
    )

    assert response.status_code == 503
    assert _payload(response)["error"] == "mutation_unavailable"
    assert len(cancellations.calls) == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"actor_subject": "bad actor"},
        {"run_id": "bad run"},
        {"expected_state": AgentRunState.COMPLETED},
        {"expected_generation": -1},
        {"expected_generation": _MAX_BIGINT + 1},
    ],
)
def test_cancel_command_rejects_unsafe_identity_or_cas(
    kwargs: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "actor_subject": "run-admin",
        "run_id": "run_0001",
        "expected_state": AgentRunState.EXECUTING,
        "expected_generation": 7,
    }
    values.update(kwargs)
    with pytest.raises(RuntimeApiConfigurationError):
        CancelAgentRunCommand(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"operation_id": "short"},
        {"previous_state": AgentRunState.COMPLETED},
        {"run": _run(state=AgentRunState.COMPLETED)},
        {"cancellation_settled": False},
        {"audit_recorded": False},
    ],
)
def test_cancel_result_requires_settled_terminal_state_and_audit(
    kwargs: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "operation_id": _OPERATION_ID,
        "previous_state": AgentRunState.EXECUTING,
        "run": _run(state=AgentRunState.CANCELLED),
        "cancellation_settled": True,
        "audit_recorded": True,
    }
    values.update(kwargs)
    with pytest.raises(RuntimeApiConfigurationError):
        CancelAgentRunResult(**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_caller_cancellation_propagates_from_reader_and_port() -> None:
    list_service, list_runs, _ = _service()
    list_runs.list_error = asyncio.CancelledError()
    get_service, get_runs, _ = _service()
    get_runs.get_error = asyncio.CancelledError()
    cancel_service, _, cancellations = _service()
    cancellations.error = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await list_service.handle(_request())
    with pytest.raises(asyncio.CancelledError):
        await get_service.handle(_request("/agent-runs/run_0001"))
    with pytest.raises(asyncio.CancelledError):
        await cancel_service.handle(
            _request(
                "/agent-runs/run_0001/cancel",
                method="POST",
                content_type="application/json",
                body=_cancel_body(),
            )
        )


@pytest.mark.asyncio
async def test_asgi_adapter_can_serve_detail_and_cancel_without_global_mount() -> None:
    service, _, _ = _service()
    app = RuntimeApiASGIApp(service=service)

    detail_status, detail_headers, detail = await _call_asgi(
        app,
        method="GET",
        path="/agent-runs/run_0001",
    )
    body = _cancel_body()
    cancel_status, cancel_headers, cancel = await _call_asgi(
        app,
        method="POST",
        path="/agent-runs/run_0001/cancel",
        body=body,
        content_type=b"application/json",
    )

    assert detail_status == 200
    assert detail["run"]["run_id"] == "run_0001"
    assert cancel_status == 200
    assert cancel["run"]["state"] == "cancelled"
    for headers in (detail_headers, cancel_headers):
        assert headers[b"cache-control"] == b"no-store"
        assert headers[b"x-content-type-options"] == b"nosniff"
        assert b"access-control-allow-origin" not in headers


def test_module_has_no_service_app_reader_or_cancellation_singleton() -> None:
    module = importlib.import_module("nonebot_plugin_moellmchats.agent_run_api")

    assert not any(isinstance(value, AgentRunApiService) for value in vars(module).values())
    for name in (
        "agent_run_api_service",
        "agent_run_api_app",
        "agent_run_reader",
        "agent_run_cancellations",
    ):
        assert not hasattr(module, name)

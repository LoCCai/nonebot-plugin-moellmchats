from __future__ import annotations

from dataclasses import dataclass, replace
import json
import socket
from typing import Any

import pytest

from nonebot_plugin_moellmchats.agent_run_api import (
    AGENT_RUN_API_READ_SCOPE,
    AGENT_RUN_API_WRITE_SCOPE,
    AgentRunReadRequest,
    CancelAgentRunCommand,
    CancelAgentRunResult,
)
from nonebot_plugin_moellmchats.agent_runtime import AgentRun, AgentRunState
from nonebot_plugin_moellmchats.generated_tool_lifecycle import (
    LifecycleState,
    VersionRecord,
    VersionState,
)
from nonebot_plugin_moellmchats.metrics_api import (
    METRICS_API_READ_SCOPE,
    MODEL_API_READ_SCOPE,
)
from nonebot_plugin_moellmchats.model_selector import ModelRuntimeState
from nonebot_plugin_moellmchats.platform_api import (
    PlatformApiConfigurationError,
    PlatformApiMounts,
    PlatformApiSettings,
    build_platform_api_mounts,
)
from nonebot_plugin_moellmchats.platform_metrics import PlatformMetricsRegistry
from nonebot_plugin_moellmchats.runtime_api import (
    RUNTIME_API_READ_SCOPE,
    RuntimeApiASGIApp,
    RuntimeApiCredential,
    RuntimeApiPrincipal,
    RuntimeApiRequest,
    RuntimeApiResponse,
    StaticBearerRuntimeApiAuthenticator,
)
from nonebot_plugin_moellmchats.runtime_resources import (
    RuntimeGenerationResourceState,
    RuntimeResourceBuilder,
    RuntimeResourceSettings,
)
from nonebot_plugin_moellmchats.runtime_snapshot import RuntimeSnapshot
from nonebot_plugin_moellmchats.tool_bundle_api import (
    TOOL_BUNDLE_API_READ_SCOPE,
    TOOL_BUNDLE_API_WRITE_SCOPE,
    ActivateToolBundleCommand,
    ApproveToolDraftCommand,
    ToolBundleMutationResult,
)
from nonebot_plugin_moellmchats.tool_manager import ToolSnapshot
from nonebot_plugin_moellmchats.tool_providers import ProviderCatalogSnapshot
from nonebot_plugin_moellmchats.web_admin import WebAdminASGIApp, WebAdminConfig

_TOKEN = "p" * 32
_WRONG_TOKEN = "w" * 32
_BUNDLE_ID = "SafeBundle"
_BUNDLE_DIGEST = "a" * 64
_NEXT_STATE_DIGEST = "b" * 64
_OPERATION_ID = "c" * 64
_ALL_SCOPES = (
    RUNTIME_API_READ_SCOPE,
    TOOL_BUNDLE_API_READ_SCOPE,
    TOOL_BUNDLE_API_WRITE_SCOPE,
    AGENT_RUN_API_READ_SCOPE,
    AGENT_RUN_API_WRITE_SCOPE,
    MODEL_API_READ_SCOPE,
    METRICS_API_READ_SCOPE,
)


def _lifecycle() -> LifecycleState:
    return LifecycleState(
        revision=1,
        drafts={},
        versions={
            _BUNDLE_ID: {
                _BUNDLE_DIGEST: VersionRecord(
                    bundle_id=_BUNDLE_ID,
                    digest=_BUNDLE_DIGEST,
                    state=VersionState.APPROVED,
                    source_draft_id="draft000001",
                    created_at=1,
                    approved_at=2,
                )
            }
        },
        active={},
        permission_grants={},
    )


def _snapshot(
    state: LifecycleState,
    *,
    generation: int = 7,
) -> RuntimeSnapshot:
    tool_snapshot = ToolSnapshot(
        generation=generation,
        plugin_info={},
        custom_tools={},
        tool_dependencies={},
        mcp_tool_names=frozenset(),
        provider_catalog=ProviderCatalogSnapshot.empty(generation),
        generated_state_revision=state.revision,
        generated_state_digest=state.state_digest,
        generated_active=state.active,
    )
    model_state = ModelRuntimeState(
        models={
            "safe-model (safe-provider)": {
                "model": "safe-model",
                "provider": "safe-provider",
                "key": ["model-secret-must-not-leak"],
                "url": "https://model-secret.invalid",
            }
        },
        providers={
            "safe-provider": {
                "api_key": "provider-secret-must-not-leak",
            }
        },
        global_default={"secret": "global-secret-must-not-leak"},
        model_config={"secret": "model-config-secret-must-not-leak"},
    )
    return RuntimeSnapshot(
        generation=generation,
        config={
            "database_url": "postgresql://private:secret@database.invalid/private",
            "sql": "SELECT private_payload FROM secret_table",
            "payload": "request-payload-must-not-leak",
        },
        model_state=model_state,
        temperaments={},
        temperament_assignments={},
        replies={},
        tool_snapshot=tool_snapshot,
        emotions=(),
        reloaded_at=10.0,
        generated_state_revision=state.revision,
        generated_state_digest=state.state_digest,
        generated_active=state.active,
    )


def _run() -> AgentRun:
    return AgentRun(
        run_id="run_0001",
        request_id=17,
        user_id="user_0001",
        group_id="group_0001",
        conversation_id="conversation_0001",
        generation=7,
        state=AgentRunState.EXECUTING,
        started_at=100.25,
        finished_at=None,
    )


def _credential(token: str = _TOKEN) -> RuntimeApiCredential:
    credential = RuntimeApiCredential.from_authorization_header(f"Bearer {token}".encode("ascii"))
    assert credential is not None
    return credential


def _request(
    path: str,
    *,
    method: str = "GET",
    token: str | None = _TOKEN,
    body: bytes = b"",
    content_type: str | None = None,
) -> RuntimeApiRequest:
    return RuntimeApiRequest(
        method=method,
        path=path,
        credential=None if token is None else _credential(token),
        body=body,
        content_type=content_type,
    )


def _payload(response: RuntimeApiResponse) -> dict[str, Any]:
    value = json.loads(response.body)
    assert isinstance(value, dict)
    return value


class _LifecycleReader:
    def __init__(self, state: LifecycleState) -> None:
        self.state = state
        self.calls = 0

    async def read_current(self) -> LifecycleState:
        self.calls += 1
        return self.state


class _Mutations:
    def __init__(self) -> None:
        self.approvals: list[ApproveToolDraftCommand] = []
        self.activations: list[ActivateToolBundleCommand] = []

    async def approve_draft(
        self,
        command: ApproveToolDraftCommand,
    ) -> ToolBundleMutationResult:
        self.approvals.append(command)
        raise AssertionError("platform composition test has no approvable draft")

    async def activate_bundle(
        self,
        command: ActivateToolBundleCommand,
    ) -> ToolBundleMutationResult:
        self.activations.append(command)
        return ToolBundleMutationResult(
            operation="activate_bundle",
            operation_id=_OPERATION_ID,
            generation=command.expected_generation + 1,
            lifecycle_revision=command.expected_lifecycle_revision + 1,
            lifecycle_state_digest=_NEXT_STATE_DIGEST,
            bundle_id=command.bundle_id,
            digest=command.digest,
            draft_id=None,
            active_digest=command.digest,
            audit_recorded=True,
        )


class _Runs:
    def __init__(self, record: AgentRun) -> None:
        self.record = record
        self.list_calls: list[AgentRunReadRequest] = []
        self.get_calls: list[str] = []

    async def list_runs(
        self,
        request: AgentRunReadRequest,
    ) -> tuple[AgentRun, ...]:
        self.list_calls.append(request)
        return (self.record,)[: request.limit]

    async def get_run(self, run_id: str) -> AgentRun | None:
        self.get_calls.append(run_id)
        return self.record if run_id == self.record.run_id else None


class _Cancellations:
    def __init__(self, runs: _Runs) -> None:
        self.runs = runs
        self.calls: list[CancelAgentRunCommand] = []

    async def cancel_run(
        self,
        command: CancelAgentRunCommand,
    ) -> CancelAgentRunResult:
        self.calls.append(command)
        return CancelAgentRunResult(
            operation_id=_OPERATION_ID,
            previous_state=self.runs.record.state,
            run=replace(
                self.runs.record,
                state=AgentRunState.CANCELLED,
                finished_at=self.runs.record.started_at + 1.0,
            ),
            cancellation_settled=True,
            audit_recorded=True,
        )


@dataclass
class _PlatformPorts:
    settings: PlatformApiSettings
    lifecycle: _LifecycleReader
    mutations: _Mutations
    runs: _Runs
    cancellations: _Cancellations


def _ports(
    state: LifecycleState,
    *,
    scopes: tuple[str, ...] = _ALL_SCOPES,
) -> _PlatformPorts:
    lifecycle = _LifecycleReader(state)
    mutations = _Mutations()
    runs = _Runs(_run())
    cancellations = _Cancellations(runs)
    authenticator = StaticBearerRuntimeApiAuthenticator(
        token=_TOKEN,
        principal=RuntimeApiPrincipal(
            subject="platform-admin",
            scopes=frozenset(scopes),
        ),
    )
    return _PlatformPorts(
        settings=PlatformApiSettings(
            authenticator=authenticator,
            tool_lifecycle=lifecycle,
            tool_mutations=mutations,
            agent_runs=runs,
            agent_cancellations=cancellations,
            admin=WebAdminConfig(
                base_path="/internal-admin",
                api_prefix="/internal-api",
            ),
        ),
        lifecycle=lifecycle,
        mutations=mutations,
        runs=runs,
        cancellations=cancellations,
    )


def _mounts(
    state: LifecycleState,
    ports: _PlatformPorts,
    *,
    generation: int = 7,
) -> PlatformApiMounts:
    return build_platform_api_mounts(
        snapshot=_snapshot(state, generation=generation),
        metrics=PlatformMetricsRegistry(
            generation=generation,
            pid_getter=lambda: 12_345,
        ),
        settings=ports.settings,
    )


async def _call_api_app(
    app: RuntimeApiASGIApp,
    *,
    path: str,
    token: str = _TOKEN,
) -> tuple[int, dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "query_string": b"",
            "headers": [
                (b"authorization", f"Bearer {token}".encode("ascii")),
            ],
        },
        receive,
        send,
    )
    assert len(sent) == 2
    return sent[0]["status"], json.loads(sent[1]["body"])


async def _call_admin_app(
    app: WebAdminASGIApp,
    *,
    method: str,
    path: str,
) -> tuple[int, bytes]:
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(
        {
            "type": "http",
            "method": method,
            "path": path,
            "query_string": b"",
            "headers": [],
        },
        receive,
        send,
    )
    assert len(sent) == 2
    return sent[0]["status"], sent[1]["body"]


def test_settings_are_explicit_structural_and_redacted() -> None:
    state = _lifecycle()
    ports = _ports(state)

    assert "configured=True" in repr(ports.settings)
    assert _TOKEN not in repr(ports.settings)
    with pytest.raises(PlatformApiConfigurationError, match="authenticator"):
        PlatformApiSettings(
            authenticator=object(),  # type: ignore[arg-type]
            tool_lifecycle=ports.lifecycle,
            tool_mutations=ports.mutations,
            agent_runs=ports.runs,
            agent_cancellations=ports.cancellations,
        )


def test_mount_builder_rejects_cross_generation_metrics() -> None:
    state = _lifecycle()
    ports = _ports(state)

    with pytest.raises(PlatformApiConfigurationError, match="generation"):
        build_platform_api_mounts(
            snapshot=_snapshot(state, generation=7),
            metrics=PlatformMetricsRegistry(
                generation=8,
                pid_getter=lambda: 12_345,
            ),
            settings=ports.settings,
        )


@pytest.mark.asyncio
async def test_fixed_h01_through_h04_routes_share_one_generation_and_redact() -> None:
    state = _lifecycle()
    ports = _ports(state)
    mounts = _mounts(state, ports)
    responses: dict[str, dict[str, Any]] = {}

    for path in (
        "/runtime/status",
        "/runtime/generation",
        "/tools",
        "/tool-bundles",
        "/tool-drafts",
        "/agent-runs",
        "/agent-runs/run_0001",
        "/models",
        "/metrics",
    ):
        status, payload = await _call_api_app(mounts.api_app, path=path)
        assert status == 200, (path, payload)
        responses[path] = payload

    top_level_generation_paths = (
        "/runtime/status",
        "/runtime/generation",
        "/tools",
        "/tool-bundles",
        "/tool-drafts",
        "/models",
        "/metrics",
    )
    assert {responses[path]["generation"] for path in top_level_generation_paths} == {7}
    assert {item["generation"] for item in responses["/agent-runs"]["items"]} == {7}
    assert responses["/agent-runs/run_0001"]["run"]["generation"] == 7
    assert responses["/metrics"]["database"]["pool"] == {
        "active": 0,
        "peak": 0,
        "wait_duration": {
            "count": 0,
            "maximum_seconds": None,
            "minimum_seconds": None,
            "total_seconds": 0.0,
        },
    }
    metrics_text = json.dumps(responses["/metrics"], sort_keys=True)
    assert not any(forbidden in metrics_text for forbidden in ("labels", "user_id", "group_id", "run_id", "sql", "payload"))
    combined = json.dumps(responses, sort_keys=True)
    assert not any(
        forbidden in combined
        for forbidden in (
            _TOKEN,
            "postgresql://",
            "SELECT private_payload",
            "request-payload-must-not-leak",
            "model-secret-must-not-leak",
            "provider-secret-must-not-leak",
            "global-secret-must-not-leak",
            "model-config-secret-must-not-leak",
        )
    )
    unknown = await mounts.router.handle(_request("/unknown"))
    assert unknown.status_code == 404


@pytest.mark.asyncio
async def test_authentication_and_each_read_scope_precede_injected_ports() -> None:
    state = _lifecycle()
    ports = _ports(state, scopes=(RUNTIME_API_READ_SCOPE,))
    mounts = _mounts(state, ports)

    unauthorized = await mounts.router.handle(_request("/runtime/status", token=_WRONG_TOKEN))
    assert unauthorized.status_code == 401
    for path in ("/tools", "/agent-runs", "/models", "/metrics"):
        response = await mounts.router.handle(_request(path))
        assert response.status_code == 403, path

    assert ports.lifecycle.calls == 0
    assert ports.runs.list_calls == []
    assert ports.runs.get_calls == []
    assert ports.mutations.activations == []
    assert ports.cancellations.calls == []


@pytest.mark.asyncio
async def test_mutations_still_cross_only_exact_double_cas_ports() -> None:
    state = _lifecycle()
    ports = _ports(state)
    mounts = _mounts(state, ports)
    activation_body = json.dumps(
        {
            "digest": _BUNDLE_DIGEST,
            "expected_generation": 7,
            "expected_lifecycle_revision": state.revision,
            "expected_lifecycle_state_digest": state.state_digest,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")

    activated = await mounts.router.handle(
        _request(
            f"/tool-bundles/{_BUNDLE_ID}/activate",
            method="POST",
            body=activation_body,
            content_type="application/json",
        )
    )
    assert activated.status_code == 200
    assert ports.mutations.activations == [
        ActivateToolBundleCommand(
            actor_subject="platform-admin",
            bundle_id=_BUNDLE_ID,
            digest=_BUNDLE_DIGEST,
            expected_generation=7,
            expected_lifecycle_revision=state.revision,
            expected_lifecycle_state_digest=state.state_digest,
        )
    ]

    stale_body = json.loads(activation_body)
    stale_body["expected_generation"] = 6
    stale = await mounts.router.handle(
        _request(
            f"/tool-bundles/{_BUNDLE_ID}/activate",
            method="POST",
            body=json.dumps(stale_body).encode("utf-8"),
            content_type="application/json",
        )
    )
    assert stale.status_code == 409
    assert len(ports.mutations.activations) == 1

    cancelled = await mounts.router.handle(
        _request(
            "/agent-runs/run_0001/cancel",
            method="POST",
            body=b'{"expected_generation":7,"expected_state":"executing"}',
            content_type="application/json",
        )
    )
    assert cancelled.status_code == 200
    assert ports.cancellations.calls == [
        CancelAgentRunCommand(
            actor_subject="platform-admin",
            run_id="run_0001",
            expected_state=AgentRunState.EXECUTING,
            expected_generation=7,
        )
    ]


@pytest.mark.asyncio
async def test_h05_admin_mount_is_static_read_only_and_keeps_token_in_memory() -> None:
    state = _lifecycle()
    mounts = _mounts(state, _ports(state))

    assert tuple(asset.path for asset in mounts.admin_service.assets) == (
        "/internal-admin",
        "/internal-admin/app.js",
        "/internal-admin/styles.css",
    )
    status, html = await _call_admin_app(
        mounts.admin_app,
        method="GET",
        path="/internal-admin",
    )
    assert status == 200
    assert _TOKEN.encode("ascii") not in html
    rejected, _body = await _call_admin_app(
        mounts.admin_app,
        method="POST",
        path="/internal-admin",
    )
    assert rejected == 405

    assets = b"\n".join(asset.body for asset in mounts.admin_service.assets)
    assert b"localStorage" not in assets
    assert b"sessionStorage" not in assets
    assert b"/approve" not in assets
    assert b"/activate" not in assets
    assert b"/cancel" not in assets


@pytest.mark.asyncio
async def test_runtime_builder_mounts_explicit_platform_without_backend_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _lifecycle()
    ports = _ports(state)
    socket_calls = 0

    def reject_socket(self: socket.socket, _address: object) -> None:
        nonlocal socket_calls
        del self
        socket_calls += 1
        raise AssertionError("platform API composition performed network I/O")

    monkeypatch.setattr(socket.socket, "connect", reject_socket)
    default_resources = RuntimeResourceBuilder().build(_snapshot(state))
    assert default_resources.platform_api_mounts is None
    assert default_resources.safe_diagnostics()["api_handler_count"] == 0

    resources = RuntimeResourceBuilder(RuntimeResourceSettings(platform_api=ports.settings)).build(_snapshot(state))
    assert resources.platform_api_mounts is not None
    assert resources.api_ports.handlers == (resources.platform_api_mounts.router,)
    assert resources.safe_diagnostics()["platform_api_mounted"] is True
    assert resources.state is RuntimeGenerationResourceState.CREATED
    assert resources.database_manager is None
    assert resources.redis_manager is None
    assert socket_calls == 0

    await resources.start()
    assert resources.state is RuntimeGenerationResourceState.RUNNING
    assert socket_calls == 0
    await resources.close()
    assert resources.state is RuntimeGenerationResourceState.CLOSED
    assert socket_calls == 0

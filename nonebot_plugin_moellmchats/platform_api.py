from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .agent_run_api import (
    AgentRunApiService,
    AgentRunCancellationPort,
    AgentRunStateReader,
)
from .metrics_api import MetricsApiService, RuntimeMetricsReader
from .platform_metrics import PlatformMetricsRegistry
from .runtime_api import (
    RuntimeApiASGIApp,
    RuntimeApiAuthenticator,
    RuntimeApiHandler,
    RuntimeApiRequest,
    RuntimeApiResponse,
    RuntimeApiService,
    RuntimeSnapshotReader,
)
from .runtime_snapshot import RuntimeSnapshot
from .tool_bundle_api import (
    ToolBundleApiService,
    ToolBundleMutationPort,
    ToolLifecycleStateReader,
)
from .web_admin import (
    WebAdminASGIApp,
    WebAdminConfig,
    WebAdminService,
)


class PlatformApiError(RuntimeError):
    """Base error for generation-local H-01 through H-05 composition."""


class PlatformApiConfigurationError(PlatformApiError):
    """Injected API ports do not preserve the detached primitive contracts."""


@dataclass(frozen=True, repr=False)
class PlatformApiSettings:
    authenticator: RuntimeApiAuthenticator
    tool_lifecycle: ToolLifecycleStateReader
    tool_mutations: ToolBundleMutationPort
    agent_runs: AgentRunStateReader
    agent_cancellations: AgentRunCancellationPort
    admin: WebAdminConfig = field(default_factory=WebAdminConfig)

    def __post_init__(self) -> None:
        checks = (
            (self.authenticator, RuntimeApiAuthenticator, "authenticator"),
            (self.tool_lifecycle, ToolLifecycleStateReader, "tool_lifecycle"),
            (self.tool_mutations, ToolBundleMutationPort, "tool_mutations"),
            (self.agent_runs, AgentRunStateReader, "agent_runs"),
            (self.agent_cancellations, AgentRunCancellationPort, "agent_cancellations"),
        )
        for value, contract, label in checks:
            if not isinstance(value, contract):
                raise PlatformApiConfigurationError(f"{label} 未实现安全 API port")
        if not isinstance(self.admin, WebAdminConfig):
            raise PlatformApiConfigurationError("admin 必须是 WebAdminConfig")

    def __repr__(self) -> str:
        return f"{type(self).__name__}(configured=True, admin_base_path={self.admin.base_path!r})"


class _BoundSnapshotReader(RuntimeSnapshotReader):
    def __init__(self, snapshot: RuntimeSnapshot) -> None:
        if not isinstance(snapshot, RuntimeSnapshot):
            raise TypeError("snapshot 必须是 RuntimeSnapshot")
        self._snapshot = snapshot

    def current(self) -> RuntimeSnapshot:
        return self._snapshot


class PlatformRuntimeMetricsReader(RuntimeMetricsReader):
    """Expose one closed PlatformMetrics snapshot as a detached mapping."""

    def __init__(self, registry: PlatformMetricsRegistry) -> None:
        if not isinstance(registry, PlatformMetricsRegistry):
            raise TypeError("registry 必须是 PlatformMetricsRegistry")
        self._registry = registry

    def snapshot(self) -> Mapping[str, Any]:
        return self._registry.snapshot().as_dict()


class PlatformApiRouter(RuntimeApiHandler):
    """Route only the fixed H-01 through H-04 authenticated API paths."""

    def __init__(
        self,
        *,
        runtime: RuntimeApiService,
        tools: ToolBundleApiService,
        runs: AgentRunApiService,
        metrics: MetricsApiService,
    ) -> None:
        services = (runtime, tools, runs, metrics)
        if any(not isinstance(service, RuntimeApiHandler) for service in services):
            raise PlatformApiConfigurationError("platform API service 非法")
        self._runtime = runtime
        self._tools = tools
        self._runs = runs
        self._metrics = metrics

    async def handle(self, request: RuntimeApiRequest) -> RuntimeApiResponse:
        if not isinstance(request, RuntimeApiRequest):
            return RuntimeApiResponse(
                status_code=400,
                payload={"api_version": 1, "error": "invalid_request"},
            )
        path = request.path
        if path.startswith("/runtime/"):
            service: RuntimeApiHandler = self._runtime
        elif (
            path == "/tools" or path.startswith("/tools/") or path.startswith("/tool-bundles") or path.startswith("/tool-drafts")
        ):
            service = self._tools
        elif path == "/agent-runs" or path.startswith("/agent-runs/"):
            service = self._runs
        elif path in {"/models", "/metrics"}:
            service = self._metrics
        else:
            return RuntimeApiResponse(
                status_code=404,
                payload={"api_version": 1, "error": "not_found"},
            )
        return await service.handle(request)


@dataclass(frozen=True)
class PlatformApiMounts:
    router: PlatformApiRouter
    api_app: RuntimeApiASGIApp
    admin_service: WebAdminService
    admin_app: WebAdminASGIApp

    def __post_init__(self) -> None:
        if not isinstance(self.router, PlatformApiRouter):
            raise PlatformApiConfigurationError("router 非法")
        if not isinstance(self.api_app, RuntimeApiASGIApp):
            raise PlatformApiConfigurationError("api_app 非法")
        if not isinstance(self.admin_service, WebAdminService):
            raise PlatformApiConfigurationError("admin_service 非法")
        if not isinstance(self.admin_app, WebAdminASGIApp):
            raise PlatformApiConfigurationError("admin_app 非法")


def build_platform_api_mounts(
    *,
    snapshot: RuntimeSnapshot,
    metrics: PlatformMetricsRegistry,
    settings: PlatformApiSettings,
) -> PlatformApiMounts:
    if not isinstance(settings, PlatformApiSettings):
        raise TypeError("settings 必须是 PlatformApiSettings")
    if not isinstance(snapshot, RuntimeSnapshot):
        raise TypeError("snapshot 必须是 RuntimeSnapshot")
    if not isinstance(metrics, PlatformMetricsRegistry):
        raise TypeError("metrics 必须是 PlatformMetricsRegistry")
    if metrics.generation != snapshot.generation:
        raise PlatformApiConfigurationError("metrics 与 runtime snapshot generation 不一致")
    snapshots = _BoundSnapshotReader(snapshot)
    runtime_service = RuntimeApiService(
        snapshots=snapshots,
        authenticator=settings.authenticator,
    )
    tool_service = ToolBundleApiService(
        snapshots=snapshots,
        lifecycle=settings.tool_lifecycle,
        authenticator=settings.authenticator,
        mutations=settings.tool_mutations,
    )
    run_service = AgentRunApiService(
        runs=settings.agent_runs,
        authenticator=settings.authenticator,
        cancellations=settings.agent_cancellations,
    )
    metrics_service = MetricsApiService(
        snapshots=snapshots,
        metrics=PlatformRuntimeMetricsReader(metrics),
        authenticator=settings.authenticator,
    )
    router = PlatformApiRouter(
        runtime=runtime_service,
        tools=tool_service,
        runs=run_service,
        metrics=metrics_service,
    )
    admin_service = WebAdminService(config=settings.admin)
    return PlatformApiMounts(
        router=router,
        api_app=RuntimeApiASGIApp(service=router),
        admin_service=admin_service,
        admin_app=WebAdminASGIApp(service=admin_service),
    )


__all__ = [
    "PlatformApiConfigurationError",
    "PlatformApiError",
    "PlatformApiMounts",
    "PlatformApiRouter",
    "PlatformApiSettings",
    "PlatformRuntimeMetricsReader",
    "build_platform_api_mounts",
]

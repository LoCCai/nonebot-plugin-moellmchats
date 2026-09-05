from __future__ import annotations

from types import SimpleNamespace

import pytest

from nonebot_plugin_moellmchats.builtin_tools import builtin_tool_specs
from nonebot_plugin_moellmchats.search import Search
from nonebot_plugin_moellmchats.tool_contracts import ToolPolicy, ToolSpec
from nonebot_plugin_moellmchats.tool_manager import ToolSnapshot
from nonebot_plugin_moellmchats.tool_providers import (
    DiscoveredTool,
    ProviderDiscoveryBatch,
    provider_registry,
)


async def _extractor_handler(url: str) -> str:
    return url


def _registered_snapshot(
    *,
    generation: int = 1,
    permission: str = "user",
    include_extractor: bool = True,
) -> ToolSnapshot:
    spec = ToolSpec(
        name="extract_webpage",
        description="extract a search result page",
        parameters={
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
        handler=_extractor_handler,
        permission=permission,
        policy=ToolPolicy.configured(),
    )
    batches: list[ProviderDiscoveryBatch] = []
    for registration in provider_registry.registrations:
        specs = (
            (spec,)
            if registration.provider_id == "registered" and include_extractor
            else builtin_tool_specs()
            if registration.provider_id == "builtin"
            else ()
        )
        records = tuple(
            DiscoveredTool(
                provider_id=registration.provider_id,
                source=registration.source,
                trust=registration.trust,
                generation=generation,
                spec=item,
            )
            for item in specs
        )
        batches.append(
            ProviderDiscoveryBatch(
                registration=registration,
                generation=generation,
                tools=records,
            )
        )
    catalog = provider_registry.build_snapshot(generation, tuple(batches))
    custom_tools = (
        {spec.name: {**spec.as_legacy_schema(), "source": "registered"}}
        if include_extractor
        else {}
    )
    return ToolSnapshot(
        generation=generation,
        plugin_info={},
        custom_tools=custom_tools,
        tool_dependencies={},
        mcp_tool_names=set(),
        provider_catalog=catalog,
    )


class _FakeResponse:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def json(self):
        return {
            "answer": "summary",
            "results": [
                {"title": "Source A", "url": "https://example.test/a"},
                {"title": "Source B", "url": "https://example.test/b"},
            ],
        }


class _FakeSession:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return _FakeResponse()


def _patch_search_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    *,
    provider_cutover: bool,
    blacklisted: bool,
) -> _FakeSession:
    from nonebot_plugin_moellmchats import search as search_module

    session = _FakeSession()
    monkeypatch.setattr(search_module, "get_session", lambda: session)

    def get_config(key: str, default=None):
        if key == "provider_catalog_search_enabled":
            return provider_cutover
        if key == "search_api":
            return "Bearer test"
        return default

    monkeypatch.setattr(search_module.config_parser, "get_config", get_config)
    monkeypatch.setattr(
        search_module.tool_manager,
        "is_tool_blacklisted",
        lambda name: blacklisted and name == "extract_webpage",
    )
    return session


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "permission",
        "is_superuser",
        "provider_cutover",
        "blacklisted",
        "expect_url",
    ),
    [
        ("user", False, True, False, True),
        ("superuser", False, True, False, False),
        ("superuser", True, True, False, True),
        ("user", False, True, True, False),
        # The independent rollback switch preserves the old membership-only
        # permission and blacklist behavior exactly.
        ("superuser", False, False, True, True),
    ],
)
async def test_search_extractor_selection_controls_source_url_disclosure(
    monkeypatch: pytest.MonkeyPatch,
    permission: str,
    is_superuser: bool,
    provider_cutover: bool,
    blacklisted: bool,
    expect_url: bool,
) -> None:
    snapshot = _registered_snapshot(permission=permission)
    session = _patch_search_dependencies(
        monkeypatch,
        provider_cutover=provider_cutover,
        blacklisted=blacklisted,
    )

    result = await Search(
        "query",
        tool_snapshot=snapshot,
        is_superuser=is_superuser,
    ).get_search()

    assert len(session.calls) == 1
    assert ("参考来源URL" in result) is expect_url
    assert ("https://example.test/a" in result) is expect_url
    if not expect_url:
        assert "参考来源：" in result
        assert "Source A" in result


@pytest.mark.asyncio
async def test_search_without_extractor_only_discloses_titles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _registered_snapshot(include_extractor=False)
    _patch_search_dependencies(
        monkeypatch,
        provider_cutover=True,
        blacklisted=False,
    )

    result = await Search("query", tool_snapshot=snapshot).get_search()

    assert "参考来源：" in result
    assert "Source A" in result
    assert "https://example.test/a" not in result


@pytest.mark.asyncio
async def test_search_legacy_snapshot_retains_membership_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_snapshot = SimpleNamespace(
        custom_tools={"extract_webpage": {"func": _extractor_handler}}
    )
    _patch_search_dependencies(
        monkeypatch,
        provider_cutover=True,
        blacklisted=True,
    )

    result = await Search("query", tool_snapshot=legacy_snapshot).get_search()

    assert "参考来源URL" in result
    assert "https://example.test/a" in result


@pytest.mark.asyncio
async def test_search_parity_failure_happens_before_network_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _registered_snapshot()
    session = _patch_search_dependencies(
        monkeypatch,
        provider_cutover=True,
        blacklisted=False,
    )

    def fail_parity(self, *, is_superuser, provider_cutover=None):
        raise RuntimeError("parity drift")

    monkeypatch.setattr(
        ToolSnapshot,
        "resolve_search_extractor",
        fail_parity,
    )

    result = await Search("query", tool_snapshot=snapshot).get_search()

    assert result is None
    assert session.calls == []


def test_search_rejects_non_boolean_actor() -> None:
    with pytest.raises(TypeError, match="is_superuser"):
        Search("query", is_superuser=1)  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "placeholder",
    ["your api", "your_api", "yourkey", "changeme", "", "  "],
)
async def test_search_placeholder_api_key_skips_request(
    monkeypatch: pytest.MonkeyPatch,
    placeholder: str,
) -> None:
    from nonebot_plugin_moellmchats import search as search_module

    session = _FakeSession()
    monkeypatch.setattr(search_module, "get_session", lambda: session)

    def get_config(key: str, default=None):
        if key == "search_api":
            return placeholder
        return default

    monkeypatch.setattr(search_module.config_parser, "get_config", get_config)

    result = await Search("query").get_search()

    assert session.calls == []
    assert isinstance(result, str)
    assert "search_api" in result


@pytest.mark.asyncio
async def test_search_keeps_tls_verification_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _registered_snapshot()
    session = _patch_search_dependencies(
        monkeypatch,
        provider_cutover=True,
        blacklisted=False,
    )

    await Search("query", tool_snapshot=snapshot).get_search()

    assert len(session.calls) == 1
    assert "ssl" not in session.calls[0]

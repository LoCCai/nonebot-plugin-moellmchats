from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

import nonebot_plugin_moellmchats.categorize as categorize_module
from nonebot_plugin_moellmchats.categorize import Categorize
from nonebot_plugin_moellmchats.classification_cache import (
    ClassificationCacheRecord,
    ClassificationCacheUnavailableError,
    MemoryClassificationCache,
)
from nonebot_plugin_moellmchats.llm_payload import LlmPayloadMixin
from nonebot_plugin_moellmchats.model_selector import model_selector
from nonebot_plugin_moellmchats.tool_catalog_cache import (
    MemoryToolCatalogCache,
    ToolCatalogCacheUnavailableError,
)
from nonebot_plugin_moellmchats.tool_discovery import with_menu_discovery
from nonebot_plugin_moellmchats.tool_manager import ToolSnapshot
from nonebot_plugin_moellmchats.tool_schema_cache import (
    MemoryToolSchemaCache,
    ToolSchemaCacheUnavailableError,
)


def _snapshot(generation: int = 42) -> ToolSnapshot:
    return ToolSnapshot(
        generation=generation,
        plugin_info={
            "alpha": {
                "name": "Alpha",
                "description": "alpha tool",
                "usage": "/alpha <query>",
            },
            "beta": {
                "name": "Beta",
                "description": "beta tool",
                "usage": "/beta <query>",
            },
        },
        custom_tools={},
        tool_dependencies={},
        mcp_tool_names=set(),
    )


class _CountingSnapshot:
    def __init__(self, snapshot: ToolSnapshot) -> None:
        self._snapshot = snapshot
        self.generation = snapshot.generation
        self.catalog_builds = 0
        self.schema_builds = 0
        self.legacy_schema_calls = 0

    def capture_brief_catalog_context(self, **kwargs: Any):
        return self._snapshot.capture_brief_catalog_context(**kwargs)

    def build_brief_catalog_record(self, context):
        self.catalog_builds += 1
        return self._snapshot.build_brief_catalog_record(context)

    def get_brief_catalog(self, **kwargs: Any):
        return self._snapshot.get_brief_catalog(**kwargs)

    def capture_llm_payload_schema_context(self, *args: Any, **kwargs: Any):
        return self._snapshot.capture_llm_payload_schema_context(
            *args,
            **kwargs,
        )

    def build_llm_payload_schema_record(self, context):
        self.schema_builds += 1
        return self._snapshot.build_llm_payload_schema_record(context)

    def get_llm_payload_tools(self, *args: Any, **kwargs: Any):
        self.legacy_schema_calls += 1
        return self._snapshot.get_llm_payload_tools(*args, **kwargs)

    def resolve_business_intent(self, *args: Any, **kwargs: Any):
        return self._snapshot.resolve_business_intent(*args, **kwargs)

    @property
    def directory_digest(self) -> str:
        return self._snapshot.directory_digest


class _FakeResponse:
    def __init__(
        self,
        payload: dict[str, Any],
        *,
        status: int = 200,
        enter_delay: float = 0,
    ) -> None:
        self.payload = payload
        self.status = status
        self.enter_delay = enter_delay

    async def __aenter__(self):
        if self.enter_delay:
            await asyncio.sleep(self.enter_delay)
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> bool:
        return False

    async def json(self) -> dict[str, Any]:
        return self.payload

    async def text(self) -> str:
        return "test response"


class _FakeSession:
    def __init__(self, responses: list[_FakeResponse | dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.call_count = 0

    def post(self, **_kwargs: Any) -> _FakeResponse:
        self.call_count += 1
        if not self.responses:
            raise AssertionError("unexpected classification model request")
        response = self.responses.pop(0)
        return response if isinstance(response, _FakeResponse) else _FakeResponse(response)


def _model_response(
    *,
    difficulty: object = "1",
    vision_required: object = False,
    required_plugins: object = None,
) -> dict[str, Any]:
    plugins = ["alpha"] if required_plugins is None else required_plugins
    return {
        "choices": [
            {
                "message": {
                    "content": categorize_module.json.dumps(
                        {
                            "difficulty": difficulty,
                            "vision_required": vision_required,
                            "required_plugins": plugins,
                        }
                    )
                }
            }
        ]
    }


def _configure_runtime(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {
        "classification_timeout_seconds": 1,
        "resident_plugins": [],
        "tools_enabled": True,
        "web_search_enabled": False,
    }
    model_info = {
        "model": "classifier-v1",
        "url": "https://classifier.invalid/chat",
        "key": "test-only-key",
        "json_mode": True,
        "stream": True,
    }

    def get_config(key: str, default: Any = None) -> Any:
        if key == "provider_catalog_categorize_enabled":
            return False
        if key == "provider_catalog_llm_payload_enabled":
            return False
        return state.get(key, default)

    monkeypatch.setattr(
        categorize_module.config_parser,
        "get_config",
        get_config,
    )
    monkeypatch.setattr(model_selector, "get_moe", lambda: False)
    monkeypatch.setattr(
        model_selector,
        "get_use_tools",
        lambda: state["tools_enabled"],
    )
    monkeypatch.setattr(
        model_selector,
        "get_web_search",
        lambda: state["web_search_enabled"],
    )
    monkeypatch.setattr(
        model_selector,
        "get_resident_plugins",
        lambda: list(state["resident_plugins"]),
    )
    monkeypatch.setattr(model_selector, "get_tool_blacklist", lambda: [])
    monkeypatch.setattr(
        model_selector,
        "get_model_for_capabilities",
        lambda _name, _capabilities: model_info,
    )
    return state


def _categorizer(
    plain: str,
    snapshot: _CountingSnapshot,
    *,
    catalog_cache,
    classification_cache=None,
    scene: str = "unknown",
) -> Categorize:
    return Categorize(
        plain,
        snapshot,
        is_superuser=False,
        tool_catalog_cache=catalog_cache,
        classification_cache=classification_cache,
        runtime_generation=snapshot.generation,
        scene=scene,
    )


class _TimeoutCache:
    async def lookup(self, _key):
        raise TimeoutError

    async def publish(self, _record):
        raise AssertionError("timeout lookup must not publish")


class _PublishTimeoutCache:
    async def lookup(self, _key):
        return None

    async def publish(self, _record):
        raise TimeoutError


class _RecordingClassificationCache:
    def __init__(self) -> None:
        self.records: dict[object, ClassificationCacheRecord] = {}
        self.lookup_count = 0
        self.publish_count = 0

    async def lookup(self, key):
        self.lookup_count += 1
        return self.records.get(key)

    async def publish(
        self,
        record: ClassificationCacheRecord,
    ) -> ClassificationCacheRecord:
        self.publish_count += 1
        self.records[record.key] = record
        return record


class _PayloadHarness(LlmPayloadMixin):
    def __init__(
        self,
        snapshot: _CountingSnapshot,
        cache,
        *,
        generation: int | None = None,
    ) -> None:
        resources = SimpleNamespace(
            generation=(snapshot.generation if generation is None else generation),
            tool_schema_cache=cache,
        )
        self.agent_runtime = SimpleNamespace(coordinator=SimpleNamespace(resources=resources))
        self.tool_snapshot = snapshot
        self.model_info = {"model": "chat-v1", "stream": True}
        self.required_plugins = ["alpha"]
        self.is_superuser = False
        self.messages_handler = SimpleNamespace(current_images=[])
        self.format_message_dict: dict[str, Any] = {}
        self._tool_schema_record = None


def _messages() -> list[dict[str, str]]:
    return [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hello"},
    ]


@pytest.mark.asyncio
async def test_catalog_consumer_miss_publish_hit_builds_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_runtime(monkeypatch)
    snapshot = _CountingSnapshot(_snapshot())
    cache = MemoryToolCatalogCache()
    session = _FakeSession([_model_response(), _model_response()])
    monkeypatch.setattr(categorize_module, "get_session", lambda: session)

    first = await _categorizer(
        "hello",
        snapshot,
        catalog_cache=cache,
    ).get_category()
    second = await _categorizer(
        "hello",
        snapshot,
        catalog_cache=cache,
    ).get_category()

    assert first == second == ("1", False, ["alpha"])
    assert snapshot.catalog_builds == 1
    assert session.call_count == 2


@pytest.mark.asyncio
async def test_catalog_consumer_rejects_generation_and_backend_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_runtime(monkeypatch)
    snapshot = _CountingSnapshot(_snapshot())

    with pytest.raises(ToolCatalogCacheUnavailableError, match="generation"):
        Categorize(
            "hello",
            snapshot,
            tool_catalog_cache=MemoryToolCatalogCache(),
            runtime_generation=snapshot.generation + 1,
        )

    with pytest.raises(ToolCatalogCacheUnavailableError, match="lookup"):
        await _categorizer(
            "hello",
            snapshot,
            catalog_cache=_TimeoutCache(),
        ).get_category()


@pytest.mark.asyncio
async def test_schema_consumer_miss_publish_hit_and_detached_materialize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_runtime(monkeypatch)
    snapshot = _CountingSnapshot(_snapshot())
    cache = MemoryToolSchemaCache()
    first = _PayloadHarness(snapshot, cache)
    second = _PayloadHarness(snapshot, cache)

    await first._prepare_tool_schema_record()
    await second._prepare_tool_schema_record()

    first_data, first_stream = first._build_payload(_messages())
    first_data["tools"][0]["function"]["description"] = "tampered"
    second_data, second_stream = second._build_payload(_messages())

    assert snapshot.schema_builds == 1
    assert snapshot.legacy_schema_calls == 0
    assert first_stream is second_stream is False
    assert second_data["tools"][0]["function"]["name"] == "alpha"
    assert second_data["tools"][0]["function"]["description"] != "tampered"


@pytest.mark.asyncio
async def test_schema_consumer_rejects_identity_generation_and_backend_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _configure_runtime(monkeypatch)
    snapshot = _CountingSnapshot(_snapshot())

    identity_drift = _PayloadHarness(snapshot, MemoryToolSchemaCache())
    await identity_drift._prepare_tool_schema_record()
    state["resident_plugins"] = ["beta"]
    with pytest.raises(ToolSchemaCacheUnavailableError, match="identity"):
        identity_drift._build_payload(_messages())

    generation_drift = _PayloadHarness(
        snapshot,
        MemoryToolSchemaCache(),
        generation=snapshot.generation + 1,
    )
    with pytest.raises(ToolSchemaCacheUnavailableError, match="generation"):
        await generation_drift._prepare_tool_schema_record()

    state["resident_plugins"] = []
    timeout = _PayloadHarness(snapshot, _TimeoutCache())
    with pytest.raises(ToolSchemaCacheUnavailableError, match="lookup"):
        await timeout._prepare_tool_schema_record()


@pytest.mark.asyncio
async def test_classification_consumer_hits_without_second_model_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_runtime(monkeypatch)
    snapshot = _CountingSnapshot(_snapshot())
    catalog_cache = MemoryToolCatalogCache()
    classification_cache = MemoryClassificationCache()
    session = _FakeSession([_model_response(difficulty="2")])
    monkeypatch.setattr(categorize_module, "get_session", lambda: session)

    first = await _categorizer(
        "analyze this",
        snapshot,
        catalog_cache=catalog_cache,
        classification_cache=classification_cache,
    ).get_category()
    second = await _categorizer(
        "analyze this",
        snapshot,
        catalog_cache=catalog_cache,
        classification_cache=classification_cache,
    ).get_category()

    assert first == second == ("2", False, ["alpha"])
    assert snapshot.catalog_builds == 1
    assert session.call_count == 1


@pytest.mark.asyncio
async def test_unique_business_owner_bypasses_wrong_classifier_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_runtime(monkeypatch)
    owner_info = with_menu_discovery(
        {
            "name": "群报告",
            "description": "今日发言排行",
            "usage": "/B话榜 今日",
        },
        [
            {
                "func": "B 话榜与即时排行",
                "brief_des": "今日排行",
                "trigger_method": "命令",
                "trigger_condition": "B话榜 今日",
                "pmn_llm_intents": [
                    "今天谁发言最多",
                    "今日谁发言最多",
                    "看一下今日的群发言排行",
                    "查看今日群发言排行",
                    "今日发言榜",
                    "今日发言排行",
                    "今天发言榜",
                    "今天发言排行",
                    "今天谁说话最多",
                    "今天谁话最多",
                ],
            }
        ],
    )
    snapshot = _CountingSnapshot(
        ToolSnapshot(
            generation=42,
            plugin_info={
                "qi_post": owner_info,
                "qi_db_analytics": {
                    "name": "统计",
                    "description": "错误分类候选",
                    "usage": "/查询",
                },
            },
            custom_tools={},
            tool_dependencies={},
            mcp_tool_names=set(),
        )
    )
    session = _FakeSession(
        [_model_response(required_plugins=["qi_db_analytics"])]
    )
    monkeypatch.setattr(categorize_module, "get_session", lambda: session)

    variants = [
        "今天谁发言最多",
        "今日谁发言最多",
        "看一下今日的群发言排行",
        "查看今日群发言排行",
        "今日发言榜",
        "今日发言排行",
        "今天发言榜",
        "今天发言排行",
        "今天谁说话最多",
        "今天谁话最多",
    ]
    for phrase in variants:
        categorizer = _categorizer(
            phrase,
            snapshot,
            catalog_cache=MemoryToolCatalogCache(),
            scene="group",
        )
        assert await categorizer.get_category() == (
            "0",
            False,
            ["qi_post"],
        )
        assert categorizer.selection_source == "business_intent_owner"
    assert session.call_count == 0


@pytest.mark.asyncio
async def test_business_owner_ambiguity_and_unavailability_do_not_fall_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_runtime(monkeypatch)

    def info(name: str, *, hidden: bool = False):
        return with_menu_discovery(
            {"name": name, "description": name, "usage": name},
            [
                {
                    "func": name,
                    "brief_des": name,
                    "trigger_method": "命令",
                    "trigger_condition": name,
                    "pmn_hidden": hidden,
                    "pmn_llm_intents": ["今天谁发言最多"],
                }
            ],
        )

    ambiguous = _CountingSnapshot(
        ToolSnapshot(
            generation=42,
            plugin_info={"qi_post": info("排行"), "other": info("其他")},
            custom_tools={},
            tool_dependencies={},
            mcp_tool_names=set(),
        )
    )
    session = _FakeSession([])
    monkeypatch.setattr(categorize_module, "get_session", lambda: session)
    result = await _categorizer(
        "今天谁发言最多",
        ambiguous,
        catalog_cache=MemoryToolCatalogCache(),
    ).get_category()
    assert isinstance(result, str)
    assert "多个" in result

    hidden = _CountingSnapshot(
        ToolSnapshot(
            generation=43,
            plugin_info={"qi_post": info("隐藏排行", hidden=True)},
            custom_tools={},
            tool_dependencies={},
            mcp_tool_names=set(),
        )
    )
    result = await _categorizer(
        "今天谁发言最多",
        hidden,
        catalog_cache=MemoryToolCatalogCache(),
    ).get_category()
    assert isinstance(result, str)
    assert "不可用" in result
    assert session.call_count == 0


@pytest.mark.asyncio
async def test_classification_cache_separates_group_and_private_scene(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_runtime(monkeypatch)
    snapshot = _CountingSnapshot(_snapshot())
    session = _FakeSession([_model_response(), _model_response()])
    monkeypatch.setattr(categorize_module, "get_session", lambda: session)
    catalog_cache = MemoryToolCatalogCache()
    classification_cache = MemoryClassificationCache()

    for scene in ("group", "private"):
        result = await _categorizer(
            "same prompt",
            snapshot,
            catalog_cache=catalog_cache,
            classification_cache=classification_cache,
            scene=scene,
        ).get_category()
        assert result == ("1", False, ["alpha"])
    assert session.call_count == 2


@pytest.mark.asyncio
async def test_classification_record_validation_retries_before_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_runtime(monkeypatch)
    snapshot = _CountingSnapshot(_snapshot())
    cache = _RecordingClassificationCache()
    session = _FakeSession(
        [
            _model_response(difficulty="9"),
            _model_response(difficulty="0", required_plugins=[]),
        ]
    )
    monkeypatch.setattr(categorize_module, "get_session", lambda: session)

    result = await _categorizer(
        "hello",
        snapshot,
        catalog_cache=MemoryToolCatalogCache(),
        classification_cache=cache,
    ).get_category()

    assert result == ("0", False, [])
    assert session.call_count == 2
    assert cache.publish_count == 1


@pytest.mark.asyncio
async def test_content_blocked_classification_is_never_published(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_runtime(monkeypatch)
    snapshot = _CountingSnapshot(_snapshot())
    cache = _RecordingClassificationCache()
    session = _FakeSession(
        [
            {"code": "DataInspectionFailed"},
            {"code": "DataInspectionFailed"},
        ]
    )
    monkeypatch.setattr(categorize_module, "get_session", lambda: session)

    results = [
        await _categorizer(
            "blocked",
            snapshot,
            catalog_cache=MemoryToolCatalogCache(),
            classification_cache=cache,
        ).get_category()
        for _ in range(2)
    ]

    assert results == ["内容不合规，拒绝回答", "内容不合规，拒绝回答"]
    assert cache.publish_count == 0
    assert session.call_count == 2


@pytest.mark.asyncio
async def test_parse_fallback_classification_is_never_published(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_runtime(monkeypatch)
    snapshot = _CountingSnapshot(_snapshot())
    cache = _RecordingClassificationCache()
    invalid = {"choices": [{"message": {"content": "not json"}}]}
    session = _FakeSession([invalid, invalid, invalid, invalid])
    monkeypatch.setattr(categorize_module, "get_session", lambda: session)
    catalog_cache = MemoryToolCatalogCache()

    results = [
        await _categorizer(
            "invalid",
            snapshot,
            catalog_cache=catalog_cache,
            classification_cache=cache,
        ).get_category()
        for _ in range(2)
    ]

    assert results == [False, False]
    assert cache.publish_count == 0
    assert session.call_count == 4


@pytest.mark.asyncio
async def test_timeout_fallback_classification_is_never_published(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _configure_runtime(monkeypatch)
    state["classification_timeout_seconds"] = 0.01
    snapshot = _CountingSnapshot(_snapshot())
    cache = _RecordingClassificationCache()
    delayed = _FakeResponse(_model_response(), enter_delay=0.05)
    session = _FakeSession([delayed, delayed])
    monkeypatch.setattr(categorize_module, "get_session", lambda: session)
    catalog_cache = MemoryToolCatalogCache()

    results = [
        await _categorizer(
            "slow",
            snapshot,
            catalog_cache=catalog_cache,
            classification_cache=cache,
        ).get_category()
        for _ in range(2)
    ]

    assert results == [("1", False, []), ("1", False, [])]
    assert cache.publish_count == 0
    assert session.call_count == 2


@pytest.mark.asyncio
async def test_classification_cache_timeout_does_not_become_model_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_runtime(monkeypatch)
    snapshot = _CountingSnapshot(_snapshot())
    session = _FakeSession([])
    monkeypatch.setattr(categorize_module, "get_session", lambda: session)

    with pytest.raises(ClassificationCacheUnavailableError, match="lookup"):
        await _categorizer(
            "hello",
            snapshot,
            catalog_cache=MemoryToolCatalogCache(),
            classification_cache=_TimeoutCache(),
        ).get_category()

    assert session.call_count == 0


@pytest.mark.asyncio
async def test_cache_publish_timeouts_are_normalized_without_bypass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_runtime(monkeypatch)
    snapshot = _CountingSnapshot(_snapshot())

    with pytest.raises(ToolCatalogCacheUnavailableError, match="publish"):
        await _categorizer(
            "hello",
            snapshot,
            catalog_cache=_PublishTimeoutCache(),
        ).get_category()

    schema = _PayloadHarness(snapshot, _PublishTimeoutCache())
    with pytest.raises(ToolSchemaCacheUnavailableError, match="publish"):
        await schema._prepare_tool_schema_record()

    session = _FakeSession([_model_response()])
    monkeypatch.setattr(categorize_module, "get_session", lambda: session)
    with pytest.raises(ClassificationCacheUnavailableError, match="publish"):
        await _categorizer(
            "hello",
            snapshot,
            catalog_cache=MemoryToolCatalogCache(),
            classification_cache=_PublishTimeoutCache(),
        ).get_category()
    assert session.call_count == 1


@pytest.mark.asyncio
async def test_classification_key_includes_complete_user_plain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_runtime(monkeypatch)
    snapshot = _CountingSnapshot(_snapshot())
    session = _FakeSession([_model_response(), _model_response()])
    monkeypatch.setattr(categorize_module, "get_session", lambda: session)
    catalog_cache = MemoryToolCatalogCache()
    classification_cache = MemoryClassificationCache()

    first = await _categorizer(
        "first prompt",
        snapshot,
        catalog_cache=catalog_cache,
        classification_cache=classification_cache,
    ).get_category()
    second = await _categorizer(
        "second prompt",
        snapshot,
        catalog_cache=catalog_cache,
        classification_cache=classification_cache,
    ).get_category()

    assert first == second == ("1", False, ["alpha"])
    assert session.call_count == 2

from __future__ import annotations

import asyncio
import json

import pytest

from nonebot_plugin_moellmchats.generated_tools import GeneratedToolStore
from nonebot_plugin_moellmchats.tool_authoring import (
    _MODEL_ERROR_BODY_LIMIT,
    ToolAuthoringService,
    _format_model_http_error,
    _read_model_error_body,
)


class _FakeContent:
    def __init__(self, body: str) -> None:
        self.body = body.encode("utf-8")
        self.read_limits: list[int] = []

    async def readexactly(self, limit: int) -> bytes:
        self.read_limits.append(limit)
        if len(self.body) < limit:
            raise asyncio.IncompleteReadError(
                partial=self.body,
                expected=limit,
            )
        return self.body[:limit]


class _FakeResponse:
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self.body = body
        self.charset = "utf-8"
        self.content = _FakeContent(body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    async def text(self) -> str:
        return self.body


class _FakeSession:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[dict] = []

    def post(self, _url, **kwargs):
        self.requests.append(kwargs)
        return self.responses.pop(0)


def _store(tmp_path) -> GeneratedToolStore:
    store = GeneratedToolStore()
    store.root = tmp_path / "generated"
    store.drafts_dir = store.root / "drafts"
    store.versions_dir = store.root / "versions"
    store.active_file = store.root / "active.json"
    store._init_files()
    return store


def _generated() -> str:
    return json.dumps(
        {
            "manifest": {
                "bundle_id": "date_math",
                "description": "date math",
                "tools": [
                    {
                        "name": "date_math",
                        "description": "date math",
                        "parameters": {
                            "type": "object",
                            "properties": {"value": {"type": "integer"}},
                            "required": ["value"],
                        },
                        "handler": "date_math",
                        "permission": "user",
                        "effect": "read_only",
                        "timeout_seconds": 5,
                        "result_limit": 100,
                    }
                ],
            },
            "tool_py": "async def date_math(value):\n    return str(value)\n",
            "tests_py": (
                "async def run_tests(tool_module):\n    assert await tool_module.date_math(2) == '2'\n    return '1 passed'\n"
            ),
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", [True, False])
async def test_authoring_uses_selected_then_summary_and_persists_review(tmp_path, monkeypatch, approved) -> None:
    from nonebot_plugin_moellmchats import tool_authoring as module

    store = _store(tmp_path)
    service = ToolAuthoringService()
    calls = []

    async def call_model(model_key, system, user):
        calls.append(model_key)
        if model_key == "selected_model":
            return _generated()
        return json.dumps({"approved": approved, "summary": "reviewed", "risks": []})

    async def run_tests(path):
        return "1 passed"

    monkeypatch.setattr(module, "generated_tool_store", store)
    monkeypatch.setattr(module.generated_tool_runner, "run_tests", run_tests)
    monkeypatch.setattr(service, "_call_model", call_model)
    draft_id, validation, review, summary = await service.create("calculate dates", actor_key="1")
    _, metadata, _ = store.get_draft(draft_id)
    assert calls == ["selected_model", "summary_model"]
    assert review["approved"] is approved
    assert metadata["status"] == ("reviewed" if approved else "review_failed")
    evidence_states = [item["state"] for item in metadata["lifecycle_evidence"]]
    assert evidence_states[:2] == ["static_validated", "sandbox_tested"]
    assert evidence_states[-1] == ("model_reviewed" if approved else "review_failed")
    assert validation.digest == metadata["digest"]
    assert summary == "1 passed"
    if not approved:
        review_snapshot = store.get_draft_review_snapshot(draft_id)
        with pytest.raises(ValueError, match="不可批准"):
            store.prepare_approval(
                draft_id,
                validation.digest[:12],
                review_snapshot.review_stamp,
            )


@pytest.mark.asyncio
async def test_authoring_is_single_flight(tmp_path, monkeypatch) -> None:
    from nonebot_plugin_moellmchats import tool_authoring as module

    store = _store(tmp_path)
    service = ToolAuthoringService()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_model(model_key, system, user):
        entered.set()
        await release.wait()
        return _generated()

    monkeypatch.setattr(module, "generated_tool_store", store)
    monkeypatch.setattr(service, "_call_model", blocked_model)
    task = asyncio.create_task(service.create("first", actor_key="1"))
    await entered.wait()
    with pytest.raises(RuntimeError, match="已有一个"):
        await service.create("second", actor_key="1")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_model_http_error_is_structured_bounded_and_redacted() -> None:
    body = json.dumps(
        {
            "error": {
                "code": "invalid_parameter",
                "type": "invalid_request_error",
                "param": "top_k",
                "message": (
                    "Unsupported parameter; Authorization: Bearer secret-token; "
                    "api_key=sk-abcdefghijk; file=/root/private/providers.toml; "
                    "endpoint=https://user:password@example.invalid/v1"
                ),
            }
        }
    )

    rendered = _format_model_http_error(
        400,
        body,
        truncated=False,
        compatibility_retried=True,
    )

    assert "invalid_parameter" in rendered
    assert "invalid_request_error" in rendered
    assert "top_k" in rendered
    assert "已使用最小兼容参数重试" in rendered
    assert "secret-token" not in rendered
    assert "sk-abcdefghijk" not in rendered
    assert "/root/private" not in rendered
    assert "user:password" not in rendered
    assert "<redacted>" in rendered
    assert len(rendered) < 700


@pytest.mark.asyncio
async def test_model_http_error_body_read_is_bounded() -> None:
    response = _FakeResponse(
        400,
        "x" * (_MODEL_ERROR_BODY_LIMIT + 200),
    )

    body, truncated, read_bytes = await _read_model_error_body(response)

    assert body == "x" * _MODEL_ERROR_BODY_LIMIT
    assert truncated is True
    assert read_bytes == _MODEL_ERROR_BODY_LIMIT + 1
    assert response.content.read_limits == [_MODEL_ERROR_BODY_LIMIT + 1]


@pytest.mark.asyncio
async def test_call_model_retries_http_400_with_minimal_payload(
    monkeypatch,
) -> None:
    from nonebot_plugin_moellmchats import tool_authoring as module

    first = _FakeResponse(
        400,
        json.dumps(
            {
                "error": {
                    "param": "top_k",
                    "message": "Unsupported parameter: top_k",
                }
            }
        ),
    )
    second = _FakeResponse(
        200,
        json.dumps({"choices": [{"message": {"content": "generated content"}}]}),
    )
    session = _FakeSession([first, second])
    monkeypatch.setattr(
        module.model_selector,
        "get_model",
        lambda _key: {
            "model": "test-model",
            "url": "https://example.invalid/v1/chat/completions",
            "key": "Bearer test",
            "top_k": 1,
            "max_tokens": 100,
            "extra_payload": {"vendor_option": True},
        },
    )
    monkeypatch.setattr(module, "get_session", lambda: session)

    result = await ToolAuthoringService()._call_model(
        "selected_model",
        "system",
        "user",
    )

    assert result == "generated content"
    assert len(session.requests) == 2
    assert session.requests[0]["json"]["top_k"] == 1
    assert session.requests[0]["json"]["max_tokens"] == 100
    assert session.requests[0]["json"]["vendor_option"] is True
    assert session.requests[1]["json"] == {
        "model": "test-model",
        "messages": [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "user"},
        ],
        "stream": False,
    }
    assert session.requests[1]["headers"]["Accept-Encoding"] == "identity"
    assert first.content.read_limits == [_MODEL_ERROR_BODY_LIMIT + 1]


@pytest.mark.asyncio
async def test_call_model_does_not_retry_content_policy_rejection(
    monkeypatch,
) -> None:
    from nonebot_plugin_moellmchats import tool_authoring as module

    response = _FakeResponse(
        400,
        json.dumps(
            {
                "error": {
                    "code": "content_filter",
                    "message": "safety policy rejected token=secret-value",
                }
            }
        ),
    )
    session = _FakeSession([response])
    monkeypatch.setattr(
        module.model_selector,
        "get_model",
        lambda _key: {
            "model": "test-model",
            "url": "https://example.invalid/v1/chat/completions",
            "key": "Bearer test",
            "temperature": 0,
        },
    )
    monkeypatch.setattr(module, "get_session", lambda: session)

    with pytest.raises(RuntimeError) as captured:
        await ToolAuthoringService()._call_model(
            "selected_model",
            "system",
            "user",
        )

    assert "安全策略" in str(captured.value)
    assert "secret-value" not in str(captured.value)
    assert len(session.requests) == 1


@pytest.mark.asyncio
async def test_call_model_hides_unstructured_error_body(monkeypatch) -> None:
    from nonebot_plugin_moellmchats import tool_authoring as module

    response = _FakeResponse(502, "proxy dumped secret-token and request body")
    session = _FakeSession([response])
    monkeypatch.setattr(
        module.model_selector,
        "get_model",
        lambda _key: {
            "model": "test-model",
            "url": "https://example.invalid/v1/chat/completions",
            "key": "Bearer test",
        },
    )
    monkeypatch.setattr(module, "get_session", lambda: session)

    with pytest.raises(RuntimeError) as captured:
        await ToolAuthoringService()._call_model(
            "selected_model",
            "system",
            "user",
        )

    assert str(captured.value) == "模型请求失败 HTTP 502"
    assert "secret-token" not in str(captured.value)

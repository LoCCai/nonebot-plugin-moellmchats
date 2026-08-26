from __future__ import annotations

import asyncio
import json

import pytest

from nonebot_plugin_moellmchats.generated_tools import GeneratedToolStore
from nonebot_plugin_moellmchats.tool_authoring import ToolAuthoringService


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
                "async def run_tests(tool_module):\n"
                "    assert await tool_module.date_math(2) == '2'\n"
                "    return '1 passed'\n"
            ),
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", [True, False])
async def test_authoring_uses_selected_then_summary_and_persists_review(
    tmp_path, monkeypatch, approved
) -> None:
    from nonebot_plugin_moellmchats import tool_authoring as module

    store = _store(tmp_path)
    service = ToolAuthoringService()
    calls = []

    async def call_model(model_key, system, user):
        calls.append(model_key)
        if model_key == "selected_model":
            return _generated()
        return json.dumps(
            {"approved": approved, "summary": "reviewed", "risks": []}
        )

    async def run_tests(path):
        return "1 passed"

    monkeypatch.setattr(module, "generated_tool_store", store)
    monkeypatch.setattr(module.generated_tool_runner, "run_tests", run_tests)
    monkeypatch.setattr(service, "_call_model", call_model)
    draft_id, validation, review, summary = await service.create(
        "calculate dates", actor_key="1"
    )
    _, metadata, _ = store.get_draft(draft_id)
    assert calls == ["selected_model", "summary_model"]
    assert review["approved"] is approved
    assert metadata["status"] == ("reviewed" if approved else "review_failed")
    evidence_states = [
        item["state"] for item in metadata["lifecycle_evidence"]
    ]
    assert evidence_states[:2] == ["static_validated", "sandbox_tested"]
    assert evidence_states[-1] == (
        "model_reviewed" if approved else "review_failed"
    )
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

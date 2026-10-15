from __future__ import annotations

from copy import deepcopy
import json

from nonebot.exception import StopPropagation
from nonebot.message import check_and_run_matcher
import pytest
from test_admin_commands import _group_event, _RecordingBot
from test_model_pools import pool
from test_model_selector import _initialized_selector

import nonebot_plugin_moellmchats as plugin
from nonebot_plugin_moellmchats import model_admin
from nonebot_plugin_moellmchats.model_admin import format_chain, format_pools, manage_pool
from nonebot_plugin_moellmchats.runtime_snapshot import runtime_snapshots


def test_pool_edit_persists_without_changing_quota_or_rotation(tmp_path, monkeypatch):
    selector = pool(tmp_path, monkeypatch)
    selector._pool_offsets = {"0": 12}
    manage_pool(selector, "设置", "0", "backup (other); third (other)")
    assert selector.get_moe_current_model("0")["model"] == "backup"
    assert selector._pool_offsets == {"0": 13}
    manage_pool(selector, "添加", "0", "model-a (good)")
    manage_pool(selector, "移除", "0", "third (other)")
    saved = json.loads(selector.model_config_file.read_text())
    assert saved["model_pools"]["0"] == ["backup (other)", "model-a (good)"]
    assert selector.model_config_file.stat().st_mode & 0o777 == 0o600
    manage_pool(selector, "关闭", "0")
    assert "0" not in selector.model_config["model_pools"]
    assert selector.get_moe_current_model("0")["model"] == "model-a"
    assert not selector.is_model_exhausted("model-a (good)")


@pytest.mark.parametrize(("action", "query"), [
    ("设置", "backup (other);missing"), ("设置", "backup (other);backup (other)"),
    ("设置", "backup (other);"), ("移除", "third (other)"), ("关闭", "unexpected"),
    ("移除", "model-a (good);backup (other)"),
])
def test_invalid_edit_is_atomic(tmp_path, monkeypatch, action, query):
    selector = pool(tmp_path, monkeypatch)
    before = deepcopy(selector.model_config)
    disk = selector.model_config_file.read_bytes()
    with pytest.raises(ValueError, match="模型"):
        manage_pool(selector, action, "0", query)
    assert selector.model_config == before
    assert selector.model_config_file.read_bytes() == disk


def test_save_failure_keeps_effective_config(tmp_path, monkeypatch):
    selector = pool(tmp_path, monkeypatch)
    before = deepcopy(selector.model_config)

    def fail(*args):
        raise PermissionError("private path")

    monkeypatch.setattr(model_admin, "atomic_write_private_text", fail)
    with pytest.raises(PermissionError):
        manage_pool(selector, "设置", "0", "third (other)")
    assert selector.model_config == before


def test_capability_routing_conflict_keeps_config(tmp_path, monkeypatch):
    selector = pool(tmp_path, monkeypatch)
    selector.model_config["model_pools"] = {}
    selector.model_config["capability_routing"] = {"enabled": True}
    with pytest.raises(ValueError, match="能力路由"):
        manage_pool(selector, "设置", "0", "third (other)")
    assert selector.model_config["model_pools"] == {}


def test_saved_pool_survives_candidate_reload_and_numeric_identity(tmp_path, monkeypatch):
    selector = _initialized_selector(tmp_path, monkeypatch)
    with selector.providers_file.open("a") as stream:
        stream.write('\n[providers.other]\nbase_url="https://other.invalid/v1"\napi_key="test"\nmodels=["backup"]\n')
    selector.load_providers(strict=True)
    selector._load_all_models(strict=True)
    names = selector.get_sorted_model_names()
    number = str(names.index("backup (other)") + 1)
    manage_pool(selector, "设置", "0", number)
    candidate = selector.build_candidate()
    assert candidate.model_config["model_pools"]["0"] == ("backup (other)",)
    selector.commit_candidate(candidate)
    assert selector.get_moe_current_model("0")["model"] == "backup"


def test_key_status_understands_keys_and_legacy_key_without_selecting(tmp_path, monkeypatch):
    selector = pool(tmp_path, monkeypatch)
    selector.models["third (other)"].pop("key", None)
    selector.models["third (other)"]["keys"] = ["private-test"]
    assert "third (other) · 可参与选择" in format_pools(selector, "1")
    selector.models["third (other)"]["keys"] = [" "]
    assert "third (other) · 未配置密钥" in format_pools(selector, "1")


@pytest.mark.asyncio
async def test_read_only_chain_reports_effective_models_and_thinking_without_secrets(tmp_path, monkeypatch):
    selector = pool(tmp_path, monkeypatch)
    selector.model_config["use_moe"] = True
    selector.models["backup (other)"]["extra_payload"] = {
        "enable_thinking": False, "reasoning_effort": "high", "secret": "never-print-this",
        "extra_body": {"google": {"thinking_config": {"thinking_level": "low"}}},
    }
    selector.models["third (other)"]["key"] = []
    await selector.mark_model_exhausted(selector.models["model-a (good)"], 403)

    def forbid(*args, **kwargs):
        pytest.fail("read-only query selected a credential/model")

    monkeypatch.setattr(selector, "_get_random_key", forbid)
    offsets = deepcopy(getattr(selector, "_pool_offsets", {}))
    text = format_chain(selector, {"tool_image_vision_enabled": False}) + format_pools(selector)
    for value in ("思考开关=关", "思考强度=high", "思考强度=low", "额度耗尽", "未配置密钥", "二次识图：关"):
        assert value in text
    for value in ("never-print-this", "test-backup", "good.invalid", "Bearer", "api_key"):
        assert value not in text
    assert getattr(selector, "_pool_offsets", {}) == offsets


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["/", "!", "！", ""])
async def test_real_matcher_permission_and_complete_model_names(tmp_path, monkeypatch, prefix):
    selector = pool(tmp_path, monkeypatch)
    monkeypatch.setattr(plugin, "model_selector", selector)
    bot = _RecordingBot()
    event = _group_event(user_id=1, text=f"{prefix}设置模型池 分类 backup (other)；third (other)")
    with pytest.raises(StopPropagation):
        await check_and_run_matcher(plugin.model_pool_matcher, bot, event, {})
    assert selector.model_config["model_pools"]["category"] == ["backup (other)", "third (other)"]
    assert "已保存" in str(bot.calls[-1][1]["message"])
    before = deepcopy(selector.model_config)
    bot.calls.clear()
    await check_and_run_matcher(plugin.model_pool_matcher, bot,
        _group_event(user_id=2, text=f"{prefix}关闭模型池 分类"), {})
    assert selector.model_config == before
    assert not bot.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["!查看LLM链路", "/查看配置", "查看当前配置"])
async def test_chain_matcher_routes_without_llm(tmp_path, monkeypatch, text):
    monkeypatch.setattr(plugin, "model_selector", pool(tmp_path, monkeypatch))
    bot = _RecordingBot()
    with pytest.raises(StopPropagation):
        await check_and_run_matcher(plugin.check_config_matcher, bot, _group_event(user_id=1, text=text), {})
    assert "生效配置" in str(bot.calls[-1][1]["message"])
    bot.calls.clear()
    await check_and_run_matcher(plugin.check_config_matcher, bot, _group_event(user_id=2, text=text), {})
    assert not bot.calls


def test_pool_commit_patches_current_snapshot_and_keeps_bound_request(tmp_path, monkeypatch):
    import importlib

    module = importlib.import_module("nonebot_plugin_moellmchats.model_selector")
    selector = pool(tmp_path, monkeypatch)
    monkeypatch.setattr(module, "model_selector", selector)
    current = runtime_snapshots.current()
    from nonebot_plugin_moellmchats.runtime_snapshot import RuntimeSnapshot

    snapshot = RuntimeSnapshot(generation=1, config={}, model_state=selector.capture_state(),
        temperaments={}, temperament_assignments={}, replies={}, tool_snapshot=None, emotions=(), reloaded_at=0)
    monkeypatch.setattr(runtime_snapshots, "_current", snapshot)
    manage_pool(selector, "设置", "0", "third (other)")
    assert runtime_snapshots.current().model_state.model_config["model_pools"]["0"] == ("third (other)",)
    with runtime_snapshots.bind(snapshot):
        assert selector._active_state().model_config["model_pools"]["0"] != ("third (other)",)
    assert selector._active_state().model_config["model_pools"]["0"] == ("third (other)",)
    runtime_snapshots._current = current

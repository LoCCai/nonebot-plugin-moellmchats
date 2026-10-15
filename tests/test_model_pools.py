from __future__ import annotations

from copy import deepcopy
import json

import pytest
from test_model_failover import _pair
from test_model_selector import _initialized_selector

from nonebot_plugin_moellmchats.model_selector import ModelSelector


def pool(tmp_path, monkeypatch):
    selector = _pair(tmp_path, monkeypatch)
    selector.models["third (other)"] = {**deepcopy(selector.models["backup (other)"]), "model": "third"}
    selector.model_config["model_pools"] = {
        "category": ["model-a (good)", "backup (other)"],
        "0": ["model-a (good)", "backup (other)"], "1": ["third (other)"],
    }
    return selector


def test_roles_rotate_across_providers_without_changing_tiers(tmp_path, monkeypatch):
    selector = pool(tmp_path, monkeypatch)
    assert [selector.get_moe_current_model("0")["provider"] for _ in range(4)] == ["good", "other", "good", "other"]
    assert [selector.get_model("category_model")["model"] for _ in range(2)] == ["model-a", "backup"]
    assert selector.get_moe_current_model("1")["model"] == "third"
    model = selector.get_moe_current_model("0")
    backup = selector.get_rate_limit_fallback(model, set())
    assert backup["model"] == "backup"
    assert backup["key"] == "Bearer test-backup"
    assert backup["_model_pool"] == "0"


@pytest.mark.asyncio
async def test_quota_exhaustion_survives_reload_and_applies_to_all_roles(tmp_path, monkeypatch):
    selector = pool(tmp_path, monkeypatch)
    model = selector.get_model("category_model")
    await selector.mark_model_exhausted(model, 403)
    assert selector.get_moe_current_model("0")["model"] == "backup"
    assert selector.get_model("category_model")["model"] == "backup"
    state = json.loads(selector.model_config_file.with_name("model_quota_status.json").read_text())
    assert state["model-a (good)"]["reason"] == "quota_exhausted"
    assert "key" not in json.dumps(state)
    reloaded = object.__new__(ModelSelector)
    reloaded.model_config_file = selector.model_config_file
    reloaded.models, reloaded.providers = selector.models, selector.providers
    reloaded.global_default, reloaded.model_config = {}, deepcopy(selector.model_config)
    assert reloaded.get_model("category_model")["model"] == "backup"
    assert "额度耗尽" in reloaded.get_formatted_model_list()
    await reloaded.restore_model_quota("model-a (good)")
    assert not reloaded.is_model_exhausted("model-a (good)")


@pytest.mark.asyncio
async def test_exhausted_pool_stops_without_falling_to_other_tier(tmp_path, monkeypatch):
    selector = pool(tmp_path, monkeypatch)
    for _ in range(2):
        model = selector.get_moe_current_model("0")
        await selector.mark_model_exhausted(model, 403)
    assert selector.get_moe_current_model("0") is None
    assert selector.get_moe_current_model("1")["model"] == "third"
    assert selector.get_rate_limit_fallback(model, set()) is None


def test_pool_fallback_is_bounded_and_does_not_disable_rate_limited_model(tmp_path, monkeypatch):
    selector = pool(tmp_path, monkeypatch)
    first = selector.get_moe_current_model("0")
    attempted = set()
    backup = selector.get_rate_limit_fallback(first, attempted)
    assert selector.get_rate_limit_fallback(backup, attempted) is None
    assert not selector.is_model_exhausted("model-a (good)")
    assert selector.get_moe_current_model("0") is not None


@pytest.mark.parametrize("pools", [{"3": ["model-a (good)"]}, {"0": []},
    {"0": ["unknown"]}, {"category": ["model-a (good)"] * 2}])
def test_invalid_pools_fail_configuration_validation(tmp_path, monkeypatch, pools):
    selector = _initialized_selector(tmp_path, monkeypatch)
    selector.model_config["model_pools"] = pools
    assert not selector.validate_model_config()[0]


def test_pools_skip_models_without_required_tool_or_vision_capability(tmp_path, monkeypatch):
    selector = pool(tmp_path, monkeypatch)
    selector.models["model-a (good)"]["no_tools"] = True
    selector.models["backup (other)"]["is_vision"] = True
    assert selector.get_moe_current_model("0", has_tools=True)["model"] == "backup"
    assert selector.get_model("category_model", has_images=True)["model"] == "backup"

from __future__ import annotations

import json

import pytest

from nonebot_plugin_moellmchats.model_selector import ModelSelector


class _Response:
    def __init__(self, status: int, models: list[str] | None = None) -> None:
        self.status = status
        self._models = models or []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def json(self):
        return {"data": [{"id": item} for item in self._models]}


class _Session:
    def get(self, url, **kwargs):
        if "good.invalid" in url:
            return _Response(200, ["new-good"])
        return _Response(503)


@pytest.mark.asyncio
async def test_provider_refresh_keeps_last_known_good_on_failure(
    tmp_path, monkeypatch
) -> None:
    from nonebot_plugin_moellmchats import utils

    selector = object.__new__(ModelSelector)
    selector.models_file = tmp_path / "models.json"
    selector.providers_file = tmp_path / "providers.toml"
    selector.cache_file = tmp_path / "model_cache.json"
    selector.model_config_file = tmp_path / "model_config.json"
    selector.models = {}
    selector.global_default = {}
    selector.model_config = {}
    selector.providers = {
        "good": {"base_url": "https://good.invalid/v1", "api_key": "one"},
        "bad": {"base_url": "https://bad.invalid/v1", "api_key": "two"},
    }
    selector.cache_file.write_text(
        json.dumps(
            {
                "good": ["old-good"],
                "bad": ["last-known-bad"],
                "removed": ["stale"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(utils, "get_session", lambda: _Session())
    await selector.fetch_models_from_providers()
    cache = json.loads(selector.cache_file.read_text(encoding="utf-8"))
    assert cache == {"good": ["new-good"], "bad": ["last-known-bad"]}
    assert "new-good (good)" in selector.models
    assert "last-known-bad (bad)" in selector.models

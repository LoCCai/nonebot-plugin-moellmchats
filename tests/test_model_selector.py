from __future__ import annotations

import importlib
import json
from pathlib import Path
import re

import pytest

from nonebot_plugin_moellmchats import private_files
from nonebot_plugin_moellmchats.model_selector import ModelSelector

model_selector_module = importlib.import_module(
    "nonebot_plugin_moellmchats.model_selector"
)


_PROVIDERS_TOML = """\
[global_default]
stream = true

[providers.good]
base_url = "https://good.invalid/v1"
api_key = "one"
models = ["model-a"]
"""


def _model_config() -> dict:
    model = "model-a (good)"
    return {
        "use_moe": False,
        "moe_models": {"0": model, "1": model, "2": model},
        "selected_model": model,
        "category_model": model,
        "summary_model": model,
        "vision_model": "",
        "use_web_search": False,
        "use_tools": True,
        "tool_blacklist": [],
        "resident_plugins": [],
    }


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def _initialized_selector(tmp_path: Path, monkeypatch) -> ModelSelector:
    config_dir = tmp_path / "config"
    monkeypatch.setattr(model_selector_module, "config_path", config_dir)
    selector = ModelSelector()
    selector.providers_file.write_text(_PROVIDERS_TOML, encoding="utf-8")
    selector.models_file.write_text("{}", encoding="utf-8")
    selector.cache_file.write_text("{}", encoding="utf-8")
    selector.model_config_file.write_text(
        json.dumps(_model_config()),
        encoding="utf-8",
    )
    selector.load_providers(strict=True)
    selector._load_all_models(strict=True)
    selector._load_model_config()
    return selector


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
    selector.providers_file.write_text(
        """\
[providers.good]
base_url = "https://good.invalid/v1"
api_key = "one"

[providers.bad]
base_url = "https://bad.invalid/v1"
api_key = "two"
""",
        encoding="utf-8",
    )
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


def test_runtime_reads_revalidate_modes_after_initialization(
    tmp_path: Path,
    monkeypatch,
) -> None:
    selector = _initialized_selector(tmp_path, monkeypatch)
    config_dir = selector.providers_file.parent
    reads = [
        (
            lambda: selector.load_providers(strict=True),
            (selector.providers_file,),
        ),
        (
            lambda: selector._load_all_models(strict=True),
            (selector.models_file, selector.cache_file),
        ),
        (
            selector._load_model_config,
            (selector.model_config_file,),
        ),
        (
            selector.build_candidate,
            (
                selector.providers_file,
                selector.models_file,
                selector.cache_file,
                selector.model_config_file,
            ),
        ),
    ]

    for read, protected_files in reads:
        config_dir.chmod(0o755)
        for path in protected_files:
            path.chmod(0o644)

        read()

        assert _mode(config_dir) == 0o700
        assert all(_mode(path) == 0o600 for path in protected_files)


@pytest.mark.parametrize(
    ("filename", "reader"),
    [
        ("providers.toml", "providers"),
        ("models.json", "models"),
        ("model_cache.json", "models"),
        ("model_config.json", "model-config"),
        ("providers.toml", "candidate"),
    ],
)
def test_runtime_reads_reject_post_init_symlink_replacement(
    tmp_path: Path,
    monkeypatch,
    filename: str,
    reader: str,
) -> None:
    selector = _initialized_selector(tmp_path, monkeypatch)
    protected = selector.providers_file.parent / filename
    outside = tmp_path / f"outside-{filename}"
    outside.write_bytes(protected.read_bytes())
    protected.unlink()
    protected.symlink_to(outside)

    calls = {
        "providers": lambda: selector.load_providers(strict=True),
        "models": lambda: selector._load_all_models(strict=True),
        "model-config": selector._load_model_config,
        "candidate": selector.build_candidate,
    }
    with pytest.raises(
        private_files.PrivateStorageError,
        match=rf"禁止符号链接: .*{re.escape(filename)}",
    ):
        calls[reader]()


@pytest.mark.asyncio
async def test_provider_refresh_rejects_replaced_file_before_http_session(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from nonebot_plugin_moellmchats import utils

    selector = _initialized_selector(tmp_path, monkeypatch)
    outside = tmp_path / "outside-providers.toml"
    outside.write_text(_PROVIDERS_TOML, encoding="utf-8")
    selector.providers_file.unlink()
    selector.providers_file.symlink_to(outside)
    session_requested = False

    def forbidden_session():
        nonlocal session_requested
        session_requested = True
        raise AssertionError("HTTP session must not be acquired")

    monkeypatch.setattr(utils, "get_session", forbidden_session)

    with pytest.raises(
        private_files.PrivateStorageError,
        match=r"禁止符号链接: .*providers\.toml",
    ):
        await selector.fetch_models_from_providers()

    assert session_requested is False


@pytest.mark.asyncio
async def test_provider_refresh_rejects_replaced_cache_before_read(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from nonebot_plugin_moellmchats import utils

    selector = _initialized_selector(tmp_path, monkeypatch)
    outside = tmp_path / "outside-model-cache.json"
    outside.write_text('{"good": ["outside-model"]}', encoding="utf-8")
    selector.cache_file.unlink()
    selector.cache_file.symlink_to(outside)
    cache_read = False

    def forbidden_json_load(*_args, **_kwargs):
        nonlocal cache_read
        cache_read = True
        raise AssertionError("replaced cache must not be read")

    monkeypatch.setattr(utils, "get_session", lambda: _Session())
    monkeypatch.setattr(model_selector_module.json, "load", forbidden_json_load)

    with pytest.raises(
        private_files.PrivateStorageError,
        match=r"禁止符号链接: .*model_cache\.json",
    ):
        await selector.fetch_models_from_providers()

    assert cache_read is False

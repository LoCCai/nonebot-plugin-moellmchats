from unittest.mock import Mock

import pytest

from nonebot_plugin_moellmchats import config
from nonebot_plugin_moellmchats import qweb_resources as api


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "config_path", tmp_path)
    parser = config.ConfigParser()
    monkeypatch.setattr(api, "config_parser", parser)
    monkeypatch.setattr(api.runtime_snapshots, "patch_current", Mock())
    return parser


def test_save_is_atomic_preserves_unrelated_settings_and_updates_runtime(settings):
    before = api.emotion_settings()
    result = api.save_emotion_settings(revision=before["revision"], enabled=True, rate=.23)
    assert result["source"] == "meme_manager"
    assert result["rate"] == .23
    assert result["revision"] != before["revision"]
    assert api.emotion_settings() == result
    assert settings.get_config("emotion_rate") == .23
    api.runtime_snapshots.patch_current.assert_called_once()
    assert set(result) == {"enabled", "rate", "source", "revision"}


def test_stale_settings_do_not_overwrite_new_command(settings):
    before = api.emotion_settings()
    settings.set_config("emotion_rate", .4)
    with pytest.raises(ValueError, match="发生变化"):
        api.save_emotion_settings(revision=before["revision"], enabled=True, rate=.1)
    assert settings.get_config("emotion_rate") == .4


@pytest.mark.parametrize("value", [-.1, 1.1, float("nan"), True, ".2"])
def test_bad_rates_rejected_without_write(settings, value):
    before = api.emotion_settings()
    with pytest.raises(ValueError, match="无效"):
        api.save_emotion_settings(revision=before["revision"], enabled=True, rate=value)
    assert api.emotion_settings() == before

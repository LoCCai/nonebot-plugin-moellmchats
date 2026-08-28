from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from nonebot_plugin_moellmchats import config as config_module
from nonebot_plugin_moellmchats.config import DEFAULT_CONFIG, config_parser

_BOOLEAN_FIELDS = (
    "runtime_watch_enabled",
    "generated_tools_enabled",
    "fastai_enabled",
    "emotions_enabled",
    "private_chat_enabled",
    "show_datetime",
)


def _candidate(**overrides) -> dict:
    candidate = dict(DEFAULT_CONFIG)
    candidate.update(overrides)
    return candidate


@pytest.mark.parametrize("field", _BOOLEAN_FIELDS)
def test_boolean_fields_reject_truthy_strings(field: str) -> None:
    # 回归：这些开关此前不校验，配置成字符串 "false" 会按真值处理，
    # 功能实际上无法关闭
    with pytest.raises(ValueError, match=field):
        config_module.ConfigParser._validate(_candidate(**{field: "false"}))
    with pytest.raises(ValueError, match=field):
        config_module.ConfigParser._validate(_candidate(**{field: "true"}))


def test_default_config_passes_validation() -> None:
    config_module.ConfigParser._validate(_candidate())


def test_unknown_config_key_warns_and_is_ignored(tmp_path, monkeypatch) -> None:
    records: list[str] = []

    class _Recorder:
        def warning(self, message, *args, **kwargs) -> None:
            records.append(str(message).format(*args, **kwargs))

    monkeypatch.setattr(config_module, "logger", _Recorder())

    path = tmp_path / "config.json"
    payload = _candidate()
    payload["generated_tools_enabledd"] = True  # 拼写错误的键
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(config_parser, "filepath", path)

    candidate = config_parser.load_candidate()

    # 已知键不受影响，未识别键不会让加载失败，但有 warning 提示拼写问题
    assert candidate["generated_tools_enabled"] is True
    assert candidate["cd_seconds"] == DEFAULT_CONFIG["cd_seconds"]
    assert any("generated_tools_enabledd" in message for message in records)


def test_known_config_key_does_not_warn(tmp_path, monkeypatch) -> None:
    records: list[str] = []

    class _Recorder:
        def warning(self, message, *args, **kwargs) -> None:
            records.append(message)

    monkeypatch.setattr(config_module, "logger", _Recorder())

    path = tmp_path / "config.json"
    path.write_text(json.dumps(_candidate(cd_seconds=30)), encoding="utf-8")
    monkeypatch.setattr(config_parser, "filepath", path)

    candidate = config_parser.load_candidate()

    assert candidate["cd_seconds"] == 30
    assert records == []

from __future__ import annotations

import json

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
    "provider_catalog_categorize_enabled",
    "provider_catalog_llm_payload_enabled",
    "provider_catalog_llm_tools_enabled",
    "provider_catalog_pending_actions_enabled",
    "provider_catalog_search_enabled",
    "provider_catalog_management_enabled",
    "protocol_tools_enabled",
    "protocol_tools_napcat_extensions_enabled",
    "protocol_tools_low_risk_direct_enabled",
    "protocol_tools_business_first",
    "tool_progress_messages_enabled",
)


def _candidate(**overrides) -> dict:
    candidate = dict(DEFAULT_CONFIG)
    candidate.update(overrides)
    return candidate


@pytest.mark.parametrize("field", _BOOLEAN_FIELDS)
def test_boolean_fields_reject_truthy_strings(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        config_module.ConfigParser._validate(_candidate(**{field: "false"}))
    with pytest.raises(ValueError, match=field):
        config_module.ConfigParser._validate(_candidate(**{field: "true"}))


def test_default_config_passes_validation() -> None:
    config_module.ConfigParser._validate(_candidate())


def test_unknown_config_key_warns_and_is_preserved(tmp_path, monkeypatch) -> None:
    records: list[str] = []

    class _Recorder:
        def warning(self, message, *args, **kwargs) -> None:
            records.append(str(message).format(*args, **kwargs))

    monkeypatch.setattr(config_module, "logger", _Recorder())
    path = tmp_path / "config.json"
    payload = _candidate()
    payload["generated_tools_enabledd"] = True
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(config_parser, "filepath", path)

    candidate = config_parser.load_candidate()

    assert candidate["generated_tools_enabled"] is True
    assert candidate["generated_tools_enabledd"] is True
    assert any("generated_tools_enabledd" in message for message in records)

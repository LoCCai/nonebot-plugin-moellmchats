from __future__ import annotations

import json

from pydantic import ValidationError
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
    "plugin_command_progress_enabled",
    "tool_progress_model_preface_enabled",
    "tool_batch_sequential_fallback",
    "tool_image_vision_enabled",
)


def test_nonebot_mention_config_parses_env_values() -> None:
    disabled = config_module.MentionAckConfig(
        moellm_mention_ack_enabled="false",
        moellm_mention_ack_emoji_id="282",
    )
    assert disabled.moellm_mention_ack_enabled is False
    assert disabled.moellm_mention_ack_emoji_id == 282
    assert config_module.MentionAckConfig().moellm_mention_ack_enabled is True
    with pytest.raises(ValidationError):
        config_module.MentionAckConfig(moellm_mention_ack_emoji_id=-1)


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


@pytest.mark.parametrize("value", [0, -1, True, 16, 1.5, "15"])
def test_request_tool_count_must_be_an_integer_at_most_fifteen(value):
    with pytest.raises(ValueError, match="max_tool_calls_per_request"):
        config_module.ConfigParser._validate(_candidate(max_tool_calls_per_request=value))


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        "high",
        {"reasoning_effort": "max"},
        {"output_tokens": True},
        {"tool_rounds": "6"},
        {"tool_rounds": 6.0},
        {"tool_rounds": 13},
        {"output_tokens": 32769},
        {"output_tokens": 2047},
        {"request_seconds": 601},
        {"model_seconds": 181},
        {"tools_per_round": 9},
        {"model_attempts": 4},
        {"history_tokens": 16001},
        {"history_exchanges": 21},
        {"authorized_request_seconds": 3600},
        {"queue_wait_seconds": 30},
        {"reasoning_efort": "high"},
    ],
)
def test_ordinary_budget_rejects_invalid_or_non_ordinary_overrides(value):
    with pytest.raises(ValueError, match="ordinary_request_budget"):
        config_module.ConfigParser._validate(_candidate(ordinary_request_budget=value))


def test_ordinary_budget_allows_partial_hot_overrides_and_legacy_defaults():
    from nonebot_plugin_moellmchats.request_budget import budget_config, validate_ordinary_request_budget

    overrides = {"reasoning_effort": "high", "output_tokens": 8192, "tool_rounds": 6}
    config_module.ConfigParser._validate(_candidate(ordinary_request_budget=overrides))
    result = validate_ordinary_request_budget(overrides)
    assert result.moellm_ordinary_reasoning_effort == "high"
    assert result.moellm_ordinary_output_tokens == 8192
    assert result.moellm_ordinary_tool_rounds == 6
    assert result.moellm_ordinary_tools_per_round == budget_config.moellm_ordinary_tools_per_round
    assert validate_ordinary_request_budget({}) == budget_config


@pytest.mark.parametrize("value", [0, 0.05, 25, 60])
def test_classification_build_timeout_is_known_valid_config(value):
    assert DEFAULT_CONFIG["classification_build_timeout_seconds"] == 25
    config_module.ConfigParser._validate(_candidate(classification_build_timeout_seconds=value))


@pytest.mark.parametrize("value", [True, "25", -1, float("nan"), float("inf")])
def test_classification_build_timeout_rejects_invalid_config(value):
    with pytest.raises(ValueError, match="classification_build_timeout_seconds"):
        config_module.ConfigParser._validate(_candidate(classification_build_timeout_seconds=value))


@pytest.mark.parametrize(
    "reactions",
    [
        [],
        {"enabled": "false"},
        {"completed": True},
        {"started": -1},
        {"completed": "✔"},
        {"completed": "2147483648"},
        {"rejected": "-1"},
        {"completed": "1\n"},
        {"unknown": "76"},
    ],
)
def test_reaction_config_rejects_invalid_values(reactions):
    with pytest.raises(ValueError, match="message_reactions"):
        config_module.ConfigParser._validate(_candidate(message_reactions=reactions))


def test_reaction_config_reloads_custom_and_disabled_phases(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setattr(config_parser, "filepath", path)
    settings = {"enabled": True, "started": "76", "completed": "128077", "rejected": ""}
    path.write_text(json.dumps({"message_reactions": settings}))
    assert config_parser.load_candidate()["message_reactions"] == settings
    settings["completed"] = "10004"
    path.write_text(json.dumps({"message_reactions": settings}))
    assert config_parser.load_candidate()["message_reactions"]["completed"] == "10004"


def test_slow_plugin_defaults_and_multiple_tool_rounds() -> None:
    assert DEFAULT_CONFIG["request_timeout_seconds"] == 180
    assert DEFAULT_CONFIG["tool_timeout_seconds"] == 180
    assert DEFAULT_CONFIG["legacy_dispatch_timeout_seconds"] == 180
    assert DEFAULT_CONFIG["max_tool_rounds"] == 6
    assert DEFAULT_CONFIG["max_agent_steps"] == 6
    assert DEFAULT_CONFIG["max_tool_calls_per_round"] == 4


@pytest.mark.parametrize("value", [0, -1, True, "4"])
def test_tool_call_limit_must_be_a_positive_integer(value) -> None:
    with pytest.raises(ValueError, match="max_tool_calls_per_round"):
        config_module.ConfigParser._validate(_candidate(max_tool_calls_per_round=value))


@pytest.mark.parametrize("value", ["qi_zmws", [1], [""]])
def test_image_vision_exclusions_require_tool_names(value):
    with pytest.raises(ValueError, match="tool_image_vision_excluded_tools"):
        config_module.ConfigParser._validate(_candidate(tool_image_vision_excluded_tools=value))


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


def test_background_plugins_must_be_string_array() -> None:
    with pytest.raises(ValueError, match="legacy_background_plugins"):
        config_module.ConfigParser._validate(_candidate(legacy_background_plugins=["slow_report", 3]))
    with pytest.raises(ValueError, match="legacy_background_plugins"):
        config_module.ConfigParser._validate(_candidate(legacy_background_plugins="qi_post"))


def test_background_timeout_must_exceed_grace() -> None:
    with pytest.raises(ValueError, match="legacy_background_timeout_seconds"):
        config_module.ConfigParser._validate(_candidate(legacy_background_grace_seconds=5, legacy_background_timeout_seconds=5))
    with pytest.raises(ValueError, match="legacy_background_timeout_seconds"):
        config_module.ConfigParser._validate(
            _candidate(legacy_background_grace_seconds=300, legacy_background_timeout_seconds=30)
        )


def test_background_defaults_pass_validation() -> None:
    candidate = _candidate(legacy_background_plugins=["qi_post"])
    config_module.ConfigParser._validate(candidate)
    assert candidate["legacy_background_timeout_seconds"] == 300
    assert candidate["legacy_background_grace_seconds"] == 5

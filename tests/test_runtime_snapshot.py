from __future__ import annotations

import pytest

from nonebot_plugin_moellmchats.config import config_parser
from nonebot_plugin_moellmchats.runtime_snapshot import (
    RuntimeSnapshot,
    immutable_mapping,
    runtime_snapshots,
)


def _snapshot(generation: int, value: int) -> RuntimeSnapshot:
    return RuntimeSnapshot(
        generation=generation,
        config=immutable_mapping({"request_timeout_seconds": value}),
        model_state=None,
        temperaments=immutable_mapping({"默认": "prompt"}),
        temperament_assignments=immutable_mapping({}),
        replies=immutable_mapping({"hello": ("hi",), "poke": ("poke",)}),
        tool_snapshot=None,
        emotions=(),
        reloaded_at=0,
    )


def test_bound_request_keeps_old_generation_after_publish() -> None:
    old = _snapshot(1, 10)
    new = _snapshot(2, 20)
    runtime_snapshots.publish(old)
    with runtime_snapshots.bind(old):
        runtime_snapshots.publish(new)
        assert config_parser.get_config("request_timeout_seconds") == 10
    assert config_parser.get_config("request_timeout_seconds") == 20


def test_runtime_mapping_is_recursively_immutable() -> None:
    value = immutable_mapping({"nested": {"value": 1}, "items": [1, 2]})
    with pytest.raises(TypeError):
        value["nested"]["value"] = 2
    assert value["items"] == (1, 2)

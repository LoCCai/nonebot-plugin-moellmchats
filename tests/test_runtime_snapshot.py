from __future__ import annotations

import pytest

from nonebot_plugin_moellmchats.config import config_parser
from nonebot_plugin_moellmchats.model_selector import ModelRuntimeState
from nonebot_plugin_moellmchats.runtime_snapshot import (
    RuntimeSnapshot,
    RuntimeSnapshotStore,
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


def test_bound_request_keeps_old_generation_after_publish(monkeypatch) -> None:
    monkeypatch.setattr(runtime_snapshots, "_current", None)
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


def test_runtime_snapshot_constructor_detaches_all_runtime_collections() -> None:
    config = {"nested": {"value": 1}}
    temperaments = {"default": "prompt"}
    assignments = {"1": "default"}
    replies = {"hello": ["hi"]}
    emotions = ["smile"]
    snapshot = RuntimeSnapshot(
        generation=1,
        config=config,
        model_state=None,
        temperaments=temperaments,
        temperament_assignments=assignments,
        replies=replies,
        tool_snapshot=None,
        emotions=emotions,
        reloaded_at=0,
    )

    config["nested"]["value"] = 2
    temperaments["default"] = "changed"
    assignments["1"] = "changed"
    replies["hello"].append("changed")
    emotions.append("changed")

    assert snapshot.config["nested"]["value"] == 1
    assert snapshot.temperaments == {"default": "prompt"}
    assert snapshot.temperament_assignments == {"1": "default"}
    assert snapshot.replies["hello"] == ("hi",)
    assert snapshot.emotions == ("smile",)
    with pytest.raises(TypeError):
        snapshot.config["nested"]["value"] = 3


def test_model_runtime_state_constructor_detaches_nested_mappings() -> None:
    models = {"model": {"model": "old"}}
    state = ModelRuntimeState(
        models=models,
        providers={"provider": {"base_url": "https://example.invalid"}},
        global_default={},
        model_config={"selected_model": "model"},
    )
    snapshot = RuntimeSnapshot(
        generation=1,
        config={},
        model_state=state,
        temperaments={},
        temperament_assignments={},
        replies={},
        tool_snapshot=None,
        emotions=(),
        reloaded_at=0,
    )

    models["model"]["model"] = "changed"
    assert snapshot.model_state.models["model"]["model"] == "old"
    with pytest.raises(TypeError):
        snapshot.model_state.models["model"]["model"] = "tampered"


def test_runtime_snapshot_rejects_non_snapshot_members() -> None:
    common = {
        "generation": 1,
        "config": {},
        "temperaments": {},
        "temperament_assignments": {},
        "replies": {},
        "emotions": (),
        "reloaded_at": 0,
    }
    with pytest.raises(ValueError, match="model_state"):
        RuntimeSnapshot(**common, model_state={}, tool_snapshot=None)
    with pytest.raises(ValueError, match="tool_snapshot"):
        RuntimeSnapshot(**common, model_state=None, tool_snapshot={})
    with pytest.raises(ValueError, match="emotions"):
        RuntimeSnapshot(
            **{**common, "emotions": ("ok", object())},
            model_state=None,
            tool_snapshot=None,
        )


def test_generated_state_stamp_is_detached_and_immutable() -> None:
    active = {"weather": "a" * 64}
    snapshot = RuntimeSnapshot(
        generation=1,
        config=immutable_mapping({}),
        model_state=None,
        temperaments=immutable_mapping({}),
        temperament_assignments=immutable_mapping({}),
        replies=immutable_mapping({}),
        tool_snapshot=None,
        emotions=(),
        reloaded_at=0,
        generated_state_revision=7,
        generated_state_digest="b" * 64,
        generated_active=active,
    )
    active["weather"] = "c" * 64

    assert snapshot.generated_state_revision == 7
    assert snapshot.generated_state_digest == "b" * 64
    assert snapshot.generated_active == {"weather": "a" * 64}
    with pytest.raises(TypeError):
        snapshot.generated_active["weather"] = "d" * 64


def test_legacy_runtime_snapshot_constructor_gets_empty_generated_stamp() -> None:
    snapshot = _snapshot(1, 10)

    assert snapshot.generated_state_revision == 0
    assert snapshot.generated_state_digest == ""
    assert snapshot.generated_active == {}
    with pytest.raises(TypeError):
        snapshot.generated_active["new"] = "e" * 64


def test_publish_supports_identity_cas() -> None:
    store = RuntimeSnapshotStore()
    first = _snapshot(1, 10)
    second = _snapshot(2, 20)
    equal_but_distinct = _snapshot(1, 10)

    store.publish(first, expected_current=None)
    with pytest.raises(RuntimeError, match="CAS"):
        store.publish(second, expected_current=equal_but_distinct)
    assert store.current() is first

    store.publish(second, expected_current=first)
    assert store.current() is second


@pytest.mark.parametrize("generation", [1, 2])
def test_publish_rejects_non_increasing_generation(generation: int) -> None:
    store = RuntimeSnapshotStore()
    current = _snapshot(2, 20)
    store.publish(current)

    with pytest.raises(ValueError, match="严格递增"):
        store.publish(_snapshot(generation, 30), expected_current=current)
    assert store.current() is current


def test_patch_current_supports_identity_cas() -> None:
    store = RuntimeSnapshotStore()
    current = _snapshot(1, 10)
    equal_but_distinct = _snapshot(1, 10)
    store.publish(current)

    with pytest.raises(RuntimeError, match="CAS"):
        store.patch_current(
            expected_current=equal_but_distinct,
            config=immutable_mapping({"request_timeout_seconds": 99}),
        )
    assert store.current() is current

    store.patch_current(
        expected_current=current,
        config=immutable_mapping({"request_timeout_seconds": 30}),
    )
    patched = store.current()
    assert patched is not None
    assert patched is not current
    assert patched.generation == current.generation
    assert patched.config["request_timeout_seconds"] == 30

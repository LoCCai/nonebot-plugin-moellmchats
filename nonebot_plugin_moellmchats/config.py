from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from types import MappingProxyType
from typing import Any

from nonebot.log import logger
import nonebot_plugin_localstore as store
import ujson as json

from .private_files import (
    atomic_write_private_text,
    ensure_private_directory,
    ensure_private_file,
    harden_private_tree,
)

config_path: Path = store.get_plugin_config_dir()
MAX_CD_SECONDS = 86_400


DEFAULT_CONFIG: dict[str, Any] = {
    "max_group_history": 10,
    "max_user_history": 8,
    "max_history_chars": 16_000,
    "max_history_tokens": 4_000,
    "max_context_sessions": 1_000,
    "max_retry_times": 3,
    "max_tool_rounds": 6,
    "max_agent_steps": 6,
    "max_repeated_tool_calls": 2,
    "max_tool_result_chars": 6_000,
    "max_tool_images": 4,
    "request_timeout_seconds": 180,
    "classification_timeout_seconds": 20,
    "tool_timeout_seconds": 30,
    "pending_action_ttl_seconds": 120,
    "pending_action_max_entries": 256,
    "pending_action_max_argument_bytes": 16_384,
    "pending_action_failure_window_seconds": 60,
    "pending_action_max_failures": 8,
    "pending_action_max_failure_keys": 4_096,
    "llm_max_active": 4,
    "llm_max_pending": 32,
    "llm_max_per_user": 2,
    "legacy_dispatch_max_pending": 16,
    "legacy_dispatch_timeout_seconds": 20,
    "legacy_full_event_plugins": [],
    "member_cache_ttl_seconds": 600,
    "member_cache_max_entries": 4096,
    "member_lookup_timeout_seconds": 2,
    "runtime_watch_enabled": True,
    "runtime_watch_interval_seconds": 2,
    "provider_catalog_categorize_enabled": True,
    "provider_catalog_llm_payload_enabled": True,
    "provider_catalog_llm_tools_enabled": True,
    "provider_catalog_pending_actions_enabled": True,
    "provider_catalog_search_enabled": True,
    "provider_catalog_management_enabled": True,
    "protocol_tools_enabled": False,
    "protocol_tools_napcat_extensions_enabled": True,
    "protocol_tools_low_risk_direct_enabled": True,
    "protocol_tools_business_first": True,
    "tool_progress_messages_enabled": True,
    "tool_progress_model_preface_enabled": False,
    "user_history_expire_seconds": 600,
    "cd_seconds": 120,
    "search_api": "your api",
    "fastai_enabled": False,
    "emotions_enabled": False,
    "emotion_rate": 0.1,
    "emotions_dir": "absolute path",
    "private_chat_enabled": False,
    "show_datetime": False,
    "poke_llm_rate": 0.3,
    "generated_tools_enabled": True,
    "generated_tool_max_pending": 4,
    "generated_tool_timeout_seconds": 30,
    "generated_tool_cpu_seconds": 10,
    "generated_tool_memory_mb": 256,
    "generated_tool_output_bytes": 65_536,
    "generated_tool_workspace_mb": 64,
    "generated_tool_workspace_max_files": 256,
    "generated_tool_workspace_max_depth": 8,
    "generated_tool_workspace_max_file_bytes": 8_388_608,
    "generated_tool_max_processes": 16,
}

_POSITIVE_INTEGER_FIELDS = {
    "max_group_history",
    "max_user_history",
    "max_history_chars",
    "max_history_tokens",
    "max_context_sessions",
    "max_retry_times",
    "max_tool_rounds",
    "max_agent_steps",
    "max_repeated_tool_calls",
    "max_tool_result_chars",
    "max_tool_images",
    "request_timeout_seconds",
    "classification_timeout_seconds",
    "tool_timeout_seconds",
    "pending_action_ttl_seconds",
    "pending_action_max_entries",
    "pending_action_max_argument_bytes",
    "pending_action_failure_window_seconds",
    "pending_action_max_failures",
    "pending_action_max_failure_keys",
    "llm_max_active",
    "llm_max_pending",
    "llm_max_per_user",
    "legacy_dispatch_max_pending",
    "legacy_dispatch_timeout_seconds",
    "member_cache_ttl_seconds",
    "member_cache_max_entries",
    "member_lookup_timeout_seconds",
    "runtime_watch_interval_seconds",
    "user_history_expire_seconds",
    "generated_tool_max_pending",
    "generated_tool_timeout_seconds",
    "generated_tool_cpu_seconds",
    "generated_tool_memory_mb",
    "generated_tool_output_bytes",
    "generated_tool_workspace_mb",
    "generated_tool_workspace_max_files",
    "generated_tool_workspace_max_depth",
    "generated_tool_workspace_max_file_bytes",
    "generated_tool_max_processes",
}


class ConfigParser:
    """Backward-compatible configuration facade with atomic reload semantics."""

    def __init__(self) -> None:
        self.filepath = Path(config_path / "config.json")
        self._config: Mapping[str, Any] = MappingProxyType({})
        self._load_initial()

    @property
    def config(self) -> Mapping[str, Any]:
        return self._config

    @config.setter
    def config(self, value: Mapping[str, Any]) -> None:
        # Kept for old integrations that assign this attribute directly.
        self._config = MappingProxyType(deepcopy(dict(value)))

    def _load_initial(self) -> None:
        self._harden_storage()
        if not self.filepath.exists():
            self._write(DEFAULT_CONFIG)
        self.commit_candidate(self.load_candidate())

    def _harden_storage(self) -> None:
        ensure_private_directory(self.filepath.parent)
        # Approved versions are separately managed as owner-readable immutable
        # artifacts. Do not accidentally add write bits while tightening config.
        versions_dir = self.filepath.parent / "generated_tools" / "versions"
        harden_private_tree(
            self.filepath.parent,
            skip_children=(versions_dir,),
        )

    def parse_config(self) -> dict[str, Any]:
        """Compatibility alias returning a validated mutable copy."""
        return self.load_candidate()

    def load_candidate(self) -> dict[str, Any]:
        self._harden_storage()
        ensure_private_file(self.filepath)
        with self.filepath.open("r", encoding="utf-8") as file:
            loaded = json.load(file)
        if not isinstance(loaded, dict):
            raise ValueError("config.json 顶层必须是对象")

        unknown_keys = sorted(set(loaded) - set(DEFAULT_CONFIG))
        if unknown_keys:
            logger.warning(
                "config.json 包含未知配置项（不会生效，请检查拼写）: {}",
                ", ".join(unknown_keys),
            )

        candidate = deepcopy(DEFAULT_CONFIG)
        candidate.update(loaded)
        self._validate(candidate)
        return candidate

    @staticmethod
    def _validate(candidate: dict[str, Any]) -> None:
        cooldown_seconds = candidate.get("cd_seconds")
        if (
            not isinstance(cooldown_seconds, int)
            or isinstance(cooldown_seconds, bool)
            or not 0 <= cooldown_seconds <= MAX_CD_SECONDS
        ):
            raise ValueError(f"config.json: cd_seconds 必须是 0 到 {MAX_CD_SECONDS} 的整数")
        for field in _POSITIVE_INTEGER_FIELDS:
            value = candidate.get(field)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"config.json: {field} 必须是正整数")
        for field in ("emotion_rate", "poke_llm_rate"):
            value = candidate.get(field)
            if not isinstance(value, (int, float)) or not 0 <= value <= 1:
                raise ValueError(f"config.json: {field} 必须在 0 到 1 之间")
        full_event_plugins = candidate.get("legacy_full_event_plugins")
        if not isinstance(full_event_plugins, list) or not all(
            isinstance(item, str) and item.strip() for item in full_event_plugins
        ):
            raise ValueError("config.json: legacy_full_event_plugins 必须是字符串数组")
        for field in (
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
            "tool_progress_model_preface_enabled",
            "runtime_watch_enabled",
            "generated_tools_enabled",
            "fastai_enabled",
            "emotions_enabled",
            "private_chat_enabled",
            "show_datetime",
        ):
            if type(candidate.get(field)) is not bool:
                raise ValueError(f"config.json: {field} 必须是布尔值")

    def commit_candidate(self, candidate: Mapping[str, Any]) -> None:
        self._config = MappingProxyType(deepcopy(dict(candidate)))

    def reload(self) -> None:
        self.commit_candidate(self.load_candidate())

    def get_config(self, key: str, default: Any = None) -> Any:
        # An active LLM request remains pinned to the generation it admitted.
        from .runtime_snapshot import runtime_snapshots

        snapshot = runtime_snapshots.active()
        if snapshot is not None:
            return snapshot.config.get(key, default)
        return self._config.get(key, default)

    def set_config(self, key: str, value: Any) -> None:
        candidate = dict(self._config)
        candidate[key] = value
        self._validate(candidate)
        self._write(candidate)
        self.commit_candidate(candidate)
        from .runtime_snapshot import immutable_mapping, runtime_snapshots

        runtime_snapshots.patch_current(config=immutable_mapping(candidate))

    def _write(self, config: Mapping[str, Any]) -> None:
        self._harden_storage()
        atomic_write_private_text(
            self.filepath,
            json.dumps(dict(config), indent=4, ensure_ascii=False),
        )


config_parser = ConfigParser()

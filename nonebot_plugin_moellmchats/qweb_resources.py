"""Narrow Qiqi host interface; auth/CSRF/audit remain the QWeb host's job."""
from __future__ import annotations

import hashlib
import json

from .config import config_parser
from .runtime_snapshot import immutable_mapping, runtime_snapshots


def _view(config: dict) -> dict:
    value = {
        "enabled": config.get("emotions_enabled", False),
        "rate": config.get("emotion_rate", 0.1),
        "source": config.get("emotion_source", "directory"),
    }
    revision = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    return {**value, "revision": revision}


def emotion_settings() -> dict:
    return _view(config_parser.load_candidate())


def save_emotion_settings(*, revision: str, enabled: bool, rate: float) -> dict:
    """Persist and publish the three managed fields in one existing atomic write.

    Run synchronously on the Bot event loop, like ConfigParser.set_config, so
    command-based settings cannot interleave between the read and publication.
    No model calls, provider configuration or unrelated settings are returned.
    """
    if type(enabled) is not bool or type(rate) not in {int, float} or not 0 <= rate <= 1:
        raise ValueError("表情开关或触发概率无效")
    candidate = config_parser.load_candidate()
    if _view(candidate)["revision"] != revision:
        raise ValueError("设置已发生变化，请重新读取后保存")
    candidate.update(emotions_enabled=enabled, emotion_rate=rate, emotion_source="meme_manager")
    config_parser._validate(candidate)
    config_parser._write(candidate)
    config_parser.commit_candidate(candidate)
    runtime_snapshots.patch_current(config=immutable_mapping(candidate))
    return _view(candidate)

"""Explicit role pools and provider/model quota status, independent of snapshots."""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
import json
from pathlib import Path
import threading
import time

from nonebot.log import logger

from .private_files import atomic_write_private_text, ensure_private_directory, ensure_private_file


class ModelQuotaStatus:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        ensure_private_directory(path.parent)
        ensure_private_file(path)
        self.exhausted = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(self.exhausted, dict) or any(
            not isinstance(name, str) or not isinstance(row, dict) or row.get("reason") != "quota_exhausted"
            for name, row in self.exhausted.items()
        ):
            raise ValueError("model_quota_status.json 格式错误")

    def set(self, identity: str, status: int | None) -> None:
        with self.lock:
            updated = dict(self.exhausted)
            if status is None:
                updated.pop(identity, None)
            else:
                updated[identity] = {"reason": "quota_exhausted", "http_status": status, "at": time.time()}
            atomic_write_private_text(self.path, json.dumps(updated, ensure_ascii=False, indent=2))
            self.exhausted = updated


class ModelPoolMixin:
    @staticmethod
    def validate_model_pools(pools, models) -> None:
        if not isinstance(pools, Mapping) or any(role not in {"category", "0", "1", "2"} for role in pools):
            raise ValueError("model_pools 仅支持 category、0、1、2 四个角色")
        for names in pools.values():
            if (not isinstance(names, (list, tuple)) or not 1 <= len(names) <= 32
                    or not all(isinstance(name, str) and name in models for name in names)
                    or len(set(names)) != len(names)):
                raise ValueError("模型池必须包含 1 至 32 个存在且不重复的完整模型名")

    def _quota_status(self) -> ModelQuotaStatus:
        store = getattr(self, "_quota_store", None)
        if store is None:
            store = self._quota_store = ModelQuotaStatus(self.model_config_file.with_name("model_quota_status.json"))
        return store

    def is_model_exhausted(self, identity: str) -> bool:
        return identity in self._quota_status().exhausted

    async def mark_model_exhausted(self, model: Mapping, status: int) -> None:
        identity = f"{model.get('model')} ({model.get('provider')})"
        store = self._quota_status()
        if identity not in store.exhausted:
            try:
                await asyncio.to_thread(store.set, identity, status)
            except Exception:
                # Exclude immediately even if the state directory is unavailable.
                store.exhausted = {**store.exhausted, identity: {"reason": "quota_exhausted", "http_status": status}}
                logger.error("模型额度状态持久保存失败，当前进程已排除该模型：model={}", identity)
            logger.warning("模型额度耗尽，已退出模型池：model={} status={}", identity, status)

    async def restore_model_quota(self, query: str) -> str:
        name = self.resolve_model_name(query)
        if not name:
            return self._get_resolve_error_msg(query)
        await asyncio.to_thread(self._quota_status().set, name, None)
        return f"已清除 {name} 的额度耗尽标记，下次请求可重新选择。"

    def _select_pool_model(self, state, role, *, has_tools=False, has_images=False, attempted=None, rotate=False):
        from .runtime_snapshot import mutable_value

        names = state.model_config.get("model_pools", {}).get(role, ())
        eligible = []
        for name in names:
            if (attempted is not None and name in attempted) or self.is_model_exhausted(name):
                continue
            model = mutable_value(state.models[name])
            if (has_tools and model.get("no_tools")) or (has_images and not model.get("is_vision")):
                continue
            key = self._get_random_key(model)
            if key:
                model.update(key=key, _model_pool=role)
                eligible.append((name, model))
        if not eligible:
            return None
        offsets = getattr(self, "_pool_offsets", {})
        index = offsets.get(role, 0) % len(eligible) if rotate else 0
        if rotate:
            self._pool_offsets = {**offsets, role: offsets.get(role, 0) + 1}
        name, model = eligible[index]
        if attempted is not None:
            attempted.add(name)
        return model

    def format_model_pools(self) -> str:
        state = self._active_state()
        lines = []
        for role, names in state.model_config.get("model_pools", {}).items():
            label = "分类" if role == "category" else f"第{int(role) + 1}档"
            active = sum(not self.is_model_exhausted(name) for name in names)
            lines.append(f"▪ {label}模型池（{active}/{len(names)}）：" + "、".join(
                name + (" [额度耗尽]" if self.is_model_exhausted(name) else "") for name in names))
        return "\n".join(lines)

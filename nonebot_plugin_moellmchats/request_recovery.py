"""Owner-private request checkpoints and an acknowledgement-aware reply outbox.

Never replay an in-flight tool batch or an uncertain adapter send. No provider
configuration, model headers or authentication keys belong in this journal.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import time

from nonebot.exception import FinishedException
from nonebot.log import logger
import nonebot_plugin_localstore as localstore

from .config import recovery_config
from .onebot_facade import bot_self_id, event_message_id, onebot_protocol
from .private_files import atomic_write_private_text, ensure_private_directory, ensure_private_file

_MAX_RECORD_BYTES = 32 * 1024 * 1024
_binding: ContextVar[RecoveryBinding | None] = ContextVar("moellm_recovery", default=None)


class RestartDeferred(asyncio.CancelledError):
    """Execution has a durable checkpoint and will continue after startup."""


class RecoveryStorageError(RuntimeError):
    pass


class RecoveryStore:
    def __init__(self, root: Path):
        self.root = root
        self._owner = None

    def open(self) -> None:
        if self._owner is not None:
            return
        ensure_private_directory(self.root)
        path = self.root / ".owner.lock"
        ensure_private_file(path)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        owner = os.fdopen(fd, "a+")
        try:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            owner.close()
            raise RecoveryStorageError("任务恢复目录已被另一个进程占用") from None
        self._owner = owner

    def close(self) -> None:
        if self._owner is not None:
            self._owner.close()
            self._owner = None

    def path(self, key: str) -> Path:
        if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
            raise RecoveryStorageError("任务编号格式错误")
        return self.root / f"{key}.json"

    def read(self, key: str) -> dict | None:
        path = self.path(key)
        ensure_private_file(path)
        try:
            if path.stat().st_size > _MAX_RECORD_BYTES:
                raise RecoveryStorageError("任务记录超过大小限制")
            record = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        if not isinstance(record, dict) or record.get("version") != 1 or record.get("key") != key:
            raise RecoveryStorageError("任务记录格式错误")
        for field, kind in (("event", dict), ("formatted", dict), ("outbox", list), ("checkpoint", list),
                            ("inflight_tools", list), ("created_at", (int, float)), ("attempts", int),
                            ("state", str), ("protocol", str), ("bot_id", str)):
            if not isinstance(record.get(field), kind):
                raise RecoveryStorageError("任务记录字段错误")
        if any(not isinstance(entry, dict) or entry.get("state") not in
               {"pending", "sending", "sent", "unknown", "cancelled"} for entry in record["outbox"]):
            raise RecoveryStorageError("任务发件箱格式错误")
        return record

    def records(self) -> list[dict]:
        result = []
        for path in sorted(self.root.glob("*.json")):
            try:
                record = self.read(path.stem)
            except (ValueError, OSError, RecoveryStorageError):
                logger.warning("LLM 跳过损坏的任务记录：task={}", path.stem[:12])
                continue
            if record is not None:
                result.append(record)
        return result

    def save(self, record: dict) -> None:
        raw = json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        size = len(raw.encode("utf-8"))
        if size > _MAX_RECORD_BYTES:
            raise RecoveryStorageError("任务记录超过大小限制")
        target = self.path(record["key"])
        paths = list(self.root.glob("*.json"))
        total = size
        for path in paths:
            ensure_private_file(path)
            if path != target:
                total += path.stat().st_size
        count = len(paths) + int(not target.exists())
        if count > recovery_config.moellm_recovery_max_entries or total > recovery_config.moellm_recovery_max_bytes:
            # Completed records are deduplication tombstones, never a reason to
            # reject new work while all actual pending tasks fit the bound.
            for candidate in sorted(self.records(), key=lambda r: r["created_at"]):
                if candidate["key"] == record["key"] or candidate["state"] in {"running", "pending", "recovering"}:
                    continue
                if any(e["state"] == "pending" or (e["state"] in {"unknown", "sending"}
                       and not candidate.get("uncertainty_notified")) for e in candidate["outbox"]):
                    continue
                path = self.path(candidate["key"])
                total -= path.stat().st_size
                path.unlink()
                count -= 1
                if count <= recovery_config.moellm_recovery_max_entries and total <= recovery_config.moellm_recovery_max_bytes:
                    break
            if count > recovery_config.moellm_recovery_max_entries or total > recovery_config.moellm_recovery_max_bytes:
                raise RecoveryStorageError("待处理任务存储已满")
        atomic_write_private_text(target, raw)
        # atomic_write_private_text fsyncs the contents; sync the rename too.
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def prune(self) -> None:
        cutoff = time.time() - recovery_config.moellm_recovery_retention_seconds
        for record in self.records():
            if record["created_at"] < cutoff:
                self.path(record["key"]).unlink()


class RecoveryBinding:
    def __init__(self, service: RecoveryService, record: dict):
        self.service, self.record = service, record

    async def save(self) -> None:
        await self.service.io(self.service.store.save, deepcopy(self.record))

    async def checkpoint(self, messages: list, *, phase: str, calls: list | None = None) -> None:
        # Rebuild the system prompt and permissions from the current runtime.
        self.record["checkpoint"] = [deepcopy(m) for m in messages if m.get("role") != "system"]
        self.record["phase"] = phase
        self.record["inflight_tools"] = deepcopy(calls or [])
        await self.save()

    def pause_if_stopping(self) -> None:
        if self.service.stopping:
            raise RestartDeferred()


def current_binding() -> RecoveryBinding | None:
    return _binding.get()


def restarting() -> bool:
    return recovery_service.stopping


def resume_checkpoint(record: dict) -> tuple[list, list, bool]:
    """Repair only the interrupted tool turn; IDs may repeat in older turns."""
    messages, completed, pending = [], [], {}
    unsafe = bool(record.get("unsafe_tools") or record["inflight_tools"])

    def close_turn():
        nonlocal unsafe
        for call in pending.values():
            unsafe = True
            messages.append({"role": "tool", "tool_call_id": call["id"],
                "content": "重启打断了结果确认；此操作可能已经执行，不得自动重放。请提示用户核对。"})
        pending.clear()

    for message in deepcopy(record["checkpoint"]):
        if message.get("role") != "tool":
            close_turn()
        messages.append(message)
        if message.get("role") == "assistant":
            pending.update({c["id"]: c for c in message.get("tool_calls") or []})
        elif message.get("role") == "tool":
            if call := pending.pop(message.get("tool_call_id"), None):
                completed.append(call)
    close_turn()
    unsafe = unsafe or any(e["state"] in {"unknown", "sending"} for e in record["outbox"])
    return messages, completed, unsafe


@contextmanager
def bind_recovery(binding: RecoveryBinding):
    token = _binding.set(binding)
    try:
        yield binding
    finally:
        _binding.reset(token)


def _message_json(bot, message) -> list:
    # Adapter Message preserves image/base64 and real mention segments.
    if onebot_protocol(bot) == "onebot_v12":
        from nonebot.adapters.onebot.v12 import Message, MessageSegment
    else:
        from nonebot.adapters.onebot.v11 import Message, MessageSegment
    if isinstance(message, list):
        message = [MessageSegment(**segment) if isinstance(segment, dict) else segment for segment in message]
    return [
        {"type": segment.type, "data": segment.data} for segment in Message(message)
    ]


async def durable_send(bot, event, message, **kwargs):
    """Return None for a saved delivery; never manufacture a successful ack."""
    binding = current_binding()
    if binding is None:
        return await bot.send(event, message, **kwargs)
    entry = {"message": _message_json(bot, message), "kwargs": kwargs, "state": "pending"}
    binding.record["outbox"].append(entry)
    await binding.save()
    if binding.service.stopping:
        return None
    return await _deliver_entry(binding, bot, event, entry)


async def durable_reply(bot, event, messages: list) -> None:
    """Save the complete final reply, including attachments, in one transaction."""
    binding = current_binding()
    if binding is None:
        for message in messages:
            await bot.send(event, message)
        return
    entries = [{"message": _message_json(bot, message), "kwargs": {}, "state": "pending"} for message in messages]
    binding.record["outbox"].extend(entries)
    binding.record["answer_ready"] = True
    await binding.save()
    for entry in entries:
        if binding.service.stopping:
            return
        await _deliver_entry(binding, bot, event, entry)


def has_delivery_receipt(receipt) -> bool:
    if not isinstance(receipt, dict):
        return False
    message_id = receipt.get("message_id")
    return isinstance(message_id, (int, str)) and not isinstance(message_id, bool) and str(message_id) not in {"", "0"}


async def _deliver_entry(binding, bot, event, entry):
    entry["state"] = "sending"
    await binding.save()
    try:
        protocol = binding.record["protocol"]
        if protocol == "onebot_v12":
            from nonebot.adapters.onebot.v12 import Message, MessageSegment
        else:
            from nonebot.adapters.onebot.v11 import Message, MessageSegment
        receipt = await bot.send(event, Message([MessageSegment(**s) for s in entry["message"]]), **entry["kwargs"])
        if not has_delivery_receipt(receipt):
            raise RuntimeError("发送未返回可确认的消息回执，已保留为结果未知")
    except BaseException:
        # The adapter may have accepted the message. A restart must not replay it.
        entry["state"] = "unknown"
        await binding.save()
        raise
    entry["state"] = "sent"
    await binding.save()
    return receipt


class RecoveryService:
    def __init__(self):
        self.store: RecoveryStore | None = None
        self.enabled = False
        self.stopping = False
        self._io_lock = asyncio.Lock()
        self.tasks: set[asyncio.Task] = set()
        self.workers: dict[str, asyncio.Task] = {}
        self.authorize = None
        self.resume = None
        self._claimed: set[str] = set()

    async def io(self, operation, *args):
        async with self._io_lock:
            task = asyncio.create_task(asyncio.to_thread(operation, *args))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # A short fsync must finish before another journal transaction.
                await task
                raise

    async def start(self, *, root: Path | None = None) -> None:
        if self.enabled:
            return
        if not recovery_config.moellm_recovery_enabled:
            return
        self.store = RecoveryStore(root or localstore.get_plugin_data_dir() / "request_recovery")
        await self.io(self.store.open)
        await self.io(self.store.prune)
        self.enabled, self.stopping = True, False

    async def run(self, bot, event, formatted: dict, is_ai: bool, operation):
        if self.stopping:
            raise RestartDeferred()
        if current_binding() is not None:
            return await operation()
        if not self.enabled or onebot_protocol(bot, event) is None or event_message_id(event) is None:
            return await operation()
        identity = [onebot_protocol(bot, event), bot_self_id(bot, event), event.get_session_id(), event_message_id(event)]
        key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        if key in self._claimed:
            return None
        self._claimed.add(key)
        try:
            return await self._admit(key, identity, bot, event, formatted, is_ai, operation)
        finally:
            self._claimed.discard(key)

    async def _admit(self, key, identity, bot, event, formatted, is_ai, operation):
        existing = await self.io(self.store.read, key)
        if existing is not None:
            return None
        dump = event.model_dump(mode="json") if hasattr(event, "model_dump") else json.loads(event.json())
        record = {"version": 1, "key": key, "protocol": identity[0], "bot_id": identity[1],
                  "created_at": time.time(), "event": dump, "formatted": formatted, "is_ai": bool(is_ai),
                  "state": "running", "phase": "input", "checkpoint": [], "inflight_tools": [],
                  "outbox": [], "attempts": 0}
        binding = RecoveryBinding(self, record)
        # Admission is durable before any model or tool work starts.
        await binding.save()
        return await self.execute(binding, operation)

    async def execute(self, binding: RecoveryBinding, operation):
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            with bind_recovery(binding):
                binding.pause_if_stopping()
                result = await operation()
        except asyncio.CancelledError:
            binding.record["state"] = "pending" if self.stopping else "cancelled"
            await binding.save()
            raise
        except FinishedException:
            binding.record["state"] = "failed" if binding.record.get("model_failed") else "complete"
            await binding.save()
            raise
        except Exception:
            binding.record["state"] = "failed"
            await binding.save()
            raise
        else:
            binding.record["state"] = "failed" if binding.record.get("model_failed") else "complete"
            await binding.save()
            return result
        finally:
            self.tasks.discard(task)

    def connect(self, bot) -> None:
        key = str(bot.self_id)
        if not self.enabled or self.stopping or (key in self.workers and not self.workers[key].done()):
            return
        task = asyncio.create_task(self.recover(bot), name="moellm-request-recovery")
        self.workers[key] = task
        task.add_done_callback(self._worker_finished)

    @staticmethod
    def _worker_finished(task) -> None:
        if not task.cancelled() and task.exception() is not None:
            logger.error("LLM 任务恢复失败：error_type={}", type(task.exception()).__name__)

    async def recover(self, bot) -> None:
        failed = set()
        for deliveries_only in (True, False):
            await self._recover_pass(bot, deliveries_only=deliveries_only, failed=failed)

    async def _recover_pass(self, bot, *, deliveries_only: bool, failed: set) -> None:
        records = await self.io(self.store.records)
        for record in sorted(records, key=lambda r: r["created_at"]):
            if self.stopping:
                return
            if record["bot_id"] != str(bot.self_id) or record["protocol"] != onebot_protocol(bot):
                continue
            if record["key"] in failed or record["state"] in {"denied", "cancelled", "abandoned"}:
                continue
            unfinished = record["state"] in {"running", "pending", "recovering"}
            deliveries = [e for e in record["outbox"] if e["state"] == "pending"]
            if deliveries_only and not deliveries:
                continue
            uncertain = any(e["state"] in {"unknown", "sending"} for e in record["outbox"])
            if not unfinished and not deliveries and not (uncertain and not record.get("uncertainty_notified")):
                continue
            if record["created_at"] < time.time() - recovery_config.moellm_recovery_retention_seconds:
                continue
            binding = RecoveryBinding(self, record)
            try:
                event = await self.authorize(bot, record) if self.authorize is not None else None
                if event is None:
                    record["state"] = "denied"
                    for entry in deliveries:
                        entry["state"] = "cancelled"
                    await binding.save()
                    continue
                if not deliveries_only or record.get("answer_ready") or not unfinished:
                    record["attempts"] += 1
                if record["attempts"] > 3:
                    record["state"] = "abandoned"
                    for entry in deliveries:
                        entry["state"] = "cancelled"
                    await binding.save()
                    continue
                # Save each notice with its own delivery state too.
                with bind_recovery(binding):
                    if not record.get("recovery_notice") or (uncertain and not record.get("uncertainty_notified")):
                        record["recovery_notice"] = True
                        from .onebot_facade import make_mention_message
                        user = str(event.user_id)
                        text = "重启前的任务已保留，正在补发结果。" if deliveries else "重启前的任务已恢复，继续处理。"
                        if record["inflight_tools"] or uncertain:
                            text += "部分操作或消息的结果未确认，将保留现状并提示核对。"
                            record["uncertainty_notified"] = True
                        message = make_mention_message(record["protocol"], f"[CQ:at,qq={user}] {text}", {})
                        await durable_send(bot, event, message)
                    # All saved results are delivered before another model turn.
                    for entry in deliveries:
                        if self.stopping:
                            raise RestartDeferred()
                        await _deliver_entry(binding, bot, event, entry)
                if not unfinished or record.get("answer_ready"):
                    record["state"] = "complete"
                    await binding.save()
                    continue
                if deliveries_only:
                    await binding.save()
                    continue
                record["state"] = "recovering"
                await binding.save()
                if self.resume is None:
                    raise RuntimeError("恢复处理器未安装")
                await self.execute(binding, lambda: self.resume(bot, event, record))
            except asyncio.CancelledError:
                if self.stopping:
                    record["state"] = "pending" if unfinished else record["state"]
                    await binding.save()
                raise
            except FinishedException:
                continue
            except Exception as error:
                failed.add(record["key"])
                record["state"] = "pending" if unfinished and record["attempts"] < 3 else "failed"
                await binding.save()
                logger.warning("LLM 任务恢复未完成：task={} error_type={}", record["key"][:12], type(error).__name__)

    def begin_shutdown(self) -> None:
        self.stopping = True

    async def drain(self, *, grace: float | None = None) -> None:
        self.begin_shutdown()
        pending = {t for t in (*self.tasks, *self.workers.values()) if not t.done() and t is not asyncio.current_task()}
        if not pending:
            return
        _, pending = await asyncio.wait(pending, timeout=recovery_config.moellm_restart_grace_seconds if grace is None else grace)
        for task in pending:
            task.cancel()
        if pending:
            _, pending = await asyncio.wait(pending, timeout=recovery_config.moellm_restart_cancel_seconds)
        if pending:
            logger.warning("LLM 关停仍有 {} 个任务清理中，已持久保留检查点", len(pending))


recovery_service = RecoveryService()

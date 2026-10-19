"""Bounded, administrator-initiated retries for provably unexecuted requests."""
from __future__ import annotations

import asyncio
import time

from nonebot import get_bot
from nonebot.exception import FinishedException
from nonebot.log import logger

from .request_recovery import RecoveryBinding, recovery_service


def retry_denial(record: dict) -> str:
    if record.get('state') != 'failed':
        return '仅失败请求允许重试'
    if record.get('manual_retries', 0) >= 3:
        return '已达到三次人工重试上限'
    if record.get('inflight_tools') or record.get('unsafe_tools') or record.get('blocked_tools'):
        return '工具结果不确定，不能重放'
    if any(message.get('tool_calls') or message.get('role') == 'tool' for message in record.get('checkpoint', [])):
        return '已有工具执行记录，不能重放'
    if record.get('outbox') or record.get('answer_ready'):
        return '已有结果投递记录，不能重放'
    if time.time() - record['created_at'] > 86400:
        return '请求已超过保留期'
    return ''


class FailedRequests:
    def __init__(self):
        self._lock = asyncio.Lock()
        self.tasks: dict[str, asyncio.Task] = {}

    async def list(self):
        if not recovery_service.enabled:
            return []
        records = await recovery_service.io(recovery_service.store.records)
        return [{"id": row['key'], "user_id": str(row['event'].get('user_id', '')),
                 "group_id": str(row['event'].get('group_id', '')), "created_at": row['created_at'],
                 "status": row['state'], "retries": row.get('manual_retries', 0),
                 "retryable": not retry_denial(row), "reason": retry_denial(row)}
                for row in sorted(records, key=lambda row: row['created_at'], reverse=True)
                if row['state'] == 'failed' or row.get('manual_retries') and row['state'] in {'pending', 'running'}][:50]

    async def retry(self, key: str, *, actor: str) -> dict:
        async with self._lock:
            if not recovery_service.enabled or recovery_service.stopping:
                raise ValueError('LLM 恢复服务尚未就绪')
            if self.tasks:
                raise ValueError('已有后台重试任务，请等待其结束')
            record = await recovery_service.io(recovery_service.store.read, key)
            if record is None:
                raise ValueError('请求不存在或已过期')
            if reason := retry_denial(record):
                raise ValueError(reason)
            try:
                bot = get_bot(record['bot_id'])
            except KeyError:
                raise ValueError('原机器人当前未连接') from None
            record.update(state='pending', manual_retries=record.get('manual_retries', 0) + 1,
                          retry_actor=actor, model_failed=False)
            await recovery_service.io(recovery_service.store.save, record)
            task = asyncio.create_task(self._run(bot, record), name='llm-admin-retry')
            self.tasks[key] = task
            # Register immediately so shutdown can cancel a not-yet-started retry.
            recovery_service.tasks.add(task)
            task.add_done_callback(lambda done: self._finished(key, done))
            return {'id': key, 'status': 'retry_pending'}

    def _finished(self, key, task):
        self.tasks.pop(key, None)
        recovery_service.tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error('LLM 后台重试结束：task={} error_type={}', key[:12], type(task.exception()).__name__)

    async def _run(self, bot, record):
        binding = RecoveryBinding(recovery_service, record)
        try:
            event = await recovery_service.authorize(bot, record)
            if event is None:
                record['state'] = 'denied'
                await binding.save()
                return
            record['state'] = 'running'
            await binding.save()
            await recovery_service.execute(binding, lambda: recovery_service.resume(bot, event, record))
        except FinishedException:
            pass
        except asyncio.CancelledError:
            # Keep the recovery checkpoint for the normal shutdown workflow.
            raise
        except Exception:
            # Authorization itself can fail before execute() owns the record.
            if record['state'] in {'pending', 'running'}:
                record['state'] = 'failed'
                await binding.save()
            raise


failed_requests = FailedRequests()

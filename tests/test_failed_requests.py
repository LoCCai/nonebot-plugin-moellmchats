from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from test_request_recovery import bot, event, service
from nonebot_plugin_moellmchats import failed_requests as module
from nonebot_plugin_moellmchats.request_recovery import current_binding


async def failed_record(service):
    async def fail():
        raise TimeoutError
    with pytest.raises(TimeoutError):
        await service.run(bot(), event(), {'text': ['查询']}, False, fail)
    return (await service.io(service.store.records))[0]


@pytest.mark.asyncio
async def test_retry_is_background_serialized_and_nested_resume_is_not_deduplicated(service, monkeypatch):
    record = await failed_record(service)
    target, gate = bot(), asyncio.Event()
    monkeypatch.setattr(module, 'recovery_service', service)
    monkeypatch.setattr(module, 'get_bot', lambda _: target)
    async def authorize(*args):
        await gate.wait()
        return event()
    service.authorize = authorize
    performed = []
    async def operation():
        performed.append(True)
    async def resume(*args):
        return await service.run(target, event(), {}, False, operation)
    service.resume = resume
    retries = module.FailedRequests()
    receipt = await retries.retry(record['key'], actor='90001')
    assert receipt['status'] == 'retry_pending' and not performed
    with pytest.raises(ValueError, match='已有后台'):
        await retries.retry(record['key'], actor='90001')
    task = retries.tasks[record['key']]
    assert task in service.tasks
    gate.set()
    await task
    await asyncio.sleep(0)
    saved = await service.io(service.store.read, record['key'])
    assert performed == [True] and saved['state'] == 'complete'
    assert saved['manual_retries'] == 1 and not retries.tasks
    with pytest.raises(ValueError, match='仅失败'):
        await retries.retry(record['key'], actor='90001')


@pytest.mark.asyncio
async def test_retry_authorization_error_is_persisted_without_running_request(service, monkeypatch):
    record = await failed_record(service)
    monkeypatch.setattr(module, 'recovery_service', service)
    service.authorize = AsyncMock(side_effect=ConnectionError)
    retries = module.FailedRequests()
    with pytest.raises(ConnectionError):
        await retries._run(bot(), record)
    assert (await service.io(service.store.read, record['key']))['state'] == 'failed'
    service.resume.assert_not_awaited()


@pytest.mark.asyncio
async def test_handled_model_failure_remains_failed_instead_of_complete(service):
    async def operation():
        current_binding().record['model_failed'] = True
        return False
    await service.run(bot(), event(), {}, False, operation)
    assert (await service.io(service.store.records))[0]['state'] == 'failed'

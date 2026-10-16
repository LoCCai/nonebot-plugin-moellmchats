import asyncio
import json
from types import SimpleNamespace

import pytest

from nonebot_plugin_moellmchats import usage_ledger as module
from nonebot_plugin_moellmchats.usage_ledger import UsageLedger, apply_glm_policy, bind_usage_scope, budgeted_post, normalize_usage, record_usage
from nonebot_plugin_moellmchats import usage_reports as reports


MODEL = {"provider": "bigmodel", "model": "glm-5.3-flash"}
NAME = "glm-5.3-flash (bigmodel)"


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    value = UsageLedger(tmp_path / 'private' / 'usage.sqlite3')
    value.initialize()
    monkeypatch.setattr(module, '_ledger', value)
    monkeypatch.setattr(module, '_budget_locks', {})
    return value


def test_usage_counts_cache_reasoning_as_subsets_and_preserves_numeric_details():
    data = normalize_usage({'prompt_tokens': 100, 'completion_tokens': 50,
        'prompt_tokens_details': {'cached_tokens': 70},
        'completion_tokens_details': {'reasoning_tokens': 40},
        'other_tokens': 9, 'unsafe': 'secret', 'total_tokens': 999})
    assert (data['total'], data['cache'], data['reasoning']) == (150, 70, 40)
    assert data['raw_usage']['other_tokens'] == 9
    assert 'unsafe' not in data['raw_usage']
    assert normalize_usage({'prompt_tokens': True, 'completion_tokens': -1})['total'] == 0
    assert normalize_usage({'total_tokens': 90})['total'] == 90
    assert normalize_usage(None)['usage_known'] == 0


def test_stream_updates_are_idempotent_and_budget_survives_restart(ledger):
    ledger.configure_budget(NAME, 200)
    ledger.record(MODEL, {'prompt_tokens': 100, 'completion_tokens': 30}, attempt_id='stream')
    ledger.record(MODEL, {'prompt_tokens': 100, 'completion_tokens': 100}, attempt_id='stream')
    ledger.record(MODEL, {'prompt_tokens': 100, 'completion_tokens': 100}, attempt_id='stream')
    ledger.record(MODEL, {'prompt_tokens': 20}, attempt_id='stream')
    assert ledger.totals[NAME] == 200
    assert len(ledger.rows()) == 1
    restarted = UsageLedger(ledger.path)
    restarted.initialize()
    assert restarted.exhausted(NAME)
    restarted.configure_budget(NAME, 200)
    assert restarted.exhausted(NAME)  # startup must not silently refill
    restarted.configure_budget(NAME, 50, reset=True, actor='1143785758')
    assert restarted.remaining(NAME) == 50
    assert len(restarted.rows()) == 1
    assert restarted.totals[NAME] == 200


@pytest.mark.asyncio
async def test_scope_isolated_by_group_and_user_and_private(ledger):
    bot = SimpleNamespace(self_id='77')
    def event(user, group=None):
        return SimpleNamespace(get_user_id=lambda: user, group_id=group)
    async def call(user, group=None):
        with bind_usage_scope(bot, event(user, group)):
            await asyncio.sleep(0)
            await record_usage(MODEL, {'prompt_tokens': 3}, purpose='classification')
    await asyncio.gather(call('11', '100'), call('11', '200'), call('22', '100'), call('11'))
    assert len(ledger.rows(user_id='11')) == 3
    assert len(ledger.rows(group_id='100', user_id='11')) == 1
    assert len({r['request_id'] for r in ledger.rows()}) == 4
    assert ledger.rows(group_id='')[0]['scene'] == 'private'


@pytest.mark.asyncio
async def test_budget_admission_waits_for_inflight_accounting(ledger):
    ledger.configure_budget(NAME, 10)
    calls = []
    class Response:
        status = 200
        async def __aenter__(self):
            calls.append(1)
            return self
        async def __aexit__(self, *args):
            return False
    session = SimpleNamespace(post=lambda: Response())
    async def request():
        async with budgeted_post(session, MODEL) as response:
            if response.status == 200:
                await asyncio.sleep(.02)
                await record_usage(MODEL, {'prompt_tokens': 10})
            return response.status
    assert await asyncio.gather(request(), request()) == [200, 429]
    assert await request() == 403
    assert len(calls) == 1
    assert ledger.totals[NAME] == 10
    assert len(ledger.rows()) == 1


@pytest.mark.asyncio
async def test_failed_attempt_keeps_unknown_usage_and_scope(ledger):
    class Response:
        async def __aenter__(self):
            raise TimeoutError('network')
        async def __aexit__(self, *args):
            return False
    with pytest.raises(TimeoutError):
        async with budgeted_post(SimpleNamespace(post=lambda: Response()), MODEL):
            pass
    rows = ledger.rows()
    assert len(rows) == 1 and rows[0]['usage_known'] == 0
    assert rows[0]['status'] == 'interrupted_usage_unknown'


def test_glm_forces_only_supported_model_and_mandatory_parameters():
    payload = {'temperature': 0, 'thinking': {'type': 'disabled'}, 'max_tokens': 512, 'stream': True,
               'messages': [{'role': 'user', 'content': [{'type': 'file', 'file': {'file_url': 'https://example.org/a.pdf'}}]}]}
    apply_glm_policy(MODEL, payload)
    assert payload['temperature'] == 1 and payload['top_p'] == .95
    assert payload['thinking'] == {'type': 'enabled', 'clear_thinking': False}
    assert payload['reasoning_effort'] == 'max' and payload['tool_stream'] is True
    assert payload['max_tokens'] >= 8192
    assert payload['messages'][0]['content'][0]['type'] == 'file'
    with pytest.raises(ValueError):
        apply_glm_policy({'provider': 'bigmodel', 'model': 'glm-5.3'}, {})


def test_prices_cache_and_history_snapshot_are_not_retroactively_changed(ledger):
    prices = reports.newapi_prices({'ModelRatio': {'test': 1}, 'CompletionRatio': {'test': 3}, 'CacheRatio': {'test': .1}})
    ledger.put_prices(prices)
    model = {'model': 'test', 'provider': 'a'}
    ledger.record(model, {'prompt_tokens': 1000, 'completion_tokens': 200, 'prompt_cache_hit_tokens': 500})
    row = ledger.rows()[0]
    assert abs(float(row['estimate']) - .0023) < 1e-12
    assert row['currency'] == 'USD'
    ledger.put_prices({'test': {**prices['test'], 'input': '1000'}})
    assert ledger.rows()[0]['price_snapshot'] == row['price_snapshot']
    ledger.record({'model': 'missing', 'provider': 'a'}, {'prompt_tokens': 1})
    assert ledger.rows()[-1]['estimate'] is None


def test_filtered_reports_export_and_charts(ledger):
    for user, group in [('1', '100'), ('2', '100'), ('1', '200')]:
        ledger.record(MODEL, {'prompt_tokens': 10, 'completion_tokens': 2},
                      scope={'user_id': user, 'group_id': group, 'scene': 'group'})
    report = reports.report_data('周 群 100 用户 1')
    assert report['calls'] == 1 and report['total'] == 12
    assert 'QQ 1' in reports.format_report(report)
    path, count = reports.export_history('全部 用户 1')
    assert count == 2
    assert 'group_id' in path.read_text('utf-8-sig')
    assert path.stat().st_mode & 0o777 == 0o600
    assert reports.draw_report(report).startswith(b'\x89PNG')
    for period in ['月', '年', '全部']:
        assert reports.draw_report(reports.report_data(period)).startswith(b'\x89PNG')


def test_history_cursor_traverses_every_page_without_duplicates(ledger):
    for index in range(7):
        ledger.record(MODEL, {'prompt_tokens': 1}, attempt_id=str(index))
    first = ledger.rows(limit=3)
    second = ledger.rows(limit=3, after=(first[-1]['at'], first[-1]['id']))
    third = ledger.rows(limit=3, after=(second[-1]['at'], second[-1]['id']))
    assert len({r['id'] for r in first + second + third}) == 7


@pytest.mark.asyncio
async def test_classifier_persists_usage_with_personal_scope(ledger, monkeypatch):
    from nonebot_plugin_moellmchats import categorize
    payloads = []
    class Response:
        status = 200
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        async def json(self):
            return {'choices': [{'message': {'content': '{"difficulty":"0","vision_required":false,"required_plugins":[]}'}}],
                    'usage': {'prompt_tokens': 100, 'completion_tokens': 20}}
    def post(**kwargs):
        payloads.append(json.loads(kwargs['data']))
        return Response()
    monkeypatch.setattr(categorize, 'get_session', lambda: SimpleNamespace(post=post))
    category = categorize.Categorize('hi', SimpleNamespace())
    with bind_usage_scope(SimpleNamespace(self_id='77'), SimpleNamespace(get_user_id=lambda: '11', group_id=100)):
        result = await category._request_category_model_once({**MODEL, 'key': 'Bearer fake', 'url': 'https://example.invalid'}, '只返回JSON')
    assert result == ('0', False, [])
    assert payloads[0]['thinking']['type'] == 'enabled'
    row = ledger.rows()[0]
    assert row['purpose'] == 'classification' and row['user_id'] == '11' and row['group_id'] == '100'
    assert row['total'] == 120


@pytest.mark.asyncio
async def test_native_file_has_text_fallback_after_glm_exhaustion(monkeypatch):
    from unittest.mock import AsyncMock
    from nonebot_plugin_moellmchats import media_models
    root = {**MODEL, 'key': 'Bearer fake', 'url': 'https://example.invalid', 'is_vision': True, 'media_types': ['file']}
    backup = {'model': 'backup', 'provider': 'other', 'key': 'Bearer fake', 'url': 'https://example.invalid',
              'is_vision': True, 'media_types': ['image']}
    selector = SimpleNamespace(get_model=lambda role: root, get_rate_limit_fallback=lambda *a, **k: backup,
                               mark_model_exhausted=AsyncMock())
    monkeypatch.setattr(media_models, 'model_selector', selector)
    payloads = []
    async def post(model, url, *, payload):
        payloads.append(payload)
        if len(payloads) == 1:
            return 403, {'error': {'code': 'insufficient_quota'}}
        return 200, {'choices': [{'message': {'content': '已根据文件提取文字回答'}}]}
    monkeypatch.setattr(media_models, '_post', post)
    result = await media_models.complete_media('multimodal_model', prompt='文件提取文字：你好', parts=[{
        'type': 'file', 'file': {'file_url': 'https://example.org/document.pdf'}, '_text_fallback': True}])
    assert result[1] == 'backup'
    assert '_text_fallback' not in payloads[0]['messages'][1]['content'][1]
    assert payloads[1]['messages'][1]['content'] == '文件提取文字：你好'


def test_priority_model_consumed_first_without_removing_backups(tmp_path, monkeypatch, ledger):
    from test_model_pools import pool
    selector = pool(tmp_path, monkeypatch)
    selector.models['model-a (good)']['priority_consume'] = True
    assert [selector.get_model('category_model')['model'] for _ in range(5)] == ['model-a'] * 5
    assert selector.get_rate_limit_fallback(selector.get_model('category_model'), set())['model'] == 'backup'

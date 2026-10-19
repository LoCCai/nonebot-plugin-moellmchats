import json
from decimal import Decimal

import pytest

from nonebot_plugin_moellmchats.usage_pricing import applicable_price, estimate_price, expression_tiers, newapi_prices
from test_usage_ledger import ledger, MODEL
from nonebot_plugin_moellmchats import usage_reports as reports


GLM = 'tier("standard", p * 0.15 + cr * 0.03 + cc * 0 + c * 0.5)'


def catalog(**models):
    return {'success': True, 'data': {'billing_expr': models,
        'billing_mode': {name: 'tiered_expr' for name in models}}}


def test_public_expression_prices_use_real_usd_not_legacy_ratio(ledger):
    prices = newapi_prices({}, catalog(**{'glm-5.3-flash': GLM}))
    ledger.put_prices(prices)
    ledger.record(MODEL, {'prompt_tokens': 1000000, 'completion_tokens': 100000,
        'prompt_tokens_details': {'cached_tokens': 500000}})
    assert Decimal(ledger.rows()[0]['estimate']) == Decimal('.14')
    assert prices['glm-5.3-flash']['input'] == '.15' or prices['glm-5.3-flash']['input'] == '0.15'


def test_long_context_uses_full_prompt_even_when_most_tokens_are_cached(ledger):
    expression = ('len <= 32000 ? tier("short", p * 1 + cr * 0.1 + c * 2) : '
        'len <= 128000 ? tier("medium", p * 2 + cr * 0.2 + c * 3) : '
        'tier("long", p * 4 + cr * 0.4 + c * 6)')
    ledger.put_prices(newapi_prices({}, catalog(test=expression)))
    for count, expected in [(32000, '.0032'), (32001, '.0064002'), (128001, '.0512004')]:
        ledger.record({'model': 'test'}, {'prompt_tokens': count,
            'prompt_cache_hit_tokens': count})
        assert Decimal(ledger.rows()[-1]['estimate']) == Decimal(expected)


@pytest.mark.parametrize('expression', [
    '__import__("os").system("echo no")',
    'tier("standard", p * 1 + c * -2)',
    'param("enable_thinking") ? tier("a", p * 1) : tier("b", p * 2)',
    'tier("audio", p * 1 + ai * 5 + c * 2)',
    'len <= 20 ? tier("a", p * 1) : len <= 10 ? tier("b", p * 2) : tier("c", p * 3)',
])
def test_unsupported_expressions_never_execute_or_guess_prices(expression):
    with pytest.raises(ValueError):
        expression_tiers(expression)
    assert 'input' not in newapi_prices({}, catalog(test=expression))['test']


def test_local_overrides_and_manual_prices_take_precedence(ledger):
    reference = catalog(**{'glm-5.3-flash': GLM})
    local = {'ModelRatio': {'glm-5.3-flash': 1}, 'CompletionRatio': {'glm-5.3-flash': 3}}
    assert newapi_prices(local, reference)['glm-5.3-flash']['output'] == '6'
    local['billing_setting.billing_mode'] = {'glm-5.3-flash': 'tiered_expr'}
    local['billing_setting.billing_expr'] = {'glm-5.3-flash': 'tier("local", p * 9 + c * 10)'}
    assert newapi_prices(local, reference)['glm-5.3-flash']['input'] == '9'
    local['billing_setting.billing_expr']['glm-5.3-flash'] = 'unknown()'
    assert 'input' not in newapi_prices(local, reference)['glm-5.3-flash']
    ledger.put_prices({'glm-5.3-flash (bigmodel)': {'source': 'manual_admin',
        'input': '3', 'output': '4', 'cached': '1', 'currency': 'CNY'}})
    ledger.put_prices(newapi_prices({}, reference), replace_imported=True)
    assert ledger.estimate(MODEL, {'usage_known': 1, 'total': 1, 'prompt': 1, 'cache': 0, 'completion': 0})[1] == 'CNY'
    ledger.put_prices({}, replace_imported=True)
    assert list(ledger.prices) == ['glm-5.3-flash (bigmodel)']


def test_namespaced_case_aliases_do_not_guess_different_versions():
    prices = newapi_prices({}, catalog(**{'MiniMax-M3': GLM}))
    assert applicable_price(prices, {'model': 'minimax/minimax-m3'})['input'] == '0.15'
    assert applicable_price(prices, {'model': 'minimax-m2.7'}) is None


def test_invalid_quota_and_malformed_catalog_are_rejected():
    for reference in [[], {'success': False}, {'data': []}]:
        with pytest.raises(ValueError):
            newapi_prices({}, reference)
    with pytest.raises(ValueError):
        newapi_prices({'QuotaPerUnit': 0})


def test_report_supplements_missing_prices_without_changing_saved_history(ledger):
    ledger.record(MODEL, {'prompt_tokens': 1000000, 'completion_tokens': 100000})
    ledger.record({**MODEL, 'provider': 'tencent'}, {'prompt_tokens': 500000})
    before = ledger.rows()
    ledger.put_prices(newapi_prices({}, catalog(**{'glm-5.3-flash': GLM})))
    report = reports.report_data('全部')
    assert report['reference_estimated'] == 2 and report['unpriced'] == 0
    assert report['costs']['USD'] == Decimal('.275')
    assert report['model_totals'] == {'glm-5.3-flash': 1600000}
    assert report['raw_model_calls']['glm-5.3-flash'] == 2
    assert report['platforms']['glm-5.3-flash'] == {'bigmodel', 'tencent'}
    assert sum(report['model_days']['glm-5.3-flash'].values()) == 1600000
    assert '100.0%' not in reports.format_report(report)  # platform rows retain their own shares
    path, _ = reports.export_history('全部')
    assert 'reference_estimate' in path.read_text('utf-8-sig')
    assert ledger.rows() == before


def test_chart_shows_single_multi_and_empty_data(ledger):
    for stage in range(3):
        if stage:
            ledger.record({'model': f'model-{stage}', 'provider': 'test'}, {'prompt_tokens': stage * 10})
        assert reports.draw_report(reports.report_data('周')).startswith(b'\x89PNG')


def test_report_converts_each_model_and_marks_partial_prices(ledger, monkeypatch):
    monkeypatch.setattr(reports.usage_display_config, 'moellm_usd_cny_rate', Decimal('7'))
    ledger.put_prices({'usd-model': {'input': '1', 'output': '2', 'cached': '0.1', 'currency': 'USD'},
                       'cny-model': {'input': '3', 'output': '6', 'cached': '1', 'currency': 'CNY'}})
    ledger.record({'model': 'usd-model'}, {'prompt_tokens': 1000000})
    ledger.record({'model': 'usd-model'}, None)
    ledger.record({'model': 'cny-model'}, {'prompt_tokens': 1000000})
    ledger.record({'model': 'unpriced'}, {'prompt_tokens': 1000000})
    data = reports.report_data('全部')
    assert data['cost_cny'] == Decimal('10')
    assert reports.model_cost_label(data, ['usd-model']) == '¥7.0000（部分）'
    assert reports.model_cost_label(data, ['cny-model']) == '¥3.0000'
    assert reports.model_cost_label(data, ['unpriced']) == '缺价'
    rendered = reports.format_report(data)
    assert '$1.428571 USD / ¥10.0000 人民币' in rendered
    assert '40,000,000' in reports.glm_note() and '0.33' in reports.glm_note()


def test_refresh_downloads_and_reuses_snapshot_without_network_on_startup(ledger, monkeypatch):
    from nonebot_plugin_moellmchats import usage_admin as admin
    monkeypatch.setattr(admin.usage_config, 'moellm_newapi_prices_file', '')
    monkeypatch.setattr(admin.usage_config, 'moellm_newapi_prices_url', 'https://example.invalid/prices')
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, size):
            return json.dumps(catalog(**{'glm-5.3-flash': GLM})).encode()
    class Client:
        def open(self, *args, **kwargs):
            return Response()
    monkeypatch.setattr(admin, 'build_opener', lambda *args: Client())
    assert '已载入 1 个' in admin.refresh_prices()
    assert (ledger.path.parent / 'newapi-reference.json').stat().st_mode & 0o777 == 0o600
    def no_network(*args):
        raise AssertionError('startup must not fetch prices')
    monkeypatch.setattr(admin, 'build_opener', no_network)
    assert '已载入 1 个' in admin.refresh_prices(fetch=False)
    class Offline:
        def open(self, *args, **kwargs):
            raise OSError('offline')
    monkeypatch.setattr(admin, 'build_opener', lambda *args: Offline())
    assert '拉取失败，使用已有快照' in admin.refresh_prices()
    monkeypatch.setattr(Response, 'read', lambda *args: b'[]')
    monkeypatch.setattr(admin, 'build_opener', lambda *args: Client())
    assert '拉取失败，使用已有快照' in admin.refresh_prices()
    assert json.loads((ledger.path.parent / 'newapi-reference.json').read_text())['success'] is True

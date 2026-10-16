"""Import NewAPI reference prices without executing downloaded expressions."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
import re
import time


def mapping(options, key):
    value = options.get(key, {})
    value = json.loads(value) if isinstance(value, str) else value
    if not isinstance(value, dict):
        raise ValueError("价格配置必须为对象")
    return value


def expression_tiers(expression):
    """A deliberately small grammar: linear token prices and len <= tiers.

    Request-parameter conditions, functions and audio/image-specific counters
    cannot be evaluated from this ledger and are left unpriced.
    """
    if not isinstance(expression, str) or len(expression) > 16000:
        raise ValueError("不支持的价格表达式")
    rest, result, previous = expression.strip(), [], -1
    leaf = r'tier\(\s*"[^"\\]{1,80}"\s*,\s*([^()]+)\)'
    for _ in range(12):
        branch = re.fullmatch(r'len\s*<=\s*(\d+)\s*\?\s*' + leaf + r'\s*:\s*(.+)', rest, re.S)
        if branch:
            upper, body, rest = int(branch[1]), branch[2], branch[3].strip()
            if upper <= previous:
                raise ValueError("无效的上下文价格阶梯")
            previous = upper
        else:
            match = re.fullmatch(leaf, rest)
            if not match:
                raise ValueError("不支持的价格表达式")
            upper, body = None, match[1]
        rates = {}
        for term in body.split('+'):
            match = re.fullmatch(r'\s*(p|c|cr|cc|cc1h)\s*\*\s*(\d+(?:\.\d+)?)\s*', term)
            if not match or match[1] in rates:
                raise ValueError("不支持的价格项")
            value = Decimal(match[2])
            if not value.is_finite() or not 0 <= value <= 10**9:
                raise ValueError("价格超出范围")
            rates[match[1]] = str(value)
        if 'p' not in rates:
            raise ValueError("缺少输入价格")
        result.append({'until': upper, 'input': rates['p'], 'output': rates.get('c', '0'),
            'cached': rates.get('cr', rates['p']),
            'cache_write': rates.get('cc', rates['p']),
            'cache_write_1h': rates.get('cc1h', rates.get('cc', rates['p']))})
        if upper is None:
            return result
    raise ValueError("价格阶梯过多")


def newapi_prices(options, reference=None):
    """Public catalog is a fallback; explicit local site prices take precedence.

    Billing expressions already use USD/M, unlike the legacy ratio * 2.
    Per-request prices and unsupported expressions suppress catalog fallbacks.
    """
    if not isinstance(options, dict) or reference is not None and not isinstance(reference, dict):
        raise ValueError('价格配置必须为对象')
    result, stamp = {}, time.time()
    metadata = {'currency': 'USD', 'per_tokens': 1_000_000, 'at': stamp,
                'group_ratio': '1', 'estimate_only': True}

    def add_expressions(expressions, modes, source):
        for model, expression in expressions.items():
            if modes.get(model, 'tiered_expr') != 'tiered_expr':
                continue
            result[model] = {**metadata, 'source': source + '_unsupported'}
            try:
                tiers = expression_tiers(expression)
            except (ValueError, InvalidOperation):
                continue
            result[model] = {**metadata, **{k: v for k, v in tiers[0].items() if k != 'until'},
                'tiers': tiers, 'expression': expression, 'source': source}

    if reference:
        if reference.get('success') is False:
            raise ValueError("价格源返回失败")
        data = reference.get('data', reference)
        if not isinstance(data, dict):
            raise ValueError("价格源格式无效")
        add_expressions(mapping(data, 'billing_expr'), mapping(data, 'billing_mode'), 'newapi_reference_catalog')

    inputs, outputs, caches = (mapping(options, key) for key in ('ModelRatio', 'CompletionRatio', 'CacheRatio'))
    writes = mapping(options, 'CreateCacheRatio')
    per_request = mapping(options, 'ModelPrice')
    modes = mapping(options, 'billing_setting.billing_mode')
    expressions = mapping(options, 'billing_setting.billing_expr')
    try:
        quota = Decimal(str(options.get('QuotaPerUnit', 500000)))
        if not quota.is_finite() or quota <= 0:
            raise ValueError('无效的NewAPI额度单位')
    except InvalidOperation as error:
        raise ValueError('无效的NewAPI额度单位') from error
    for model in set(inputs) | set(per_request) | set(modes) | set(expressions):
        # An explicit local override, even one we cannot interpret, must never
        # silently fall back to a different public price for that model.
        result[model] = {**metadata, 'source': 'local_newapi_unpriced'}
        if modes.get(model) == 'tiered_expr' or model in per_request or model not in inputs:
            continue
        try:
            incoming = Decimal(str(inputs[model])) * 1_000_000 / quota
            outgoing_ratio = outputs.get(model, 1 if model.startswith(('glm-', 'chatglm_', 'deepseek-', 'deepseek-ai/', 'qwen-')) else None)
            if outgoing_ratio is None:
                continue
            outgoing = incoming * Decimal(str(outgoing_ratio))
            cached = incoming * Decimal(str(caches.get(model, 1)))
            created = incoming * Decimal(str(writes.get(model, 1)))
            if any(not v.is_finite() or v < 0 for v in (incoming, outgoing, cached, created)):
                continue
            result[model] = {**metadata, 'input': str(incoming), 'output': str(outgoing),
                'cached': str(cached), 'cache_write': str(created), 'source': 'local_newapi_options'}
        except (InvalidOperation, TypeError, ValueError):
            continue
    add_expressions(expressions, modes, 'local_newapi_expression')
    return result


def applicable_price(prices, model):
    identity = f"{model.get('model')} ({model.get('provider')})"
    if price := prices.get(identity) or prices.get(model.get('model')):
        return price
    # Case and vendor namespaces vary between providers. Only accept a unique
    # equivalent catalog key; never guess a different model/version.
    name = str(model.get('model', '')).casefold()
    exact = [value for key, value in prices.items() if key.casefold() == name]
    if len(exact) == 1:
        return exact[0]
    if '/' in name:
        leaf = name.rsplit('/', 1)[-1]
        matches = [value for key, value in prices.items() if key.casefold() == leaf]
        if len(matches) == 1:
            return matches[0]
    return None


def estimate_price(price, counters):
    if not price or not counters['usage_known']:
        return None, None, {}
    try:
        if counters['total'] and not (counters['prompt'] or counters['completion']):
            return None, None, price
        rate = price
        if price.get('tiers'):
            rate = next(row for row in price['tiers']
                        if row['until'] is None or counters['prompt'] <= row['until'])
        raw = counters.get('raw_usage', {})
        raw = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(raw, dict):
            return None, None, price
        created = raw.get('cache_creation_input_tokens', 0)
        details = raw.get('cache_creation') or {}
        if not isinstance(details, dict):
            return None, None, price
        one_hour = details.get('ephemeral_1h_input_tokens', 0)
        if any(type(n) is not int or n < 0 for n in (created, one_hour)):
            return None, None, price
        one_hour = min(created, one_hour)
        if created + counters['cache'] > counters['prompt']:
            return None, None, price
        cost = (Decimal(rate['input']) * (counters['prompt'] - counters['cache'] - created)
            + Decimal(rate['cached']) * counters['cache']
            + Decimal(rate['output']) * counters['completion']
            + Decimal(rate.get('cache_write', rate['input'])) * (created - one_hour)
            + Decimal(rate.get('cache_write_1h', rate.get('cache_write', rate['input']))) * one_hour) / 1_000_000
        if not cost.is_finite() or cost < 0:
            return None, None, price
        return str(cost), price['currency'], price
    except (KeyError, InvalidOperation, ValueError, TypeError, StopIteration):
        return None, None, price

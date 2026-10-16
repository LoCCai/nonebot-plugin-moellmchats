"""Administrator usage reports, complete CSV history and charts."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import csv
import io
import json
from pathlib import Path
import shlex
import time
import threading
import uuid

from .usage_ledger import get_ledger
_chart_lock = threading.Lock()

HELP = ("用法：模型用量统计 / 模型用量图 / 导出模型用量 [周|月|年|全部] "
        "[群 群号] [用户 QQ] [模型 模型名]\n"
        "周为近7天，月为近30天，年为近365天；时间统一UTC。导出默认全部。\n"
        "补充模型额度 <模型名或编号> <Token数>；刷新模型价格。仅超级管理员可用。")


def parse_filters(text, *, export=False):
    words = shlex.split(text)
    period = words.pop(0) if words and words[0] in {"周", "月", "年", "全部"} else "全部" if export else "月"
    filters = {"since": 0 if period == "全部" else time.time() - {"周": 7, "月": 30, "年": 365}[period] * 86400,
               "until": time.time()}
    while words:
        key = words.pop(0)
        if key not in {"群", "用户", "QQ", "模型"} or not words:
            raise ValueError(HELP)
        value = words.pop(0)
        if key != "模型" and (not value.isdecimal() or int(value) <= 0):
            raise ValueError("群号和QQ必须为正整数。")
        filters[{"群": "group_id", "用户": "user_id", "QQ": "user_id", "模型": "model"}[key]] = value
    return period, filters


def iter_history(ledger, filters):
    cursor = None
    while batch := ledger.rows(**filters, after=cursor, limit=5000):
        yield from batch
        cursor = batch[-1]["at"], batch[-1]["id"]


def report_data(text, *, export=False):
    period, filters = parse_filters(text, export=export)
    ledger = get_ledger()
    result = {"period": period, "filters": filters, "calls": 0, "unknown": 0,
        "prompt": 0, "completion": 0, "cache": 0, "reasoning": 0, "total": 0,
        "models": defaultdict(int), "days": defaultdict(int), "costs": defaultdict(Decimal),
        "unpriced": 0, "users": set(), "groups": set(), "unattributed": 0}
    for row in iter_history(ledger, filters):
        result["calls"] += 1
        result["unknown"] += not row["usage_known"]
        result["unpriced"] += row["estimate"] is None
        result["unattributed"] += row["scene"] == "unattributed"
        for key in ("prompt", "completion", "cache", "reasoning", "total"):
            result[key] += row[key]
        result["models"][f"{row['model']} ({row['provider']})"] += row["total"]
        stamp = datetime.fromtimestamp(row["at"], timezone.utc)
        result["days"][stamp.strftime("%Y-%m" if period in {"年", "全部"} else "%Y-%m-%d")] += row["total"]
        if row["estimate"] is not None:
            result["costs"][row["currency"]] += Decimal(row["estimate"])
        if row["user_id"]:
            result["users"].add(row["user_id"])
        if row["group_id"]:
            result["groups"].add(row["group_id"])
    return result


def format_report(data):
    scope = data["filters"]
    lines = [f"模型用量 · {data['period']} · UTC",
        "范围：" + " / ".join(f"{label} {scope[key]}" for key, label in (("group_id", "群"), ("user_id", "QQ"), ("model", "模型")) if key in scope)
        if any(key in scope for key in ("group_id", "user_id", "model")) else "范围：全部群与私聊",
        f"{data['calls']:,} 次请求 · {len(data['users'])} 位用户 · {len(data['groups'])} 个群",
        f"Token {data['total']:,}（输入 {data['prompt']:,} / 输出 {data['completion']:,}）",
        f"缓存命中 {data['cache']:,} / 输入命中率 {data['cache'] / data['prompt']:.1%}" if data["prompt"] else "缓存命中 0（暂无输入用量）",
        f"推理 {data['reasoning']:,}（已包含在输出中）"]
    lines += [f"参考估价 {amount:.6f} {currency}" for currency, amount in sorted(data["costs"].items())]
    if data["unpriced"]:
        lines.append(f"{data['unpriced']} 次缺少用量或适用价格，未计入估价；估价不等于实际扣费。")
    if data["unknown"]:
        lines.append(f"{data['unknown']} 次上游未返回用量，Token未猜算。")
    if data["unattributed"]:
        lines.append(f"{data['unattributed']} 次系统/测试调用没有个人归属，不用于个人计费。")
    for name, tokens in sorted(data["models"].items(), key=lambda pair: pair[1], reverse=True)[:12]:
        lines.append(f"• {name}：{tokens:,}")
    ledger = get_ledger()
    for name, (allowance, baseline) in ledger.budgets.items():
        left = ledger.remaining(name)
        lines.append(f"额度 {name}：剩余 {left:,} / {allowance:,}" + (" [已用尽，保留待补充]" if left == 0 else ""))
    lines.append("完整明细：导出模型用量 全部 [群 群号] [用户 QQ]")
    return "\n".join(lines)


def export_history(text):
    _, filters = parse_filters(text, export=True)
    ledger = get_ledger()
    directory = ledger.path.parent / "exports"
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / ("model-usage-" + uuid.uuid4().hex + ".csv")
    # Exports have private permissions and a bounded retention, not a bounded
    # record count. Historical pages never silently truncate to recent rows.
    for old in directory.glob("model-usage-*.csv"):
        if old.stat().st_mtime < time.time() - 86400:
            old.unlink()
    columns = ["id", "at_utc", "provider", "model", "bot_id", "scene", "group_id", "user_id",
        "request_id", "purpose", "status", "prompt", "completion", "cache", "reasoning", "total",
        "usage_known", "estimate", "currency", "price_snapshot", "raw_usage"]
    count = 0
    with path.open("x", encoding="utf-8-sig", newline="") as file:
        path.chmod(0o600)
        writer = csv.DictWriter(file, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in iter_history(ledger, filters):
            row["at_utc"] = datetime.fromtimestamp(row["at"], timezone.utc).isoformat()
            # Prevent spreadsheet formula execution from an untrusted model ID.
            for key in ("model", "provider", "purpose"):
                if row[key].startswith(("=", "+", "-", "@")):
                    row[key] = "'" + row[key]
            writer.writerow(row)
            count += 1
    return path, count


def draw_report(data):
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib import pyplot as plt
    from matplotlib.font_manager import FontProperties, findSystemFonts
    font = next((path for path in findSystemFonts() if any(x in path.lower() for x in ("notosanscjk", "sourcehansans", "wqy"))), None)
    style = {"font.family": FontProperties(fname=font).get_name() if font else "DejaVu Sans",
        "axes.unicode_minus": False, "figure.facecolor": "#f4f6fb", "axes.facecolor": "#ffffff"}
    with _chart_lock, plt.rc_context(style):
        fig, (pie, line) = plt.subplots(1, 2, figsize=(14, 6), gridspec_kw={"width_ratios": [1, 1.4]})
        title = f"模型用量 · {data['period']} · {data['total']:,} tokens · UTC"
        scope = data["filters"]
        if scope.get("group_id"):
            title += f" · 群 {scope['group_id']}"
        if scope.get("user_id"):
            title += f" · QQ {scope['user_id']}"
        fig.suptitle(title, fontsize=15, y=.97)
        models = sorted(data["models"].items(), key=lambda item: item[1], reverse=True)
        entries = models[:6] + ([("其他", sum(v for _, v in models[6:]))] if len(models) > 6 else [])
        if sum(value for _, value in entries):
            wedges, _, _ = pie.pie([v for _, v in entries], autopct="%1.1f%%", startangle=90,
                                  wedgeprops={"width": .42, "edgecolor": "white"}, pctdistance=.8)
            pie.legend(wedges, [name for name, _ in entries], loc="lower center", bbox_to_anchor=(.5, -.18), fontsize=8)
        else:
            pie.text(.5, .5, "暂无已知用量", ha="center", transform=pie.transAxes)
        pie.set_title("Token占比（按模型与平台）")
        monthly = data["period"] in {"年", "全部"}
        now = datetime.fromtimestamp(scope["until"], timezone.utc)
        start = datetime.fromtimestamp(scope["since"], timezone.utc) if scope["since"] else datetime.fromisoformat(min(data["days"], default=now.strftime("%Y-%m")) + "-01")
        keys = []
        cursor = start.replace(day=1) if monthly else start
        while cursor.date() <= now.date():
            keys.append(cursor.strftime("%Y-%m" if monthly else "%Y-%m-%d"))
            cursor = (cursor.replace(year=cursor.year + 1, month=1) if cursor.month == 12 else cursor.replace(month=cursor.month + 1)) if monthly else cursor + timedelta(days=1)
        line.plot(keys, [data["days"].get(key, 0) / 1_000_000 for key in keys], color="#5268d8", marker="o", linewidth=2)
        step = max(1, len(keys) // 7)
        line.set_xticks(range(0, len(keys), step), keys[::step], rotation=35, ha="right")
        line.set_ylabel("百万 Token")
        line.set_title("按月趋势" if monthly else "每日趋势")
        line.grid(axis="y", alpha=.2)
        costs = " / ".join(f"{value:.4f} {currency}" for currency, value in data["costs"].items()) or "暂无可用价格"
        fig.text(.5, .02, f"NewAPI参考估价：{costs} · 缺价/用量 {data['unpriced']} 次 · 估价不等于实际扣费", ha="center", fontsize=9)
        fig.tight_layout(rect=(0, .06, 1, .92))
        output = io.BytesIO()
        fig.savefig(output, format="png", dpi=140)
        plt.close(fig)
        return output.getvalue()


def newapi_prices(options):
    """NewAPI ratio=USD 2/M input; completion/cache are input multipliers.

    Only explicit, usable rates are imported. Missing built-in defaults or
    per-request unit prices are not silently interpreted as token prices.
    """
    def mapping(key):
        value = options.get(key, {})
        return json.loads(value) if isinstance(value, str) else value
    inputs, outputs, caches = mapping("ModelRatio"), mapping("CompletionRatio"), mapping("CacheRatio")
    per_request = mapping("ModelPrice")
    result = {}
    for model, ratio in inputs.items():
        try:
            incoming = Decimal(str(ratio)) * 2
            if model in per_request or not incoming.is_finite() or incoming < 0:
                continue
            # NewAPI's generic completion multiplier is 1 for these families;
            # special GPT/Claude/Gemini rules require an explicit saved ratio.
            ratio = outputs.get(model, 1 if model.startswith(('glm-', 'chatglm_', 'deepseek-', 'deepseek-ai/', 'qwen-')) else None)
            if ratio is None:
                continue
            outgoing = incoming * Decimal(str(ratio))
            cached = incoming * Decimal(str(caches.get(model, 1)))
            if min(outgoing, cached) < 0 or not outgoing.is_finite() or not cached.is_finite():
                continue
            result[model] = {"input": str(incoming), "output": str(outgoing), "cached": str(cached),
                "currency": "USD", "per_tokens": 1_000_000, "source": "local_newapi_options",
                "at": time.time(), "group_ratio": "1", "estimate_only": True}
        except (InvalidOperation, TypeError, ValueError):
            continue
    return result

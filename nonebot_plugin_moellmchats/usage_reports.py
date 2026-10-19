"""Administrator usage reports, complete CSV history and charts."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import csv
import io
import json
from pathlib import Path
import shlex
import time
import threading
import uuid

from .usage_ledger import get_ledger
from .usage_pricing import newapi_prices
from .config import usage_display_config
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
        "model_days": defaultdict(lambda: defaultdict(int)), "model_calls": defaultdict(int),
        "model_totals": defaultdict(int), "raw_model_calls": defaultdict(int), "platforms": defaultdict(set),
        "reference_estimated": 0, "unpriced": 0, "unconverted": 0, "users": set(), "groups": set(), "unattributed": 0,
        "model_cost_cny": defaultdict(Decimal), "model_priced": defaultdict(int),
        "cost_cny": Decimal(0), "usd_cny_rate": usage_display_config.moellm_usd_cny_rate}
    for row in iter_history(ledger, filters):
        result["calls"] += 1
        result["unknown"] += not row["usage_known"]
        result["unattributed"] += row["scene"] == "unattributed"
        for key in ("prompt", "completion", "cache", "reasoning", "total"):
            result[key] += row[key]
        identity = f"{row['model']} ({row['provider']})"
        result["models"][identity] += row["total"]
        result["model_calls"][identity] += 1
        result['model_totals'][row['model']] += row['total']
        result['raw_model_calls'][row['model']] += 1
        result['platforms'][row['model']].add(row['provider'])
        stamp = datetime.fromtimestamp(row["at"], timezone.utc)
        day = stamp.strftime("%Y-%m" if period in {"年", "全部"} else "%Y-%m-%d")
        result["days"][day] += row["total"]
        result["model_days"][row['model']][day] += row["total"]
        estimate, currency = row['estimate'], row['currency']
        if estimate is None:
            estimate, currency, _ = ledger.estimate(row, row)
            result['reference_estimated'] += estimate is not None
        result['unpriced'] += estimate is None
        if estimate is not None:
            result["costs"][currency] += Decimal(estimate)
            amount = Decimal(estimate)
            if currency in {"CNY", "RMB", "USD"}:
                cny = amount * result['usd_cny_rate'] if currency == 'USD' else amount
                result['cost_cny'] += cny
                result['model_cost_cny'][row['model']] += cny
                result['model_priced'][row['model']] += 1
            else:
                result['unconverted'] += 1
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
    lines.append(cost_summary(data))
    if data['reference_estimated']:
        lines.append(f"{data['reference_estimated']} 次历史缺价请求按当前参考价格补估，原账本未改动。")
    if data["unpriced"]:
        lines.append(f"{data['unpriced']} 次缺少用量或适用价格，未计入估价；估价不等于实际扣费。")
    if data['unconverted']:
        lines.append(f"{data['unconverted']} 次价格币种暂无换算率，未计入人民币合计。")
    if data["unknown"]:
        lines.append(f"{data['unknown']} 次上游未返回用量，Token未猜算。")
    if data["unattributed"]:
        lines.append(f"{data['unattributed']} 次系统/测试调用没有个人归属，不用于个人计费。")
    for name, tokens in sorted(data["models"].items(), key=lambda pair: pair[1], reverse=True)[:12]:
        share = tokens / data['total'] if data['total'] else 0
        raw_name = name.rsplit(' (', 1)[0]
        price = model_cost_label(data, [raw_name])
        lines.append(f"• {name}：{tokens:,} Token · {share:.1%} · {data['model_calls'][name]} 次 · 模型合计折算 {price}")
    if any('bigmodel' in platforms for platforms in data['platforms'].values()):
        lines.append(glm_note())
    ledger = get_ledger()
    for name, (allowance, baseline) in ledger.budgets.items():
        left = ledger.remaining(name)
        lines.append(f"额度 {name}：剩余 {left:,} / {allowance:,}" + (" [已用尽，保留待补充]" if left == 0 else ""))
    lines.append("完整明细：导出模型用量 全部 [群 群号] [用户 QQ]")
    return "\n".join(lines)


def cost_summary(data):
    cny = data['cost_cny']
    return f"折算合计：${cny / data['usd_cny_rate']:.6f} USD / ¥{cny:.4f} 人民币（站点参考换算 1 USD = ¥{data['usd_cny_rate']}）"


def model_cost_label(data, names):
    priced = sum(data['model_priced'][name] for name in names)
    if not priced:
        return '缺价'
    value = sum(data['model_cost_cny'][name] for name in names)
    partial = priced < sum(data['raw_model_calls'][name] for name in names)
    return f"¥{value:.4f}" + ('（部分）' if partial else '')


def glm_note():
    status = ("已用尽，已停用，待管理员补充新额度后启用。\n邀请赠送 20,000,000 Token，有效期一个月。"
              if usage_display_config.moellm_glm_promo_exhausted else "；当前可用额度以管理员配置为准。")
    return (f"智谱官方 glm-5.3-flash 为付费模型；历史活动赠送登记 {usage_display_config.moellm_glm_free_tokens:,} Token"
            + status +
            f"管理员登记历史支出 ¥{usage_display_config.moellm_glm_reported_spend_cny} 人民币。"
            "折算费用为参考价值，不代表实际扣款。")


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
        "usage_known", "estimate", "currency", "price_snapshot", "raw_usage",
        "reference_estimate", "reference_currency", "reference_price_snapshot"]
    count = 0
    with path.open("x", encoding="utf-8-sig", newline="") as file:
        path.chmod(0o600)
        writer = csv.DictWriter(file, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in iter_history(ledger, filters):
            row["at_utc"] = datetime.fromtimestamp(row["at"], timezone.utc).isoformat()
            if row['estimate'] is None:
                estimate, currency, snapshot = ledger.estimate(row, row)
                if estimate is not None:
                    row.update(reference_estimate=estimate, reference_currency=currency,
                               reference_price_snapshot=json.dumps(snapshot))
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
        models = sorted(data['model_totals'].items(), key=lambda item: item[1], reverse=True)
        shown = models[:18]
        table_height = max(1.6, .34 * (len(shown) + 1))
        fig = plt.figure(figsize=(16, 5.3 + table_height))
        grid = fig.add_gridspec(2, 2, width_ratios=[1, 1.6], height_ratios=[3.8, table_height])
        pie, line, details = fig.add_subplot(grid[0, 0]), fig.add_subplot(grid[0, 1]), fig.add_subplot(grid[1, :])
        title = f"模型用量 · {data['period']} · {data['total']:,} tokens · UTC"
        scope = data["filters"]
        if scope.get("group_id"):
            title += f" · 群 {scope['group_id']}"
        if scope.get("user_id"):
            title += f" · QQ {scope['user_id']}"
        fig.suptitle(title, fontsize=15, y=.97)
        entries = models[:6] + ([("其他", sum(v for _, v in models[6:]))] if len(models) > 6 else [])
        colors = ['#5268d8', '#21a593', '#eea54b', '#d972a6', '#8d73c6', '#5aa4ce', '#a1aabd']
        if sum(value for _, value in entries):
            wedges, _, _ = pie.pie([v for _, v in entries], autopct="%1.1f%%", startangle=90,
                                  colors=colors[:len(entries)],
                                  wedgeprops={"width": .32, "edgecolor": "white"}, pctdistance=.83)
            pie.text(0, 0, f"{len(models)} 个模型\n{data['calls']:,} 次请求", ha='center', va='center', fontsize=12)
            pie.legend(wedges, [f"{name}  {value / data['total']:.1%} · " +
                       model_cost_label(data, [key for key, _ in models[6:]] if index == 6 else [name])
                       for index, (name, value) in enumerate(entries)],
                       loc='upper center', bbox_to_anchor=(.5, -.02), fontsize=9, frameon=False, ncol=1)
        else:
            pie.text(.5, .5, "暂无已知用量", ha="center", transform=pie.transAxes)
        pie.set_title("各模型 Token 占比（同模型跨平台合计）", pad=15)
        monthly = data["period"] in {"年", "全部"}
        now = datetime.fromtimestamp(scope["until"], timezone.utc)
        start = datetime.fromtimestamp(scope["since"], timezone.utc) if scope["since"] else datetime.fromisoformat(min(data["days"], default=now.strftime("%Y-%m")) + "-01")
        keys = []
        cursor = start.replace(day=1) if monthly else start
        while cursor.date() <= now.date():
            keys.append(cursor.strftime("%Y-%m" if monthly else "%Y-%m-%d"))
            cursor = (cursor.replace(year=cursor.year + 1, month=1) if cursor.month == 12 else cursor.replace(month=cursor.month + 1)) if monthly else cursor + timedelta(days=1)
        for index, (name, _) in enumerate(entries):
            names = [key for key, _ in models[6:]] if index == 6 else [name]
            values = [sum(data['model_days'][model].get(key, 0) for model in names) / 1_000_000 for key in keys]
            line.plot(range(len(keys)), values, label=name, color=colors[index], marker='o', markersize=3, linewidth=2)
        step = max(1, len(keys) // 7)
        line.set_xticks(range(0, len(keys), step), keys[::step], rotation=35, ha="right")
        line.set_ylabel("百万 Token")
        line.set_title("各模型按月趋势" if monthly else "各模型每日趋势")
        line.grid(axis="y", alpha=.2)
        if entries:
            line.legend(fontsize=8, frameon=False, loc='upper left')
        details.axis('off')
        cells = [[name, ' / '.join(sorted(data['platforms'][name])),
                  f"{data['raw_model_calls'][name]:,}", f"{tokens:,}",
                  f"{tokens / data['total']:.1%}" if data['total'] else '—',
                  model_cost_label(data, [name])] for name, tokens in shown]
        if not cells:
            cells = [['暂无调用记录', '—', '0', '0', '—', '—']]
        table = details.table(cellText=cells, colLabels=['模型', '平台', '调用次数', 'Token', '占全部用量', '折算费用（人民币）'],
            colWidths=[.32, .21, .09, .13, .10, .15], cellLoc='left', colLoc='left', bbox=[0, 0, 1, 1])
        table.auto_set_font_size(False)
        table.set_fontsize(10)
        for (row, column), cell in table.get_celld().items():
            cell.set_edgecolor('#e8edf5')
            cell.set_facecolor('#e7ecf8' if row == 0 else '#ffffff' if row % 2 else '#f3f6fc')
            if row == 0:
                cell.set_text_props(weight='bold', color='#253454')
            elif column >= 2:
                cell.set_text_props(ha='right')
            if row > 0 and column == 0 and row <= min(6, len(shown)):
                cell.set_text_props(color=colors[row - 1])
        footnote = cost_summary(data) + f" · 按现价补估 {data['reference_estimated']} 次 · 缺价/用量 {data['unpriced']} 次"
        if data['unconverted']:
            footnote += f" · 未换算 {data['unconverted']} 次"
        if any('bigmodel' in platforms for platforms in data['platforms'].values()):
            footnote += '\n' + glm_note()
        if len(models) > len(shown):
            footnote += f"\n明细展示前{len(shown)}个模型，其余请导出完整历史；占比与趋势包含全部模型。"
        fig.text(.5, .02, footnote, ha="center", fontsize=9)
        footer_top = max(.07, .04 + (footnote.count('\n') + 1) * 12 / (fig.get_figheight() * 72))
        fig.tight_layout(rect=(0, footer_top, 1, .92), h_pad=4)
        output = io.BytesIO()
        fig.savefig(output, format="png", dpi=140)
        plt.close(fig)
        return output.getvalue()

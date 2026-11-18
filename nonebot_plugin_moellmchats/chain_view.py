"""Render the effective LLM routing chain as a themed card image.

The card is authored entirely by this plugin (no model input) and styled
only with the theme tokens published by ``render_tools.render_theme_reference``
so it follows the host's theme family and day/night mode automatically.
Quota-exhausted models are archived out of the default view; the ``全部``
mode renders them dimmed instead of hiding them.
"""

from __future__ import annotations

from collections.abc import Mapping
from html import escape
from typing import Any

from .render_tools import html_image_payload

_CHAIN_CARD_WIDTH = 960
_SHORT_STATUS = {"可参与选择": "可用", "未配置密钥": "未配钥", "额度耗尽，已退出": "耗尽"}
_POOL_LABELS = {"category": "分类池", "0": "0档池", "1": "1档池", "2": "2档池"}


def _short_status(status: str) -> str:
    return _SHORT_STATUS.get(status, status)


def _chip(model: Mapping[str, Any], *, primary: bool = False) -> str:
    classes = "chip"
    if primary:
        classes += " primary"
    if model["exhausted"]:
        classes += " dead"
    tag = "dead" if model["exhausted"] else ("warn" if model["status"] == "未配置密钥" else "")
    return (
        f'<span class="{classes}"><span class="nm">{escape(model["name"])}</span>'
        f'<span class="tag {tag}">{escape(_short_status(model["status"]))}</span></span>'
    )


def _route_row(entry: Mapping[str, Any], include_archived: bool) -> str:
    label = f'<span class="role">{escape(entry["label"])}</span>'
    if entry["kind"] == "capability":
        return (
            f'<div class="route">{label}'
            '<span class="chip soft">由能力路由按请求选择'
            '<span class="tag">固定模型仅为绑定配置</span></span></div>'
        )
    if entry["kind"] == "pool":
        pool = _POOL_LABELS.get(entry["pool"], str(entry["pool"]))
        members = "".join(_chip(member) for member in entry["models"])
        return (
            f'<div class="route">{label}'
            f'<span class="pill pool">{escape(pool)}</span><span class="tag">逐次轮转</span>'
            f"{members}</div>"
        )
    model = entry["model"]
    if model is None:
        return f'<div class="route">{label}<span class="chip soft">未配置</span></div>'
    parts = [label, _chip(model, primary=True)]
    for backup in entry["backups"]:
        parts.append('<span class="arrow">→</span>')
        parts.append(_chip(backup))
    if entry["all_backups_exhausted"]:
        parts.append('<span class="tag dead">备用均额度耗尽</span>')
    elif entry["dropped_backups"] and not include_archived:
        parts.append(f'<span class="tag">略{entry["dropped_backups"]}个耗尽备用</span>')
    return f'<div class="route">{"".join(parts)}</div>'


def _capability_cell(value: bool, on: str, off: str) -> str:
    return f'<span class="tag {"on" if value else ""}">{on if value else off}</span>'


def _model_row(entry: Mapping[str, Any]) -> str:
    row_class = ' class="dead-row"' if entry["exhausted"] else ""
    return (
        f'<tr{row_class}><td class="nm">{escape(entry["name"])}</td>'
        f"<td>{escape(_short_status(entry['status']))}</td>"
        f'<td class="think">{escape(entry["thinking"])}</td>'
        f"<td>{_capability_cell(entry['vision'], '是', '否')}</td>"
        f"<td>{_capability_cell(entry['tools'], '是', '否')}</td>"
        f"<td>{_capability_cell(entry['stream'], '开', '关')}</td></tr>"
    )


_STYLE = """
.chain{box-sizing:border-box;border:1px solid var(--border);border-radius:var(--radius);
 background:var(--surface);box-shadow:var(--shadow);padding:20px 22px;color:var(--text)}
.chain *{box-sizing:border-box}
.hd{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;flex-wrap:wrap;
 padding-bottom:12px;border-bottom:1px solid var(--border)}
.hd h1{margin:0;font-size:20px;line-height:1.4;padding:0;border:0}
.hd .sub{margin-top:2px;font-size:12px;color:var(--muted)}
.pills{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.pill{display:inline-flex;align-items:center;font-size:12px;line-height:1;padding:5px 10px;border-radius:999px;
 border:1px solid var(--border);color:var(--muted);background:var(--surface-muted);white-space:nowrap}
.pill.on{color:var(--accent);border-color:var(--accent);background:var(--surface-accent);font-weight:600}
.pill.pool{color:var(--secondary);border-color:var(--secondary)}
h2.sec{font-size:13px;margin:16px 0 8px;color:var(--muted);font-weight:600;letter-spacing:.05em;
 padding:0;border:0}
h2.sec small{font-weight:400;letter-spacing:0}
.route{display:flex;align-items:center;flex-wrap:wrap;gap:6px;padding:4px 0}
.role{flex:0 0 172px;font-size:13px;font-weight:600}
.chip{display:inline-flex;align-items:center;gap:6px;font-size:12.5px;line-height:1.3;padding:5px 10px;
 border-radius:8px;border:1px solid var(--border);background:var(--surface);color:var(--text);
 max-width:460px;overflow-wrap:anywhere}
.chip.primary{border-color:var(--accent);font-weight:600}
.chip.soft{color:var(--muted);background:var(--surface-muted)}
.chip.dead{opacity:.55;background:var(--surface-muted)}
.chip.dead .nm{text-decoration:line-through}
.tag{display:inline-flex;align-items:center;font-size:10.5px;line-height:1;padding:3px 6px;border-radius:6px;
 background:var(--surface-muted);color:var(--muted);white-space:nowrap}
.tag.on{color:var(--accent);background:var(--surface-accent);font-weight:600}
.tag.warn{color:var(--secondary)}
.tag.dead{border:1px dashed var(--border);background:transparent}
.arrow{color:var(--muted);font-size:13px}
.stats{display:flex;gap:8px;flex-wrap:wrap}
.stat{font-size:12px;padding:6px 10px;border:1px solid var(--border);border-radius:8px;
 background:var(--surface-muted);color:var(--muted)}
.stat b{color:var(--text)}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th,td{padding:7px 10px !important;text-align:left;vertical-align:top}
th{white-space:nowrap;font-weight:600}
td.nm{font-weight:600;overflow-wrap:anywhere}
td.think{color:var(--muted);min-width:180px}
tr.dead-row td{opacity:.55}
tr.dead-row td.nm{text-decoration:line-through}
.fn{margin-top:16px;padding-top:10px;border-top:1px solid var(--border);
 font-size:11.5px;color:var(--muted);line-height:1.8}
.fn .archived{color:var(--secondary)}
"""


def chain_card_html(data: Mapping[str, Any]) -> str:
    """Turn :func:`chain_data` output into a themed chain card document."""
    include_archived = bool(data["include_archived"])
    toggles = "".join(
        f'<span class="pill{" on" if value else ""}">{label} {"开" if value else "关"}</span>'
        for label, value in (("MoE", data["moe"]), ("工具", data["tools"]), ("联网", data["web_search"]))
    )
    if include_archived:
        toggles += '<span class="pill pool">含归档</span>'
    generation = f'<span class="pill">资源版本 v{data["generation"]}</span>' if data["generation"] is not None else ""
    routes = "".join(_route_row(entry, include_archived) for entry in data["routes"])
    excluded = "、".join(data["tool_image_vision_excluded_tools"]) or "无"
    rows = "".join(_model_row(entry) for entry in data["models"])
    archived_line = ""
    if data["archived"] and not include_archived:
        archived_line = (
            f'<span class="archived">已归档 {len(data["archived"])} 个额度耗尽模型（!查看LLM链路 全部 查看）。</span><br>'
        )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>当前 LLM 链路（生效配置）</title>
<style>{_STYLE}</style>
</head>
<body>
<div class="chain">
  <div class="hd">
    <div>
      <h1>当前 LLM 链路</h1>
      <div class="sub">生效配置 · 池优先于固定模型</div>
    </div>
    <div class="pills">{toggles}{generation}</div>
  </div>
  <h2 class="sec">路由</h2>
  {routes}
  <h2 class="sec">请求设置</h2>
  <div class="stats">
    <span class="stat">工具产图二次识图 <b>{"开" if data["tool_image_vision_enabled"] else "关"}</b></span>
    <span class="stat">免二次识图工具 <b>{escape(excluded)}</b></span>
    <span class="stat">分类总预算 <b>{data["classification_timeout_seconds"]}秒</b>（超时走降级路径）</span>
  </div>
  <h2 class="sec">模型参数 <small>（已合并默认/供应商/模型配置；能力为配置声明）</small></h2>
  <table>
    <tr><th>模型</th><th>状态</th><th>思考参数</th><th>识图</th><th>工具</th><th>流式</th></tr>
    {rows}
  </table>
  <div class="fn">
    {archived_line}临时限流尝试同池备用；额度耗尽标记会跨重启保留，!恢复模型额度 可清除。
    固定模型不可用时按显式备用筛选；模型配置不替代宿主的绘图授权。<br>
    !查看模型池 ／ !模型池帮助；固定角色沿用 !设置视觉模型、!设置媒体模型 等命令。
  </div>
</div>
</body>
</html>"""


async def render_chain_image(data: Mapping[str, Any]) -> bytes:
    """Render the chain card; the caller falls back to text when this raises."""
    payload, _theme = await html_image_payload(html=chain_card_html(data), width=_CHAIN_CARD_WIDTH)
    return payload


__all__ = ["chain_card_html", "render_chain_image"]

from __future__ import annotations

import pytest

from nonebot_plugin_moellmchats import chain_view
from nonebot_plugin_moellmchats.chain_view import chain_card_html, render_chain_image


def _model(name: str, *, exhausted: bool = False) -> dict:
    return {
        "name": name,
        "status": "额度耗尽，已退出" if exhausted else "可参与选择",
        "exhausted": exhausted,
        "thinking": "思考模式=disabled" if exhausted else "思考强度=high",
        "vision": not exhausted,
        "tools": True,
        "stream": True,
    }


def _data(**overrides) -> dict:
    active, dead, backup = _model("good (p)"), _model("dead <x> (p)", exhausted=True), _model("b1 (p)")
    data = {
        "moe": False,
        "tools": True,
        "web_search": False,
        "capability_routing": False,
        "generation": 5,
        "routes": [
            {"kind": "pool", "label": "文本对话", "pool": "0", "models": [dict(active)]},
            {
                "kind": "fixed",
                "label": "绘图/改图",
                "model": dict(dead),
                "backups": [dict(backup)],
                "dropped_backups": 2,
                "all_backups_exhausted": False,
            },
            {"kind": "capability", "label": "意图分类"},
            {
                "kind": "fixed",
                "label": "未配置示例",
                "model": None,
                "backups": [],
                "dropped_backups": 0,
                "all_backups_exhausted": False,
            },
        ],
        "tool_image_vision_enabled": True,
        "tool_image_vision_excluded_tools": ["draw<x>"],
        "classification_timeout_seconds": 20,
        "models": [dict(active)],
        "archived": [dict(dead)],
        "include_archived": False,
    }
    data.update(overrides)
    return data


def test_card_hides_archived_models_and_escapes_everything():
    html = chain_card_html(_data())
    assert "dead &lt;x&gt; (p)" in html  # exhausted fixed model stays visible, escaped
    assert "已归档 1 个额度耗尽模型" in html
    assert "略2个耗尽备用" in html
    assert "draw&lt;x&gt;" in html
    assert html.count("<tr") == 2  # header + active row only
    assert "--surface" in html  # themed via CSS variables only


def test_card_all_mode_renders_archived_rows_dimmed():
    dead = _model("dead <x> (p)", exhausted=True)
    html = chain_card_html(_data(include_archived=True, models=[_model("good (p)"), dead]))
    assert 'class="dead-row"' in html
    assert "含归档" in html
    assert "已归档" not in html  # no archive summary in all-mode
    assert html.count("<tr") == 3


def test_card_reports_all_backups_exhausted():
    html = chain_card_html(
        _data(
            routes=[
                {
                    "kind": "fixed",
                    "label": "绘图/改图",
                    "model": _model("good (p)"),
                    "backups": [],
                    "dropped_backups": 1,
                    "all_backups_exhausted": True,
                }
            ]
        )
    )
    assert "备用均额度耗尽" in html


@pytest.mark.asyncio
async def test_render_chain_image_delegates_to_html_payload(monkeypatch):
    captured = {}

    async def fake_payload(*, html, width):
        captured["html"], captured["width"] = html, width
        return b"png-bytes", {"theme": "default"}

    monkeypatch.setattr(chain_view, "html_image_payload", fake_payload)
    assert await render_chain_image(_data()) == b"png-bytes"
    assert "当前 LLM 链路" in captured["html"]
    assert captured["width"] == 960

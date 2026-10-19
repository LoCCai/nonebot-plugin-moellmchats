from types import SimpleNamespace

import pytest

from nonebot_plugin_moellmchats import categorize as module
from nonebot_plugin_moellmchats.categorize import Categorize


@pytest.mark.asyncio
async def test_classifier_timeout_keeps_registered_math_drawing_tools(monkeypatch):
    available = {name: {} for name in ["plot_function", "plot_implicit", "render_markdown", "render_html", "send_message"]}
    category = Categorize("已知椭圆 x^2/9+y^2/4=1，结合画图证明结论", SimpleNamespace(custom_tools=available))
    monkeypatch.setattr(module.model_selector, "get_use_tools", lambda: True)

    async def timeout():
        raise TimeoutError

    monkeypatch.setattr(category, "_get_category", timeout)
    difficulty, vision, tools = await category.get_category()
    assert difficulty == "1"
    assert vision is False
    assert set(tools) == {"plot_function", "plot_implicit", "render_markdown", "render_html"}
    assert "send_message" not in tools
    assert category.selection_source == "classification_timeout_fallback"


@pytest.mark.parametrize(("enabled", "text", "available", "expected"), [
    (False, "画图说明椭圆", ["plot_implicit"], []),
    (True, "画图说明椭圆", [], []),
    (True, "画一只猫", ["plot_function", "render_markdown"], []),
    (True, "解释椭圆的定义", ["plot_implicit"], []),
    (True, "绘制极坐标曲线", ["plot_polar"], ["plot_polar"]),
    (True, "绘制三维参数曲线", ["plot_3d_parametric"], ["plot_3d_parametric"]),
])
def test_fallback_respects_scope_registration_and_global_switch(monkeypatch, enabled, text, available, expected):
    category = Categorize(text, SimpleNamespace(custom_tools=dict.fromkeys(available)))
    monkeypatch.setattr(module.model_selector, "get_use_tools", lambda: enabled)
    assert category._fallback_category()[2] == expected

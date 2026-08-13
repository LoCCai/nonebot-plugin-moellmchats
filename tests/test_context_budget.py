from __future__ import annotations

from types import SimpleNamespace

from nonebot_plugin_moellmchats.llm_state import context_dict
from nonebot_plugin_moellmchats.moe_llm import MoeLlm


def test_group_dynamic_context_is_character_bounded(monkeypatch) -> None:
    event = SimpleNamespace(
        user_id=1,
        group_id=2,
        sender=SimpleNamespace(card="tester", nickname="tester"),
    )
    context_dict.clear()
    context_dict[2].extend(["a" * 100, "b" * 100, "current"])
    chat = MoeLlm(
        SimpleNamespace(),
        event,
        {"text": ["current"], "reply_user": None},
    )
    from nonebot_plugin_moellmchats import moe_llm

    original = moe_llm.config_parser.get_config
    monkeypatch.setattr(
        moe_llm.config_parser,
        "get_config",
        lambda key, default=None: 120
        if key == "max_history_chars"
        else 1_000
        if key == "max_history_tokens"
        else original(key, default),
    )
    chat.prompt_handler()
    assert "b" * 100 in chat.dynamic_context
    assert "a" * 100 not in chat.dynamic_context

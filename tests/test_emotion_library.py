from types import SimpleNamespace

import pytest

from nonebot_plugin_moellmchats import emotion_library
from nonebot_plugin_moellmchats.moe_llm import MoeLlm
from nonebot_plugin_moellmchats.utils import parse_emotion


def test_missing_library_is_optional(monkeypatch):
    monkeypatch.setattr(emotion_library, '_library', lambda: None)
    assert emotion_library.catalog() == {'categories': {}, 'hints': ''}
    assert emotion_library.image('happy') is None


def test_actual_session_and_bounded_local_image(tmp_path, monkeypatch):
    path = tmp_path / 'a.png'
    path.write_bytes(b'local-image')
    seen = []
    def pick(category, **kwargs):
        seen.append((category, kwargs))
        return path
    lib = SimpleNamespace(pick_category_image=pick, MAX_IMAGE_BYTES=20)
    monkeypatch.setattr(emotion_library, '_library', lambda: lib)
    bot, event = object(), object()
    assert emotion_library.image('happy', bot=bot, event=event).type == 'image'
    assert seen == [('happy', {'bot': bot, 'event': event})]
    path.write_bytes(b'x' * 21)
    assert emotion_library.image('happy') is None
    assert emotion_library.image('happy', protocol='onebot_v12') is None


def test_single_attachment_across_multiple_reply_segments(monkeypatch):
    chat = object.__new__(MoeLlm)
    chat.bot, chat.event = object(), object()
    chat._manager_emotion_names = ('happy', 'sad')
    monkeypatch.setattr(emotion_library, 'image', lambda *a, **k: 'image')
    assert chat._parse_reply_emotions('你好[happy][sad][引用]') == ('你好[引用]', ['happy'])
    assert chat._reply_emotion('happy', protocol='onebot_v11') == 'image'
    assert chat._parse_reply_emotions('再见[sad]') == ('再见', [])


def test_unavailable_attachment_is_not_retried(monkeypatch):
    chat = object.__new__(MoeLlm)
    chat.bot, chat.event = object(), object()
    chat._manager_emotion_names = ('happy',)
    monkeypatch.setattr(emotion_library, 'image', lambda *a, **k: None)
    assert chat._reply_emotion('happy', protocol='onebot_v11') is None
    assert chat._parse_reply_emotions('[happy]') == ('', [])


def test_explicit_empty_catalog_preserves_normal_brackets():
    assert parse_emotion('[happy][图片]', names=()) == ('[happy][图片]', [])


@pytest.mark.parametrize('populated', [True, False])
def test_prompt_uses_session_lexicon_without_directory_scan(monkeypatch, populated):
    from nonebot_plugin_moellmchats import moe_llm as module
    chat = object.__new__(MoeLlm)
    chat.bot, chat.event = object(), object()
    chat.temperament, chat.format_message_dict = '七七', {}
    settings = {'emotions_enabled': True, 'emotion_rate': 1, 'emotion_source': 'meme_manager'}
    monkeypatch.setattr(module.config_parser, 'get_config', lambda key, default=None: settings.get(key, default))
    monkeypatch.setattr(module, 'event_sender_name', lambda e: 'user')
    monkeypatch.setattr(module, 'event_user_id', lambda e: '123')
    monkeypatch.setattr(module, 'event_group_id', lambda e: None)
    def catalog(bot, event):
        assert (bot, event) == (chat.bot, chat.event)
        return {'categories': {'happy': '开心'} if populated else {}, 'hints': '语境：收到好消息'}
    monkeypatch.setattr(emotion_library, 'catalog', catalog)
    monkeypatch.setattr(module, 'get_emotions_names', lambda: pytest.fail('legacy directory consulted'))
    chat.prompt_handler()
    assert chat.emotion_flag is populated
    assert ('收到好消息' in chat.dynamic_context) is populated


@pytest.mark.asyncio
@pytest.mark.parametrize('markdown', [True, False])
async def test_manager_attachment_uses_existing_durable_delivery(monkeypatch, markdown):
    from unittest.mock import AsyncMock
    from nonebot.adapters.onebot.v11 import MessageSegment
    from nonebot_plugin_moellmchats import moe_llm as module, render_tools, speech_tools
    chat = object.__new__(MoeLlm)
    chat.bot, chat.event = object(), object()
    chat.emotion_flag, chat.format_message_dict = True, {}
    chat._manager_emotion_names = ('happy',)
    monkeypatch.setattr(speech_tools, 'try_voice_reply', AsyncMock(return_value=False))
    monkeypatch.setattr(module, 'current_binding', lambda: object())
    monkeypatch.setattr(module, 'onebot_protocol', lambda *a: 'onebot_v11')
    monkeypatch.setattr(chat, '_buffer_structured_reply', lambda: markdown)
    renderer = AsyncMock(return_value=(b'card-image', {}))
    monkeypatch.setattr(render_tools, 'markdown_image_payload', renderer)
    monkeypatch.setattr(emotion_library, 'image', lambda *a, **k: MessageSegment.image(b'sticker'))
    delivery = AsyncMock()
    monkeypatch.setattr(module, 'durable_reply', delivery)
    text = '$A=3$' if markdown else '收到'
    assert await chat.send_emotion_message(text + '[happy]') == text
    assert len(delivery.await_args.args[2]) == 2
    assert await chat.send_emotion_message(text + '[happy]') == text
    assert len(delivery.await_args.args[2]) == 1

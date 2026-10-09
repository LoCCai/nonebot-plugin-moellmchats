from types import SimpleNamespace

from nonebot.rule import Rule
import pytest

from nonebot_plugin_moellmchats.confirmation_input import confirmation_requested, parse_confirmation


@pytest.mark.parametrize("text", [
    "确认执行 7B1FF0", "/确认执行 7b1ff0", "！确认执行 7B1FF0", "!确认执行 7B1FF0",
    "确认执行 7B1FF0 确认执行 8F51C7", "确认执行 7B1FF0\n8F51C7", "确认执行 7B1FF0 7B1FF0",
])
def test_confirmation_accepts_prefixless_and_batch_messages(text):
    action, codes = parse_confirmation(text)
    assert action == "确认执行"
    assert codes[0] == "7B1FF0"
    assert len(codes) == len(set(codes))
    assert all(len(c) == 6 for c in codes)


@pytest.mark.parametrize("text", [
    "确认执行", "确认执行 7B1FF0 另外删群", "确认执行 7B1FF0 取消执行 8F51C7",
    "确认执行 7B1FF0Z", "确认执行 ZZFFFF", "确认执行 " + "123456 " * 11,
    "帮我确认执行 7B1FF0", "确认执行7B1FF0",
])
def test_confirmation_rejects_ambiguous_or_mixed_input(text):
    with pytest.raises(ValueError, match=r"格式|最多"):
        parse_confirmation(text)


@pytest.mark.asyncio
async def test_rule_catches_real_plaintext_without_command_trie_or_prefix():
    # QQ @ and reply segments are absent from get_plaintext, so both ways of
    # mentioning Bot use the same exact parser rather than falling into LLM.
    event = SimpleNamespace(get_plaintext=lambda: " 确认执行 F351DE ")
    assert await confirmation_requested(event)
    assert Rule(confirmation_requested).checkers
    assert not await confirmation_requested(SimpleNamespace(get_plaintext=lambda: "我说了确认执行"))
    assert parse_confirmation("取消执行 7B1FF0 8F51C7") == ("取消执行", ["7B1FF0", "8F51C7"])

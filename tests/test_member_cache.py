from __future__ import annotations

import asyncio

import pytest

from nonebot_plugin_moellmchats.member_cache import MemberNameCache


class FakeBot:
    self_id = "10000"

    def __init__(self) -> None:
        self.calls = 0

    async def get_group_member_info(self, **kwargs):
        self.calls += 1
        await asyncio.sleep(0.01)
        return {"card": "群友"}


@pytest.mark.asyncio
async def test_member_lookup_is_singleflight_and_cached() -> None:
    cache = MemberNameCache()
    bot = FakeBot()
    results = await asyncio.gather(*(cache.get(bot, 1, 2) for _ in range(20)))
    assert results == ["群友"] * 20
    assert bot.calls == 1
    assert await cache.get(bot, 1, 2) == "群友"
    assert bot.calls == 1


@pytest.mark.asyncio
async def test_member_lookup_timeout_falls_back_to_qq(monkeypatch) -> None:
    cache = MemberNameCache()

    class SlowBot(FakeBot):
        async def get_group_member_info(self, **kwargs):
            self.calls += 1
            await asyncio.sleep(1)

    from nonebot_plugin_moellmchats import member_cache

    original = member_cache.config_parser.get_config
    monkeypatch.setattr(
        member_cache.config_parser,
        "get_config",
        lambda key, default=None: 0.01
        if key == "member_lookup_timeout_seconds"
        else original(key, default),
    )
    bot = SlowBot()
    assert await asyncio.wait_for(cache.get(bot, 1, 9988), 0.2) == "9988"
    assert bot.calls == 1


@pytest.mark.asyncio
async def test_member_lookup_failure_is_not_cached() -> None:
    # 回归：查询失败的降级名（裸 QQ 号）此前会按正常结果缓存整个 TTL
    cache = MemberNameCache()

    class FlakyBot(FakeBot):
        async def get_group_member_info(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("瞬时故障")
            return {"card": "群友"}

    bot = FlakyBot()
    assert await asyncio.wait_for(cache.get(bot, 1, 9988), 0.2) == "9988"
    # 失败结果不写缓存，第二次请求重新查询并拿到真实昵称
    assert await asyncio.wait_for(cache.get(bot, 1, 9988), 0.2) == "群友"
    assert bot.calls == 2

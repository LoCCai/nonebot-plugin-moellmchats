from __future__ import annotations

import pytest

from nonebot_plugin_moellmchats.network_safety import validate_public_url


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://10.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "file:///etc/passwd",
        "http://metadata.google.internal/",
    ],
)
async def test_network_tools_reject_non_public_targets(url: str) -> None:
    with pytest.raises(ValueError, match="网络工具"):
        await validate_public_url(url)

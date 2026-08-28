from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Any
from urllib.parse import urlsplit

_BLOCKED_HOSTS = {
    "metadata.google.internal",
    "metadata.google",
    "instance-data",
}


def _is_blocked_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    return not address.is_global


async def validate_public_url(url: str, *, dns_timeout: float = 2.0) -> None:
    """Reject local, private, metadata, and non-HTTP(S) tool URLs."""
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("网络工具只允许 http 或 https URL")
    if parsed.username or parsed.password:
        raise ValueError("网络工具 URL 不允许包含用户凭据")
    hostname = (parsed.hostname or "").rstrip(".").lower()
    if not hostname:
        raise ValueError("网络工具 URL 缺少主机名")
    if hostname in _BLOCKED_HOSTS or hostname.endswith(".internal"):
        raise ValueError("网络工具拒绝访问元数据或内部主机")

    try:
        if _is_blocked_address(hostname):
            raise ValueError("网络工具拒绝访问私网、环回或保留地址")
        return
    except ValueError as error:
        # ip_address also raises ValueError for regular DNS names.
        if "拒绝" in str(error):
            raise

    loop = asyncio.get_running_loop()
    try:
        addresses = await asyncio.wait_for(
            loop.getaddrinfo(
                hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            ),
            timeout=dns_timeout,
        )
    except asyncio.TimeoutError as error:
        # wait_for 在 3.10 上抛 asyncio.TimeoutError，与内建 TimeoutError 尚未合一
        raise ValueError("网络工具 URL DNS 解析超时") from error
    except OSError as error:
        raise ValueError("网络工具 URL 无法解析") from error
    if not addresses:
        raise ValueError("网络工具 URL 无法解析")
    for address in {item[4][0] for item in addresses}:
        if _is_blocked_address(address):
            raise ValueError("网络工具拒绝访问私网、环回或保留地址")


async def validate_url_arguments(value: Any) -> None:
    """Recursively validate strings which syntactically look like web URLs."""
    if isinstance(value, str) and value.lower().startswith(("http://", "https://")):
        await validate_public_url(value)
    elif isinstance(value, dict):
        for item in value.values():
            await validate_url_arguments(item)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            await validate_url_arguments(item)

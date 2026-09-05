from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
import ipaddress
import json
import re
import socket
import ssl
from types import MappingProxyType
from typing import Any
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

SAFE_HTTP_VERSION = 1
SAFE_HTTP_TIMEOUT_SECONDS = 15.0
SAFE_HTTP_DNS_TIMEOUT_SECONDS = 2.0
SAFE_HTTP_MAX_REDIRECTS = 5
SAFE_HTTP_MAX_RESPONSE_BYTES = 1_048_576
SAFE_HTTP_MAX_REQUEST_BYTES = 262_144
SAFE_HTTP_MAX_HEADER_BYTES = 32_768

_URL_MAX_CHARS = 4_096
_CLOSE_TIMEOUT_SECONDS = 1.0
_STATUS_LINE_MAX_BYTES = 8_192
_RESPONSE_HEADER_MAX_BYTES = 65_536
_RESPONSE_HEADER_MAX_COUNT = 200
_HEADER_NAME_RE = re.compile(rb"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
_ALLOW_TARGET_RE = re.compile(
    r"^(?:\*|(?:\*\.)?(?=.{1,253}$)"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)$"
)
_BLOCKED_HOSTS = frozenset(
    {
        "instance-data",
        "localhost",
        "metadata.google",
        "metadata.google.internal",
    }
)
_SENSITIVE_HEADERS = frozenset(
    {
        "api-key",
        "authorization",
        "clientkey",
        "cookie",
        "proxy-authorization",
        "rkey",
        "x-api-key",
        "x-auth-token",
    }
)
_FORBIDDEN_REQUEST_HEADERS = frozenset(
    {
        "connection",
        "content-length",
        "host",
        "keep-alive",
        "proxy-authenticate",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_ALLOWED_METHODS = frozenset(
    {"DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"}
)
_PUBLIC_DOCUMENT_HEADERS = MappingProxyType(
    {
        "accept": (
            "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.1"
        ),
        "user-agent": "Mozilla/5.0 (compatible; MoEllmChats-PublicDocument/1.0)",
    }
)

Resolver = Callable[[str, int], Awaitable[Sequence[tuple[Any, ...]]]]
Connector = Callable[
    [str, int, ssl.SSLContext | None, str | None, float],
    Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]],
]


class SafeHttpError(ValueError):
    """A bounded HTTP request could not satisfy its security contract."""


@dataclass(frozen=True)
class SafeHttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes
    url: str
    redirect_count: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.status, int)
            or isinstance(self.status, bool)
            or not 100 <= self.status <= 599
        ):
            raise TypeError("SafeHttpResponse.status 必须是 100 到 599 的整数")
        if not isinstance(self.body, bytes):
            raise TypeError("SafeHttpResponse.body 必须是 bytes")
        if not isinstance(self.headers, Mapping):
            raise TypeError("SafeHttpResponse.headers 必须是映射")
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))

    @property
    def status_code(self) -> int:
        return self.status

    @property
    def text(self) -> str:
        content_type = self.headers.get("content-type", "")
        match = re.search(r"(?i)(?:^|;)\s*charset=([A-Za-z0-9._-]{1,40})", content_type)
        encoding = match.group(1) if match else "utf-8"
        try:
            return self.body.decode(encoding, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.text)


@dataclass(frozen=True)
class _ValidatedTarget:
    url: str
    scheme: str
    hostname: str
    port: int
    request_target: str
    host_header: str

    @property
    def origin(self) -> tuple[str, str, int]:
        return self.scheme, self.hostname, self.port


def _remaining(deadline: float) -> float:
    value = deadline - asyncio.get_running_loop().time()
    if value <= 0:
        raise SafeHttpError("安全 HTTP 请求超过总时间预算")
    return value


def _normalize_hostname(hostname: str) -> str:
    normalized = hostname.rstrip(".").casefold()
    if not normalized:
        raise SafeHttpError("网络工具 URL 缺少主机名")
    try:
        return normalized.encode("idna").decode("ascii")
    except UnicodeError:
        raise SafeHttpError("网络工具 URL 主机名非法") from None


def _normalize_allowlist(network_allow: Sequence[str]) -> tuple[str, ...]:
    if (
        isinstance(network_allow, (str, bytes))
        or not isinstance(network_allow, Sequence)
        or not network_allow
    ):
        raise SafeHttpError("网络工具缺少有效 network allowlist")
    if not all(isinstance(item, str) for item in network_allow):
        raise SafeHttpError("网络工具 network allowlist 非法")
    normalized = tuple(item.casefold().rstrip(".") for item in network_allow)
    if (
        any(not isinstance(item, str) or not _ALLOW_TARGET_RE.fullmatch(item) for item in normalized)
        or len(set(normalized)) != len(normalized)
        or ("*" in normalized and len(normalized) != 1)
    ):
        raise SafeHttpError("网络工具 network allowlist 非法")
    return tuple(sorted(normalized))


def _host_allowed(hostname: str, network_allow: tuple[str, ...]) -> bool:
    if network_allow == ("*",):
        return True
    for target in network_allow:
        if target.startswith("*."):
            suffix = target[1:]
            if hostname.endswith(suffix) and hostname != suffix[1:]:
                return True
        elif hostname == target:
            return True
    return False


def _is_blocked_address(value: str) -> bool:
    address = ipaddress.ip_address(value.split("%", 1)[0])
    return not address.is_global


def _validate_target(url: str, network_allow: tuple[str, ...]) -> _ValidatedTarget:
    if (
        not isinstance(url, str)
        or not url
        or len(url) > _URL_MAX_CHARS
        or any(ord(character) < 32 or ord(character) == 127 for character in url)
    ):
        raise SafeHttpError("网络工具 URL 必须是有限长度且无控制字符的字符串")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise SafeHttpError("网络工具 URL 端口或结构非法") from None
    scheme = parsed.scheme.casefold()
    if scheme not in {"http", "https"}:
        raise SafeHttpError("网络工具只允许 http 或 https URL")
    if parsed.username is not None or parsed.password is not None:
        raise SafeHttpError("网络工具 URL 不允许包含用户凭据")
    if parsed.fragment:
        raise SafeHttpError("网络工具 URL 不允许包含 fragment")
    hostname = _normalize_hostname(parsed.hostname or "")
    if (
        hostname in _BLOCKED_HOSTS
        or hostname.endswith(".internal")
        or hostname.endswith(".localhost")
        or hostname.endswith(".local")
    ):
        raise SafeHttpError("网络工具拒绝访问元数据或内部主机")
    if not _host_allowed(hostname, network_allow):
        raise SafeHttpError("网络工具目标不在 network allowlist")
    try:
        address = ipaddress.ip_address(hostname.split("%", 1)[0])
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise SafeHttpError("网络工具拒绝访问私网、环回或保留地址")
    target_port = port or (443 if scheme == "https" else 80)
    if not 1 <= target_port <= 65_535:
        raise SafeHttpError("网络工具 URL 端口非法")
    path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = quote(parsed.query, safe="=&?/:@!$'()*+,;%-._~")
    request_target = urlunsplit(("", "", path, query, ""))
    host_literal = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 443 if scheme == "https" else 80
    host_header = host_literal if target_port == default_port else f"{host_literal}:{target_port}"
    canonical = urlunsplit((scheme, host_header, path, query, ""))
    return _ValidatedTarget(
        url=canonical,
        scheme=scheme,
        hostname=hostname,
        port=target_port,
        request_target=request_target,
        host_header=host_header,
    )


async def _default_resolver(hostname: str, port: int) -> Sequence[tuple[Any, ...]]:
    return await asyncio.get_running_loop().getaddrinfo(
        hostname,
        port,
        type=socket.SOCK_STREAM,
        proto=socket.IPPROTO_TCP,
    )


async def _resolve_public_addresses(
    target: _ValidatedTarget,
    *,
    resolver: Resolver,
    deadline: float,
) -> tuple[str, ...]:
    try:
        literal = ipaddress.ip_address(target.hostname.split("%", 1)[0])
    except ValueError:
        try:
            records = await asyncio.wait_for(
                resolver(target.hostname, target.port),
                timeout=min(SAFE_HTTP_DNS_TIMEOUT_SECONDS, _remaining(deadline)),
            )
        except asyncio.TimeoutError:
            raise SafeHttpError("网络工具 URL DNS 解析超时") from None
        except OSError:
            raise SafeHttpError("网络工具 URL 无法解析") from None
        addresses: set[str] = set()
        for record in records:
            try:
                raw = record[4][0]
                address = ipaddress.ip_address(str(raw).split("%", 1)[0])
            except (IndexError, TypeError, ValueError):
                raise SafeHttpError("网络工具 DNS 返回非法地址") from None
            addresses.add(address.compressed)
    else:
        addresses = {literal.compressed}
    if not addresses:
        raise SafeHttpError("网络工具 URL 无法解析")
    if any(_is_blocked_address(address) for address in addresses):
        raise SafeHttpError("网络工具拒绝访问私网、环回或保留地址")
    return tuple(sorted(addresses, key=lambda item: (":" in item, item)))


async def validate_public_url(url: str, *, dns_timeout: float = 2.0) -> None:
    """Compatibility validator; execution must still use ``safe_request``."""

    if not isinstance(dns_timeout, (int, float)) or isinstance(dns_timeout, bool) or dns_timeout <= 0:
        raise ValueError("dns_timeout 必须是正数")
    target = _validate_target(url, ("*",))
    deadline = asyncio.get_running_loop().time() + min(float(dns_timeout), SAFE_HTTP_TIMEOUT_SECONDS)
    try:
        await _resolve_public_addresses(
            target,
            resolver=_default_resolver,
            deadline=deadline,
        )
    except SafeHttpError as error:
        raise ValueError(str(error)) from None


async def validate_url_arguments(value: Any) -> None:
    """Recursively validate strings which syntactically look like web URLs."""

    if isinstance(value, str) and value.casefold().startswith(("http://", "https://")):
        await validate_public_url(value)
    elif isinstance(value, dict):
        for item in value.values():
            await validate_url_arguments(item)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            await validate_url_arguments(item)


def _normalize_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    if headers is None:
        return {}
    if not isinstance(headers, Mapping) or len(headers) > 64:
        raise SafeHttpError("安全 HTTP headers 必须是至多 64 项的映射")
    normalized: dict[str, str] = {}
    total = 0
    for raw_name, raw_value in headers.items():
        if not isinstance(raw_name, str) or not isinstance(raw_value, str):
            raise SafeHttpError("安全 HTTP header 名称和值必须是字符串")
        try:
            name_bytes = raw_name.encode("ascii")
            value_bytes = raw_value.encode("latin-1")
        except UnicodeError:
            raise SafeHttpError("安全 HTTP header 必须可编码为 HTTP/1.1") from None
        name = raw_name.casefold()
        if (
            not _HEADER_NAME_RE.fullmatch(name_bytes)
            or name in _FORBIDDEN_REQUEST_HEADERS
            or any(byte < 32 or byte == 127 for byte in value_bytes)
        ):
            raise SafeHttpError("安全 HTTP header 非法或由门面保留")
        total += len(name_bytes) + len(value_bytes) + 4
        if total > SAFE_HTTP_MAX_HEADER_BYTES:
            raise SafeHttpError("安全 HTTP headers 超过大小上限")
        normalized[name] = raw_value
    return normalized


def _normalize_body(body: bytes | str | None) -> bytes:
    if body is None:
        return b""
    if isinstance(body, str):
        encoded = body.encode("utf-8")
    elif isinstance(body, bytes):
        encoded = body
    else:
        raise SafeHttpError("安全 HTTP body 只能是 bytes、str 或 None")
    if len(encoded) > SAFE_HTTP_MAX_REQUEST_BYTES:
        raise SafeHttpError("安全 HTTP request body 超过大小上限")
    return encoded


async def _default_connector(
    address: str,
    port: int,
    ssl_context: ssl.SSLContext | None,
    server_hostname: str | None,
    timeout: float,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    kwargs: dict[str, Any] = {}
    if ssl_context is not None:
        kwargs.update(ssl=ssl_context, server_hostname=server_hostname)
    return await asyncio.wait_for(
        asyncio.open_connection(address, port, **kwargs),
        timeout=timeout,
    )


async def _readline(
    reader: asyncio.StreamReader,
    *,
    limit: int,
    deadline: float,
) -> bytes:
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=_remaining(deadline))
    except asyncio.TimeoutError:
        raise SafeHttpError("安全 HTTP 响应读取超时") from None
    if not line or len(line) > limit or not line.endswith(b"\n"):
        raise SafeHttpError("安全 HTTP 响应行非法或过长")
    return line


async def _read_headers(
    reader: asyncio.StreamReader,
    *,
    deadline: float,
) -> dict[str, str]:
    headers: dict[str, str] = {}
    total = 0
    count = 0
    while True:
        line = await _readline(reader, limit=_STATUS_LINE_MAX_BYTES, deadline=deadline)
        total += len(line)
        if total > _RESPONSE_HEADER_MAX_BYTES:
            raise SafeHttpError("安全 HTTP 响应 headers 超过大小上限")
        if line in {b"\n", b"\r\n"}:
            return headers
        if line[:1] in {b" ", b"\t"} or b":" not in line:
            raise SafeHttpError("安全 HTTP 响应 header 非法")
        raw_name, raw_value = line.rstrip(b"\r\n").split(b":", 1)
        if not _HEADER_NAME_RE.fullmatch(raw_name):
            raise SafeHttpError("安全 HTTP 响应 header 名称非法")
        if any((byte < 32 and byte != 9) or byte == 127 for byte in raw_value):
            raise SafeHttpError("安全 HTTP 响应 header 值包含控制字符")
        try:
            value = raw_value.strip().decode("latin-1")
        except UnicodeError:
            raise SafeHttpError("安全 HTTP 响应 header 编码非法") from None
        name = raw_name.decode("ascii").casefold()
        count += 1
        if count > _RESPONSE_HEADER_MAX_COUNT:
            raise SafeHttpError("安全 HTTP 响应 header 数量超限")
        headers[name] = f"{headers[name]}, {value}" if name in headers else value


async def _read_exactly(
    reader: asyncio.StreamReader,
    count: int,
    *,
    deadline: float,
) -> bytes:
    try:
        return await asyncio.wait_for(reader.readexactly(count), timeout=_remaining(deadline))
    except asyncio.IncompleteReadError:
        raise SafeHttpError("安全 HTTP 响应正文提前结束") from None
    except asyncio.TimeoutError:
        raise SafeHttpError("安全 HTTP 响应读取超时") from None


async def _read_chunked_body(
    reader: asyncio.StreamReader,
    *,
    deadline: float,
) -> bytes:
    body = bytearray()
    trailer_bytes = 0
    while True:
        line = await _readline(reader, limit=256, deadline=deadline)
        token = line.rstrip(b"\r\n").split(b";", 1)[0].strip()
        # 严格十六进制：int(token,16) 会接受 "+5"/" 5"/"1_0" 等非规范形式，
        # 与其余分帧解析的严格策略不一致
        if not token or any(
            character not in b"0123456789abcdefABCDEF" for character in token
        ):
            raise SafeHttpError("安全 HTTP chunk size 非法")
        try:
            size = int(token, 16)
        except ValueError:
            raise SafeHttpError("安全 HTTP chunk size 非法") from None
        if size < 0 or len(body) + size > SAFE_HTTP_MAX_RESPONSE_BYTES:
            raise SafeHttpError("安全 HTTP 响应正文超过大小上限")
        if size == 0:
            while True:
                trailer = await _readline(reader, limit=_STATUS_LINE_MAX_BYTES, deadline=deadline)
                trailer_bytes += len(trailer)
                if trailer_bytes > _RESPONSE_HEADER_MAX_BYTES:
                    raise SafeHttpError("安全 HTTP trailer 超过大小上限")
                if trailer in {b"\n", b"\r\n"}:
                    return bytes(body)
        body.extend(await _read_exactly(reader, size, deadline=deadline))
        if await _read_exactly(reader, 2, deadline=deadline) != b"\r\n":
            raise SafeHttpError("安全 HTTP chunk framing 非法")


async def _read_body(
    reader: asyncio.StreamReader,
    *,
    method: str,
    status: int,
    headers: Mapping[str, str],
    deadline: float,
) -> bytes:
    if method == "HEAD" or status in {204, 304} or 100 <= status < 200:
        return b""
    content_encoding = headers.get("content-encoding", "identity").strip().casefold()
    if content_encoding not in {"", "identity"}:
        raise SafeHttpError("安全 HTTP 不接受压缩响应")
    transfer = headers.get("transfer-encoding", "").strip().casefold()
    if transfer:
        if transfer != "chunked":
            raise SafeHttpError("安全 HTTP 不支持该 Transfer-Encoding")
        return await _read_chunked_body(reader, deadline=deadline)
    length_value = headers.get("content-length")
    if length_value is not None:
        values = {item.strip() for item in length_value.split(",")}
        if len(values) != 1:
            raise SafeHttpError("安全 HTTP Content-Length 冲突")
        try:
            length = int(values.pop(), 10)
        except ValueError:
            raise SafeHttpError("安全 HTTP Content-Length 非法") from None
        if not 0 <= length <= SAFE_HTTP_MAX_RESPONSE_BYTES:
            raise SafeHttpError("安全 HTTP 响应正文超过大小上限")
        return await _read_exactly(reader, length, deadline=deadline)
    body = bytearray()
    while True:
        try:
            chunk = await asyncio.wait_for(
                reader.read(min(65_536, SAFE_HTTP_MAX_RESPONSE_BYTES + 1 - len(body))),
                timeout=_remaining(deadline),
            )
        except asyncio.TimeoutError:
            raise SafeHttpError("安全 HTTP 响应读取超时") from None
        if not chunk:
            return bytes(body)
        body.extend(chunk)
        if len(body) > SAFE_HTTP_MAX_RESPONSE_BYTES:
            raise SafeHttpError("安全 HTTP 响应正文超过大小上限")


async def _request_once(
    target: _ValidatedTarget,
    *,
    method: str,
    headers: Mapping[str, str],
    body: bytes,
    resolver: Resolver,
    connector: Connector,
    deadline: float,
) -> tuple[int, dict[str, str], bytes]:
    addresses = await _resolve_public_addresses(
        target,
        resolver=resolver,
        deadline=deadline,
    )
    ssl_context = ssl.create_default_context() if target.scheme == "https" else None
    reader: asyncio.StreamReader | None = None
    writer: asyncio.StreamWriter | None = None
    for address in addresses:
        try:
            reader, writer = await connector(
                address,
                target.port,
                ssl_context,
                target.hostname if ssl_context is not None else None,
                _remaining(deadline),
            )
            break
        except (OSError, asyncio.TimeoutError, ssl.SSLError):
            continue
    if reader is None or writer is None:
        raise SafeHttpError("安全 HTTP 无法连接已验证的公网地址")
    request_headers = {
        "accept-encoding": "identity",
        "connection": "close",
        "host": target.host_header,
        **headers,
    }
    if body:
        request_headers["content-length"] = str(len(body))
    payload = bytearray(f"{method} {target.request_target} HTTP/1.1\r\n".encode("ascii"))
    for name, value in request_headers.items():
        payload.extend(name.encode("ascii"))
        payload.extend(b": ")
        payload.extend(value.encode("latin-1"))
        payload.extend(b"\r\n")
    payload.extend(b"\r\n")
    payload.extend(body)
    try:
        writer.write(bytes(payload))
        await asyncio.wait_for(writer.drain(), timeout=_remaining(deadline))
        while True:
            status_line = await _readline(
                reader,
                limit=_STATUS_LINE_MAX_BYTES,
                deadline=deadline,
            )
            match = re.fullmatch(rb"HTTP/1\.[01] ([0-9]{3})(?: [^\r\n]*)?\r?\n", status_line)
            if match is None:
                raise SafeHttpError("安全 HTTP 状态行非法")
            status = int(match.group(1))
            if not 100 <= status <= 599:
                raise SafeHttpError("安全 HTTP 状态码非法")
            response_headers = await _read_headers(reader, deadline=deadline)
            if status not in {100, 102, 103}:
                break
        response_body = await _read_body(
            reader,
            method=method,
            status=status,
            headers=response_headers,
            deadline=deadline,
        )
        return status, response_headers, response_body
    finally:
        writer.close()
        try:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining > 0:
                await asyncio.wait_for(
                    writer.wait_closed(),
                    timeout=min(_CLOSE_TIMEOUT_SECONDS, remaining),
                )
        except (AttributeError, OSError, asyncio.TimeoutError, ssl.SSLError):
            pass


async def safe_request(
    url: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    body: bytes | str | None = None,
    _network_allow: Sequence[str] = (),
    _resolver: Resolver | None = None,
    _connector: Connector | None = None,
) -> SafeHttpResponse:
    """Perform one bounded, IP-pinned HTTP request with manual redirects.

    ``_network_allow`` is injected from the immutable Custom File artifact by
    the isolated worker.  Tool parameters must never supply or widen it.
    """

    network_allow = _normalize_allowlist(_network_allow)
    if not isinstance(method, str) or method.strip().upper() not in _ALLOWED_METHODS:
        raise SafeHttpError("安全 HTTP method 非法")
    current_method = method.strip().upper()
    current_headers = _normalize_headers(headers)
    current_body = _normalize_body(body)
    if current_method in {"GET", "HEAD"} and current_body:
        raise SafeHttpError("安全 HTTP GET/HEAD 不允许 request body")
    resolver = _resolver or _default_resolver
    connector = _connector or _default_connector
    if not callable(resolver) or not callable(connector):
        raise TypeError("安全 HTTP resolver/connector 必须可调用")
    deadline = asyncio.get_running_loop().time() + SAFE_HTTP_TIMEOUT_SECONDS
    current = _validate_target(url, network_allow)
    visited = {current.url}

    for redirect_count in range(SAFE_HTTP_MAX_REDIRECTS + 1):
        status, response_headers, response_body = await _request_once(
            current,
            method=current_method,
            headers=current_headers,
            body=current_body,
            resolver=resolver,
            connector=connector,
            deadline=deadline,
        )
        location = response_headers.get("location")
        if status not in _REDIRECT_STATUSES or not location:
            return SafeHttpResponse(
                status=status,
                headers=response_headers,
                body=response_body,
                url=current.url,
                redirect_count=redirect_count,
            )
        if redirect_count >= SAFE_HTTP_MAX_REDIRECTS:
            raise SafeHttpError("安全 HTTP 重定向超过 5 次")
        next_target = _validate_target(urljoin(current.url, location), network_allow)
        if current.scheme == "https" and next_target.scheme != "https":
            raise SafeHttpError("安全 HTTP 拒绝 HTTPS 降级重定向")
        if next_target.url in visited:
            raise SafeHttpError("安全 HTTP 检测到重定向循环")
        visited.add(next_target.url)
        if next_target.origin != current.origin:
            current_headers = {
                name: value
                for name, value in current_headers.items()
                if name not in _SENSITIVE_HEADERS
            }
        if status == 303 or (status in {301, 302} and current_method == "POST"):
            current_method = "GET"
            current_body = b""
            current_headers = {
                name: value
                for name, value in current_headers.items()
                if name not in {"content-type", "content-encoding"}
            }
        current = next_target

    raise SafeHttpError("安全 HTTP 重定向状态异常")


async def safe_public_get(url: str) -> SafeHttpResponse:
    """Fetch one public document through the fixed, read-only HTTP boundary.

    This facade is intended for package-owned and registered read-only tools.
    The caller can supply only the URL: method, headers and the public-host
    ceiling are fixed here, while ``safe_request`` still revalidates DNS and
    pins an approved address on every redirect hop.

    注意：该门面固定公网主机上限（等价 ``("*",)``），不会收窄到自定义文件
    工具声明的 network allowlist——因此它已加入 ast_policy 的
    ``_SAFE_HTTP_CALLS``，导入即要求 network capability，禁止静默绕过。
    """

    return await safe_request(
        url,
        method="GET",
        headers=_PUBLIC_DOCUMENT_HEADERS,
        _network_allow=("*",),
    )


__all__ = [
    "SAFE_HTTP_VERSION",
    "SafeHttpError",
    "SafeHttpResponse",
    "safe_public_get",
    "safe_request",
    "validate_public_url",
    "validate_url_arguments",
]

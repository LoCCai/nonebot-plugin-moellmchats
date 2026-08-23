from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
import json
import re
from types import MappingProxyType
from typing import Any, Protocol, TypeAlias, runtime_checkable

WEB_ADMIN_VERSION = 1
WEB_ADMIN_DEFAULT_BASE_PATH = "/admin"

_MAX_HEADER_COUNT = 128
_MAX_HEADER_BYTES = 65_536
_MAX_REQUEST_BODY_BYTES = 1_024
_MAX_REQUEST_BODY_MESSAGES = 8
_MAX_ASSET_BYTES = 32_768
_MAX_PATH_BYTES = 256
_MAX_QUERY_BYTES = 1_024
_HEADER_NAME_RE = re.compile(rb"^[a-z0-9!#$%&'*+.^_`|~-]{1,128}$")
_CONTENT_LENGTH_RE = re.compile(rb"(?:0|[1-9][0-9]{0,9})")
_CANONICAL_PREFIX_RE = re.compile(r"^(?:/[A-Za-z0-9._~-]+)*$")
_ALLOWED_CONTENT_TYPES = frozenset(
    {
        "application/javascript; charset=utf-8",
        "text/css; charset=utf-8",
        "text/html; charset=utf-8",
        "text/plain; charset=utf-8",
    }
)
_CONTENT_SECURITY_POLICY = (
    "default-src 'none'; "
    "base-uri 'none'; "
    "connect-src 'self'; "
    "font-src 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'; "
    "img-src 'none'; "
    "manifest-src 'none'; "
    "media-src 'none'; "
    "object-src 'none'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "worker-src 'none'"
)
_COMMON_SECURITY_HEADERS = (
    (b"cache-control", b"no-store, max-age=0"),
    (b"content-security-policy", _CONTENT_SECURITY_POLICY.encode("ascii")),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), geolocation=(), microphone=(), payment=(), usb=()"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
)

WebAdminMessage: TypeAlias = dict[str, Any]
WebAdminReceive: TypeAlias = Callable[[], Awaitable[WebAdminMessage]]
WebAdminSend: TypeAlias = Callable[[WebAdminMessage], Awaitable[None]]


class WebAdminError(RuntimeError):
    """Base error for the detached H-05 Web Admin boundary."""


class WebAdminConfigurationError(WebAdminError):
    """A Web Admin component was configured with an unsafe contract."""


class WebAdminProtocolError(WebAdminError):
    """The Web Admin ASGI adapter received an unsupported scope."""


def _canonical_prefix(value: object, *, label: str, allow_empty: bool) -> str:
    if not isinstance(value, str) or not _CANONICAL_PREFIX_RE.fullmatch(value):
        raise WebAdminConfigurationError(f"{label} 必须是 canonical ASCII path prefix")
    if not allow_empty and not value:
        raise WebAdminConfigurationError(f"{label} 不得为空")
    if value == "/" or value.endswith("/") or "//" in value or any(part in {".", ".."} for part in value.split("/")):
        raise WebAdminConfigurationError(f"{label} 不得为根路径或以斜杠结尾")
    if len(value.encode("ascii")) > _MAX_PATH_BYTES:
        raise WebAdminConfigurationError(f"{label} 超过安全长度")
    return value


@dataclass(frozen=True)
class WebAdminConfig:
    base_path: str = WEB_ADMIN_DEFAULT_BASE_PATH
    api_prefix: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "base_path",
            _canonical_prefix(self.base_path, label="Web Admin base_path", allow_empty=False),
        )
        object.__setattr__(
            self,
            "api_prefix",
            _canonical_prefix(self.api_prefix, label="Web Admin api_prefix", allow_empty=True),
        )


@dataclass(frozen=True, repr=False)
class WebAdminAsset:
    path: str
    content_type: str
    body: bytes

    def __post_init__(self) -> None:
        path = _canonical_prefix(self.path, label="Web Admin asset path", allow_empty=False)
        if self.content_type not in _ALLOWED_CONTENT_TYPES - {"text/plain; charset=utf-8"}:
            raise WebAdminConfigurationError("Web Admin asset content type 非法")
        if not isinstance(self.body, bytes) or not self.body or len(self.body) > _MAX_ASSET_BYTES:
            raise WebAdminConfigurationError("Web Admin asset body 非法或超限")
        if b"\x00" in self.body:
            raise WebAdminConfigurationError("Web Admin asset body 不得包含 NUL")
        try:
            self.body.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise WebAdminConfigurationError("Web Admin asset body 必须是 UTF-8") from None
        object.__setattr__(self, "path", path)

    def __repr__(self) -> str:
        return f"WebAdminAsset(path={self.path!r}, content_type={self.content_type!r}, body_bytes={len(self.body)})"


@dataclass(frozen=True, repr=False)
class WebAdminRequest:
    method: str
    path: str
    query_string: bytes = b""
    body: bytes = b""

    def __post_init__(self) -> None:
        if self.method not in {"GET", "HEAD"}:
            if not isinstance(self.method, str) or not re.fullmatch(r"[A-Z]{1,16}", self.method):
                raise WebAdminProtocolError("Web Admin method 非法")
        if not isinstance(self.path, str) or not _CANONICAL_PREFIX_RE.fullmatch(self.path):
            raise WebAdminProtocolError("Web Admin path 非法")
        if (
            not self.path
            or self.path == "/"
            or self.path.endswith("/")
            or "//" in self.path
            or any(part in {".", ".."} for part in self.path.split("/"))
        ):
            raise WebAdminProtocolError("Web Admin path 必须是 canonical path")
        if len(self.path.encode("ascii")) > _MAX_PATH_BYTES:
            raise WebAdminProtocolError("Web Admin path 超过安全长度")
        if not isinstance(self.query_string, bytes) or len(self.query_string) > _MAX_QUERY_BYTES:
            raise WebAdminProtocolError("Web Admin query string 非法或超限")
        if not isinstance(self.body, bytes) or len(self.body) > _MAX_REQUEST_BODY_BYTES:
            raise WebAdminProtocolError("Web Admin body 非法或超限")

    def __repr__(self) -> str:
        return (
            "WebAdminRequest("
            f"method={self.method!r}, path={self.path!r}, "
            f"query_bytes={len(self.query_string)}, body_bytes={len(self.body)}"
            ")"
        )


@dataclass(frozen=True)
class WebAdminResponse:
    status_code: int
    headers: tuple[tuple[bytes, bytes], ...]
    body: bytes

    def __post_init__(self) -> None:
        if self.status_code not in {200, 400, 404, 405, 413}:
            raise WebAdminConfigurationError("Web Admin response status 非法")
        if not isinstance(self.headers, tuple) or not self.headers:
            raise WebAdminConfigurationError("Web Admin response headers 必须是非空 tuple")
        if not isinstance(self.body, bytes) or len(self.body) > _MAX_ASSET_BYTES:
            raise WebAdminConfigurationError("Web Admin response body 非法或超限")
        seen: set[bytes] = set()
        values: dict[bytes, bytes] = {}
        total = 0
        for item in self.headers:
            if not isinstance(item, tuple) or len(item) != 2:
                raise WebAdminConfigurationError("Web Admin response header 必须是成对 bytes")
            name, value = item
            if not isinstance(name, bytes) or not isinstance(value, bytes) or not _HEADER_NAME_RE.fullmatch(name):
                raise WebAdminConfigurationError("Web Admin response header 非法")
            total += len(name) + len(value)
            if total > _MAX_HEADER_BYTES or name in seen:
                raise WebAdminConfigurationError("Web Admin response header 重复或超限")
            if b"\x00" in value or b"\r" in value or b"\n" in value:
                raise WebAdminConfigurationError("Web Admin response header value 非法")
            if name == b"set-cookie" or name.startswith(b"access-control-"):
                raise WebAdminConfigurationError("Web Admin response 不得写 Cookie 或开启 CORS")
            seen.add(name)
            values[name] = value
        required = {name for name, _value in _COMMON_SECURITY_HEADERS} | {b"content-type", b"content-length"}
        if not required.issubset(seen):
            raise WebAdminConfigurationError("Web Admin response 缺少安全 header")
        if not seen.issubset(required | {b"allow"}):
            raise WebAdminConfigurationError("Web Admin response 包含未允许 header")
        if any(values[name] != expected for name, expected in _COMMON_SECURITY_HEADERS):
            raise WebAdminConfigurationError("Web Admin response 安全 header 被弱化")
        if self.status_code == 405:
            if values.get(b"allow") != b"GET, HEAD":
                raise WebAdminConfigurationError("Web Admin 405 response 缺少 canonical Allow")
        elif b"allow" in values:
            raise WebAdminConfigurationError("Web Admin 仅允许 405 response 声明 Allow")
        if values[b"content-type"].decode("ascii", errors="ignore") not in _ALLOWED_CONTENT_TYPES:
            raise WebAdminConfigurationError("Web Admin response content type 非法")
        if not _CONTENT_LENGTH_RE.fullmatch(values[b"content-length"]):
            raise WebAdminConfigurationError("Web Admin response content length 非法")
        declared_length = int(values[b"content-length"])
        if declared_length > _MAX_ASSET_BYTES or (self.body and declared_length != len(self.body)):
            raise WebAdminConfigurationError("Web Admin response content length 与 body 不一致")


@runtime_checkable
class WebAdminHandler(Protocol):
    async def handle(self, request: WebAdminRequest) -> WebAdminResponse: ...


_HTML_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="referrer" content="no-referrer">
  <title>MoEllmChats Runtime Admin</title>
  <link rel="stylesheet" href="__BASE_PATH__/styles.css">
  <script src="__BASE_PATH__/app.js" defer></script>
</head>
<body>
  <header class="page-header">
    <p class="eyebrow">MoEllmChats / Runtime Platform</p>
    <h1>Runtime Admin</h1>
    <p class="subtitle">H-05 只读运行视图</p>
  </header>

  <section class="access-card" aria-labelledby="access-title">
    <div>
      <p class="section-kicker">Same-origin session</p>
      <h2 id="access-title">使用内部 API token 连接</h2>
      <p class="hint">Token 仅驻留在当前页面内存，不保存到 URL、Cookie 或 Web Storage。</p>
    </div>
    <div class="access-controls">
      <label for="token-input">Bearer token</label>
      <input
        id="token-input"
        type="password"
        inputmode="text"
        autocomplete="off"
        autocapitalize="none"
        spellcheck="false"
        minlength="32"
        maxlength="512"
      >
      <div class="button-row">
        <button id="connect-button" type="button">连接并刷新</button>
        <button id="refresh-button" type="button" class="secondary" disabled>刷新</button>
        <button id="disconnect-button" type="button" class="ghost" disabled>断开</button>
      </div>
      <p id="connection-status" class="status" role="status" aria-live="polite">尚未连接</p>
    </div>
  </section>

  <main id="dashboard" class="dashboard" hidden>
    <section class="panel" data-resource="runtime">
      <div class="panel-heading"><h2>运行代际</h2><span>runtime:read</span></div>
      <pre tabindex="0">等待刷新</pre>
    </section>
    <section class="panel" data-resource="runs">
      <div class="panel-heading"><h2>当前请求</h2><span>agent-runs:read</span></div>
      <pre tabindex="0">等待刷新</pre>
    </section>
    <section class="panel" data-resource="bundles">
      <div class="panel-heading"><h2>Tool Bundles</h2><span>tools:read</span></div>
      <pre tabindex="0">等待刷新</pre>
    </section>
    <section class="panel" data-resource="drafts">
      <div class="panel-heading"><h2>Tool Drafts</h2><span>tools:read</span></div>
      <pre tabindex="0">等待刷新</pre>
    </section>
    <section class="panel" data-resource="tools">
      <div class="panel-heading"><h2>工具与风险摘要</h2><span>tools:read</span></div>
      <pre tabindex="0">等待刷新</pre>
    </section>
    <section class="panel" data-resource="models">
      <div class="panel-heading"><h2>模型状态</h2><span>models:read</span></div>
      <pre tabindex="0">等待刷新</pre>
    </section>
    <section class="panel panel-wide" data-resource="metrics">
      <div class="panel-heading"><h2>队列、延迟与失败聚合</h2><span>metrics:read</span></div>
      <pre tabindex="0">等待刷新</pre>
    </section>
    <aside class="boundary-note panel-wide">
      <strong>数据边界</strong>
      <p>MCP 详情和 Token 明细尚未由 H-01～H-04 安全 API 导出；本页不读取配置，也不从日志或其他运行状态推断。</p>
    </aside>
  </main>

  <footer>
    <p>只读界面 · 不包含审批、激活或取消操作 · Web Admin v1</p>
  </footer>
</body>
</html>
"""

_CSS = """:root {
  color-scheme: dark;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  background: #0a0d12;
  color: #e8edf3;
  font-synthesis: none;
}

* { box-sizing: border-box; }

body {
  min-width: 320px;
  max-width: 1440px;
  margin: 0 auto;
  padding: 48px clamp(18px, 4vw, 64px) 36px;
  background:
    radial-gradient(circle at 15% 0%, rgba(59, 130, 246, 0.13), transparent 35%),
    radial-gradient(circle at 92% 18%, rgba(45, 212, 191, 0.09), transparent 30%),
    #0a0d12;
}

.page-header { margin-bottom: 30px; }
.eyebrow, .section-kicker {
  margin: 0 0 8px;
  color: #70d7c4;
  font-size: 0.76rem;
  font-weight: 760;
  letter-spacing: 0.14em;
  text-transform: uppercase;
}

h1, h2, p { margin-top: 0; }
h1 { margin-bottom: 8px; font-size: clamp(2.25rem, 6vw, 5rem); line-height: 0.98; letter-spacing: -0.05em; }
h2 { margin-bottom: 8px; font-size: 1.08rem; letter-spacing: -0.015em; }
.subtitle, .hint, footer, .boundary-note p { color: #9ba8b8; }
.subtitle { margin-bottom: 0; font-size: 1.04rem; }

.access-card, .panel, .boundary-note {
  border: 1px solid #263140;
  background: rgba(17, 23, 31, 0.92);
  box-shadow: 0 18px 55px rgba(0, 0, 0, 0.22);
}

.access-card {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(300px, 0.82fr);
  gap: 28px;
  padding: clamp(20px, 4vw, 34px);
  border-radius: 20px;
}

.access-controls { display: grid; align-content: start; gap: 10px; }
label { color: #c5d0dc; font-size: 0.82rem; font-weight: 700; }
input {
  width: 100%;
  border: 1px solid #344256;
  border-radius: 10px;
  padding: 12px 14px;
  background: #0b1017;
  color: #f4f7fb;
  outline: none;
}
input:focus { border-color: #70d7c4; box-shadow: 0 0 0 3px rgba(112, 215, 196, 0.12); }

.button-row { display: flex; flex-wrap: wrap; gap: 8px; }
button {
  border: 1px solid #70d7c4;
  border-radius: 9px;
  padding: 10px 14px;
  background: #70d7c4;
  color: #07110f;
  font: inherit;
  font-weight: 760;
  cursor: pointer;
}
button.secondary { border-color: #5272a5; background: #17243a; color: #d9e7ff; }
button.ghost { border-color: #3a4655; background: transparent; color: #c5d0dc; }
button:disabled { cursor: not-allowed; opacity: 0.42; }
button:focus-visible { outline: 3px solid rgba(112, 215, 196, 0.32); outline-offset: 2px; }

.status { min-height: 1.4em; margin: 2px 0 0; color: #9ba8b8; font-size: 0.88rem; }
.status[data-state="ok"] { color: #80e6b7; }
.status[data-state="error"] { color: #ff9c9c; }
.status[data-state="busy"] { color: #9fc2ff; }

.dashboard {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
  margin-top: 22px;
}
.dashboard[hidden] { display: none; }
.panel, .boundary-note { min-width: 0; border-radius: 15px; padding: 18px; }
.panel-wide { grid-column: 1 / -1; }
.panel-heading { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; }
.panel-heading span { color: #708098; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.72rem; }
pre {
  max-height: 360px;
  margin: 12px 0 0;
  overflow: auto;
  border-top: 1px solid #222d3b;
  padding-top: 14px;
  color: #cbd7e5;
  font: 0.78rem/1.55 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
pre[data-state="error"] { color: #ffadad; }
pre[data-state="busy"] { color: #94b8ef; }
.boundary-note { color: #d7e1ec; }
.boundary-note p { margin: 8px 0 0; line-height: 1.65; }
footer { margin-top: 24px; font-size: 0.78rem; }
footer p { margin-bottom: 0; }

@media (max-width: 800px) {
  body { padding-top: 30px; }
  .access-card, .dashboard { grid-template-columns: 1fr; }
  .panel-wide { grid-column: auto; }
}

@media (prefers-reduced-motion: no-preference) {
  button { transition: border-color 120ms ease, background-color 120ms ease, opacity 120ms ease; }
}
"""

_JAVASCRIPT_TEMPLATE = r"""(() => {
  "use strict";

  const API_PREFIX = __API_PREFIX_JSON__;
  const MAX_RESPONSE_BYTES = 65536;
  const TOKEN_PATTERN = /^[A-Za-z0-9._~-]{32,512}$/;
  const RESOURCES = Object.freeze([
    Object.freeze({ key: "runtime", path: "/runtime/status" }),
    Object.freeze({ key: "runs", path: "/agent-runs?limit=20" }),
    Object.freeze({ key: "bundles", path: "/tool-bundles?limit=20" }),
    Object.freeze({ key: "drafts", path: "/tool-drafts?limit=20" }),
    Object.freeze({ key: "tools", path: "/tools?limit=20" }),
    Object.freeze({ key: "models", path: "/models?limit=20" }),
    Object.freeze({ key: "metrics", path: "/metrics" }),
  ]);
  const STATUS_LABELS = Object.freeze({
    400: "请求被拒绝",
    401: "认证失败",
    403: "scope 不足",
    404: "资源不存在",
    405: "method 不允许",
    409: "runtime 快照已变更",
    413: "响应超限",
    503: "运行数据暂不可用",
  });

  const tokenInput = document.getElementById("token-input");
  const connectButton = document.getElementById("connect-button");
  const refreshButton = document.getElementById("refresh-button");
  const disconnectButton = document.getElementById("disconnect-button");
  const connectionStatus = document.getElementById("connection-status");
  const dashboard = document.getElementById("dashboard");
  if (!(tokenInput instanceof HTMLInputElement)
      || !(connectButton instanceof HTMLButtonElement)
      || !(refreshButton instanceof HTMLButtonElement)
      || !(disconnectButton instanceof HTMLButtonElement)
      || !(connectionStatus instanceof HTMLElement)
      || !(dashboard instanceof HTMLElement)) {
    return;
  }

  const outputs = new Map();
  for (const resource of RESOURCES) {
    const panel = document.querySelector(`[data-resource="${resource.key}"]`);
    const output = panel instanceof HTMLElement ? panel.querySelector("pre") : null;
    if (output instanceof HTMLElement) {
      outputs.set(resource.key, output);
    }
  }

  let bearerToken = null;
  let requestEpoch = 0;
  let activeController = null;

  function setConnectionStatus(message, state) {
    connectionStatus.textContent = message;
    connectionStatus.dataset.state = state;
  }

  function setOutput(key, message, state) {
    const output = outputs.get(key);
    if (!(output instanceof HTMLElement)) {
      return;
    }
    output.textContent = message;
    output.dataset.state = state;
  }

  function clearOutputs(message) {
    for (const resource of RESOURCES) {
      setOutput(resource.key, message, "idle");
    }
  }

  function validateJsonValue(value, depth, budget) {
    if (depth > 16) {
      throw new Error("response nesting too deep");
    }
    budget.count += 1;
    if (budget.count > 8192) {
      throw new Error("response node limit exceeded");
    }
    if (value === null || typeof value === "boolean") {
      return;
    }
    if (typeof value === "number") {
      if (!Number.isFinite(value) || (Number.isInteger(value) && !Number.isSafeInteger(value))) {
        throw new Error("unsafe response number");
      }
      return;
    }
    if (typeof value === "string") {
      if (value.includes("\u0000") || new TextEncoder().encode(value).byteLength > 8192) {
        throw new Error("unsafe response string");
      }
      return;
    }
    if (Array.isArray(value)) {
      if (value.length > 512) {
        throw new Error("response array limit exceeded");
      }
      for (const item of value) {
        validateJsonValue(item, depth + 1, budget);
      }
      return;
    }
    if (typeof value === "object") {
      const entries = Object.entries(value);
      if (entries.length > 512) {
        throw new Error("response object limit exceeded");
      }
      for (const [key, item] of entries) {
        if (!/^[a-z][a-z0-9_]{0,63}$/.test(key)) {
          throw new Error("unsafe response key");
        }
        validateJsonValue(item, depth + 1, budget);
      }
      return;
    }
    throw new Error("unsupported response value");
  }

  async function readBoundedBody(response) {
    const declared = response.headers.get("content-length");
    if (declared !== null && !/^(?:0|[1-9][0-9]{0,5})$/.test(declared)) {
      throw new Error("invalid response length");
    }
    if (declared !== null && Number(declared) > MAX_RESPONSE_BYTES) {
      throw new Error("response too large");
    }
    if (response.body === null) {
      return new Uint8Array();
    }
    const reader = response.body.getReader();
    const chunks = [];
    let total = 0;
    try {
      while (true) {
        const result = await reader.read();
        if (result.done) {
          break;
        }
        if (!(result.value instanceof Uint8Array)) {
          throw new Error("invalid response chunk");
        }
        total += result.value.byteLength;
        if (total > MAX_RESPONSE_BYTES) {
          await reader.cancel();
          throw new Error("response too large");
        }
        chunks.push(result.value);
      }
    } finally {
      reader.releaseLock();
    }
    const body = new Uint8Array(total);
    let offset = 0;
    for (const chunk of chunks) {
      body.set(chunk, offset);
      offset += chunk.byteLength;
    }
    return body;
  }

  async function requestJson(path, token, signal) {
    const response = await fetch(`${API_PREFIX}${path}`, {
      method: "GET",
      headers: Object.freeze({
        accept: "application/json",
        authorization: `Bearer ${token}`,
      }),
      cache: "no-store",
      credentials: "omit",
      mode: "same-origin",
      redirect: "error",
      referrerPolicy: "no-referrer",
      signal,
    });
    const body = await readBoundedBody(response);
    if (!response.ok) {
      return Object.freeze({ ok: false, status: response.status, payload: null });
    }
    const contentType = response.headers.get("content-type");
    if (contentType === null || !/^application\/json(?:; charset=utf-8)?$/.test(contentType.toLowerCase())) {
      throw new Error("invalid response content type");
    }
    const text = new TextDecoder("utf-8", { fatal: true }).decode(body);
    const payload = JSON.parse(text);
    if (payload === null || typeof payload !== "object" || Array.isArray(payload)) {
      throw new Error("invalid response payload");
    }
    validateJsonValue(payload, 0, { count: 0 });
    return Object.freeze({ ok: true, status: response.status, payload });
  }

  async function refresh() {
    if (bearerToken === null) {
      setConnectionStatus("请先输入合法 token", "error");
      return;
    }
    requestEpoch += 1;
    const epoch = requestEpoch;
    if (activeController instanceof AbortController) {
      activeController.abort();
    }
    const controller = new AbortController();
    activeController = controller;
    const token = bearerToken;
    refreshButton.disabled = true;
    setConnectionStatus("正在读取当前 runtime 快照…", "busy");

    let failures = 0;
    let expectedGeneration = null;
    for (const resource of RESOURCES) {
      if (epoch !== requestEpoch || bearerToken === null) {
        return;
      }
      setOutput(resource.key, "读取中…", "busy");
      try {
        const result = await requestJson(resource.path, token, controller.signal);
        if (epoch !== requestEpoch || bearerToken === null) {
          return;
        }
        if (!result.ok) {
          failures += 1;
          const label = STATUS_LABELS[result.status] || `HTTP ${result.status}`;
          setOutput(resource.key, label, "error");
        } else {
          if (resource.key === "runtime") {
            if (!Number.isSafeInteger(result.payload.generation) || result.payload.generation < 0) {
              throw new Error("invalid runtime generation");
            }
            expectedGeneration = result.payload.generation;
          } else if (resource.key !== "runs") {
            if (expectedGeneration === null || result.payload.generation !== expectedGeneration) {
              throw new Error("runtime generation mismatch");
            }
          }
          setOutput(resource.key, JSON.stringify(result.payload, null, 2), "ok");
        }
      } catch (error) {
        if (controller.signal.aborted || epoch !== requestEpoch) {
          return;
        }
        failures += 1;
        setOutput(resource.key, "响应校验失败", "error");
      }
    }

    if (epoch === requestEpoch && bearerToken !== null) {
      activeController = null;
      refreshButton.disabled = false;
      setConnectionStatus(
        failures === 0 ? "已从当前 runtime 快照刷新" : `已刷新，${failures} 个视图不可用`,
        failures === 0 ? "ok" : "error",
      );
    }
  }

  function disconnect() {
    requestEpoch += 1;
    if (activeController instanceof AbortController) {
      activeController.abort();
    }
    activeController = null;
    bearerToken = null;
    tokenInput.value = "";
    refreshButton.disabled = true;
    disconnectButton.disabled = true;
    dashboard.hidden = true;
    clearOutputs("等待刷新");
    setConnectionStatus("已断开，token 已从页面状态清除", "idle");
  }

  async function connect() {
    const candidate = tokenInput.value;
    tokenInput.value = "";
    if (!TOKEN_PATTERN.test(candidate)) {
      bearerToken = null;
      dashboard.hidden = true;
      refreshButton.disabled = true;
      disconnectButton.disabled = true;
      setConnectionStatus("token 必须是 32～512 字节 canonical ASCII", "error");
      return;
    }
    bearerToken = candidate;
    dashboard.hidden = false;
    disconnectButton.disabled = false;
    await refresh();
  }

  connectButton.addEventListener("click", () => { void connect(); });
  refreshButton.addEventListener("click", () => { void refresh(); });
  disconnectButton.addEventListener("click", disconnect);
  tokenInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      void connect();
    }
  });
  window.addEventListener("pagehide", disconnect);
})();
"""


def _utf8_asset(path: str, content_type: str, text: str) -> WebAdminAsset:
    if not isinstance(text, str) or "\x00" in text:
        raise WebAdminConfigurationError("Web Admin asset source 非法")
    try:
        body = text.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise WebAdminConfigurationError("Web Admin asset source 必须是 UTF-8") from None
    return WebAdminAsset(path=path, content_type=content_type, body=body)


def _build_assets(config: WebAdminConfig) -> tuple[WebAdminAsset, ...]:
    html = _HTML_TEMPLATE.replace("__BASE_PATH__", config.base_path)
    javascript = _JAVASCRIPT_TEMPLATE.replace(
        "__API_PREFIX_JSON__",
        json.dumps(config.api_prefix, ensure_ascii=True, separators=(",", ":")),
    )
    if "__BASE_PATH__" in html or "__API_PREFIX_JSON__" in javascript:
        raise WebAdminConfigurationError("Web Admin asset template 未完整渲染")
    return (
        _utf8_asset(config.base_path, "text/html; charset=utf-8", html),
        _utf8_asset(f"{config.base_path}/app.js", "application/javascript; charset=utf-8", javascript),
        _utf8_asset(f"{config.base_path}/styles.css", "text/css; charset=utf-8", _CSS),
    )


def _response_headers(*, content_type: str, content_length: int, allow: bytes | None = None) -> tuple[tuple[bytes, bytes], ...]:
    if content_type not in _ALLOWED_CONTENT_TYPES:
        raise WebAdminConfigurationError("Web Admin response content type 非法")
    if not isinstance(content_length, int) or not 0 <= content_length <= _MAX_ASSET_BYTES:
        raise WebAdminConfigurationError("Web Admin response content length 非法")
    headers = [
        *_COMMON_SECURITY_HEADERS,
        (b"content-type", content_type.encode("ascii")),
        (b"content-length", str(content_length).encode("ascii")),
    ]
    if allow is not None:
        headers.append((b"allow", allow))
    return tuple(headers)


def _plain_response(status_code: int, message: str, *, head: bool = False, allow: bytes | None = None) -> WebAdminResponse:
    encoded = (message + "\n").encode("utf-8")
    return WebAdminResponse(
        status_code=status_code,
        headers=_response_headers(
            content_type="text/plain; charset=utf-8",
            content_length=len(encoded),
            allow=allow,
        ),
        body=b"" if head else encoded,
    )


class WebAdminService:
    """Serve a secret-free, read-only shell for the authenticated H-01–H-04 APIs."""

    def __init__(self, *, config: WebAdminConfig = WebAdminConfig()) -> None:
        if not isinstance(config, WebAdminConfig):
            raise WebAdminConfigurationError("Web Admin config 非法")
        assets = _build_assets(config)
        by_path = {asset.path: asset for asset in assets}
        if len(by_path) != len(assets):
            raise WebAdminConfigurationError("Web Admin asset path 重复")
        self._config = config
        self._assets = assets
        self._assets_by_path = MappingProxyType(by_path)

    @property
    def config(self) -> WebAdminConfig:
        return self._config

    @property
    def assets(self) -> tuple[WebAdminAsset, ...]:
        return self._assets

    async def handle(self, request: WebAdminRequest) -> WebAdminResponse:
        if not isinstance(request, WebAdminRequest):
            return _plain_response(400, "invalid_request")
        head = request.method == "HEAD"
        if request.method not in {"GET", "HEAD"}:
            return _plain_response(405, "method_not_allowed", head=head, allow=b"GET, HEAD")
        if request.query_string:
            return _plain_response(400, "query_not_supported", head=head)
        if request.body:
            return _plain_response(400, "body_not_supported", head=head)
        asset = self._assets_by_path.get(request.path)
        if asset is None:
            return _plain_response(404, "not_found", head=head)
        return WebAdminResponse(
            status_code=200,
            headers=_response_headers(content_type=asset.content_type, content_length=len(asset.body)),
            body=b"" if head else asset.body,
        )


@dataclass(frozen=True)
class _RequestHeaders:
    content_length: int | None = None
    malformed: bool = False


def _request_headers(value: object) -> _RequestHeaders:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return _RequestHeaders(malformed=True)
    if len(value) > _MAX_HEADER_COUNT:
        return _RequestHeaders(malformed=True)
    total = 0
    content_lengths: list[bytes] = []
    for item in value:
        if not isinstance(item, Sequence) or isinstance(item, (str, bytes, bytearray)) or len(item) != 2:
            return _RequestHeaders(malformed=True)
        name, raw = item
        if not isinstance(name, bytes) or not isinstance(raw, bytes):
            return _RequestHeaders(malformed=True)
        total += len(name) + len(raw)
        if total > _MAX_HEADER_BYTES or not _HEADER_NAME_RE.fullmatch(name):
            return _RequestHeaders(malformed=True)
        if b"\x00" in raw or b"\r" in raw or b"\n" in raw:
            return _RequestHeaders(malformed=True)
        if name == b"content-length":
            content_lengths.append(raw)
    if len(content_lengths) > 1:
        return _RequestHeaders(malformed=True)
    if not content_lengths:
        return _RequestHeaders()
    raw_length = content_lengths[0]
    if not _CONTENT_LENGTH_RE.fullmatch(raw_length):
        return _RequestHeaders(malformed=True)
    return _RequestHeaders(content_length=int(raw_length))


async def _receive_body(receive: WebAdminReceive) -> tuple[bytes, int | None]:
    chunks: list[bytes] = []
    size = 0
    for _ in range(_MAX_REQUEST_BODY_MESSAGES):
        try:
            message = await receive()
        except Exception:
            return b"", 400
        if not isinstance(message, Mapping) or message.get("type") != "http.request":
            return b"", 400
        chunk = message.get("body", b"")
        more_body = message.get("more_body", False)
        if not isinstance(chunk, bytes) or type(more_body) is not bool:
            return b"", 400
        size += len(chunk)
        if size > _MAX_REQUEST_BODY_BYTES:
            return b"", 413
        chunks.append(chunk)
        if not more_body:
            return b"".join(chunks), None
    return b"", 413


class WebAdminASGIApp:
    """A detached ASGI adapter; callers must explicitly mount this object."""

    def __init__(self, *, service: WebAdminHandler) -> None:
        if not isinstance(service, WebAdminHandler):
            raise WebAdminConfigurationError("Web Admin ASGI service 非法")
        self._service = service

    async def __call__(
        self,
        scope: Mapping[str, Any],
        receive: WebAdminReceive,
        send: WebAdminSend,
    ) -> None:
        if not isinstance(scope, Mapping) or scope.get("type") != "http":
            raise WebAdminProtocolError("Web Admin ASGI adapter 只接受 HTTP scope")
        headers = _request_headers(scope.get("headers", ()))
        if headers.malformed:
            response = _plain_response(400, "invalid_request")
        elif headers.content_length is not None and headers.content_length > _MAX_REQUEST_BODY_BYTES:
            response = _plain_response(413, "request_too_large")
        else:
            method = scope.get("method")
            path = scope.get("path")
            query_string = scope.get("query_string", b"")
            raw_path = scope.get("raw_path")
            if not isinstance(method, str) or not isinstance(path, str) or not isinstance(query_string, bytes):
                response = _plain_response(400, "invalid_request")
            elif raw_path is not None and (
                not isinstance(raw_path, bytes)
                or len(raw_path) > _MAX_PATH_BYTES
                or b"%" in raw_path
                or not raw_path.isascii()
                or raw_path.decode("ascii") != path
            ):
                response = _plain_response(400, "invalid_request")
            else:
                body, body_error = await _receive_body(receive)
                if body_error is not None:
                    response = _plain_response(
                        body_error,
                        "request_too_large" if body_error == 413 else "invalid_request",
                    )
                elif headers.content_length is not None and headers.content_length != len(body):
                    response = _plain_response(400, "invalid_request")
                else:
                    try:
                        request = WebAdminRequest(
                            method=method,
                            path=path,
                            query_string=query_string,
                            body=body,
                        )
                    except WebAdminProtocolError:
                        response = _plain_response(400, "invalid_request")
                    else:
                        response = await self._service.handle(request)
        await send(
            {
                "type": "http.response.start",
                "status": response.status_code,
                "headers": list(response.headers),
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": response.body,
                "more_body": False,
            }
        )


__all__ = [
    "WEB_ADMIN_DEFAULT_BASE_PATH",
    "WEB_ADMIN_VERSION",
    "WebAdminASGIApp",
    "WebAdminAsset",
    "WebAdminConfig",
    "WebAdminConfigurationError",
    "WebAdminError",
    "WebAdminHandler",
    "WebAdminProtocolError",
    "WebAdminRequest",
    "WebAdminResponse",
    "WebAdminService",
]

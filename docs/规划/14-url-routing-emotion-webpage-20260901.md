---
title: 14-url-routing-emotion-webpage-20260901
date: 2026-09-01T00:00:00+00:00
lastmod: 2026-09-01T00:00:00+00:00
---

# K-12 网页路由、表情素材与安全正文提取（0.26.6）

> 当前状态：以 `feat/generated-tool-bundles` 的 0.26.5 证据提交 `8bdb202f5e60f05ecbbc0cb4248cdd5ae623c595` 为基线，0.26.6 本地实现、四版本、静态、sandbox、制品和七七定向门禁已经完成。尚未创建 0.26.6 提交、推送、Actions、安装或七七运行验收；0.26.5 的已验证恢复点仍是 `e704092a1e8d9ad215e4e9de35a9fe403483d56f`。

## 问题与根因

用户发送“`@七七 看一下 <网页 URL>，评价一下`”时，`parser_media` 的自动裸链接 Matcher 以更高优先级和 `block=True` 先接收事件。它把所有未被 Parser Lite 认领的公网 URL 都作为推测视频送去解析，即使返回普通网页、502 或无视频结果也会阻断 MoEllmChats，因而 LLM 根本看不到原消息。

表情目录的旧候选只枚举一级目录名，发送时又从目录内任意文件抽取。现场“奶龙”目录只有 `Thumbs.db`，仍被公布给模型并作为 Base64 图片交给 NapCat，最终产生 `ActionFailed retcode=1200`。正文已发送后的表情降级本身正确，但错误素材不应进入发送链路。

七七已有共享 Playwright 浏览器池和页面提取代码，但浏览器以 `--no-sandbox` 运行，部分预设关闭 Web Security、忽略证书，Context 也固定 `ignore_https_errors=True`。把模型提供的任意 URL 直接交给它会扩大 SSRF、DNS rebinding、子资源和浏览器进程风险，因此本阶段只复用其离线 DOM 提取能力。

## 依赖顺序与状态

| 节点 | 依赖 | 当前状态 | 契约 |
| --- | --- | --- | --- |
| K-12A 自动媒体传播所有权 | K-11 | 本地已实现并通过定向测试 | 明确 `to_me` 的消息绕过自动媒体；普通裸链接仅在已有 intake 或真实候选后调用 `stop_propagation()` |
| K-12B 表情候选与发送校验 | K-12A | 本地已实现并通过定向测试 | 目录至少含一张合格图片；发送前重新校验非符号链接、普通文件、扩展名、文件头、非空和 8 MiB 上限 |
| K-12C 公网只读 HTTP 门面 | K-12B | 本地已实现并通过定向测试 | `safe_public_get(url)` 固定 GET、请求头和公网 ceiling；保留逐跳 DNS/IP/重定向/预算校验 |
| K-12D 七七网页 Registered Tool | K-12C | 七七源码本地已实现并通过定向测试 | `extract_webpage` 只接收 URL；先安全获取和净化，再离线进入阻断网络的共享浏览器池，失败回退静态正文 |
| K-12E 版本、文档与完整门禁 | K-12D | 本地已完成，待提交/远端门禁 | 版本 0.26.6；Python 3.10 显式避开不兼容的 `pygtrie 2.6.0`；四版本、静态、sandbox、制品与七七定向门禁已通过 |

## 路由与传播契约

`parser_media` 的裸链接 Matcher 保持在 LLM 前进行廉价所有权探测，但改为 `block=False`。规则阶段只接收未明确投递给 Bot 的普通群消息。处理阶段的 `_submit_video(auto=True)` 返回媒体链路是否已经认领：

- 既有持久 intake、已获得真实 `resolve_token` 并进入候选/提交链路：返回 `true`，处理完后停止传播；
- 商业策略拒绝、Parser Lite 路由、解析传输失败、普通网页、无凭据或无视频结果：返回 `false`，继续传播给后续 Matcher；
- 发生异常只记录安全异常类型，不因推测探测制造用户可见失败。

这样指定网页阅读不会再被视频解析器吞掉，真正的视频裸链接仍保持异步媒体体验。

## 表情素材契约

运行快照和旧缓存共用同一筛选器。分类目录必须是一级普通目录且至少含一张合格图片；文件必须满足：

- 不是符号链接，是普通文件；
- 扩展名是 `.jpg/.jpeg/.png/.gif/.webp/.bmp`；
- 文件头与扩展名一致；
- 大小为 1 字节到 8 MiB；
- 发送时使用 `O_NOFOLLOW` 打开并再次核对，避免候选发布后的替换或损坏。

`Thumbs.db`、空目录、空文件、错误文件头、过大文件和符号链接全部忽略。v12 仍在没有协议 `file_id` 时跳过本地可选表情；正文成功后的可选表情 Adapter 失败仍只降级表情，不重发正文。

## 网页安全与浏览器池契约

`safe_public_get(url)` 是包内公开、只读、固定公网范围的门面。调用方不能选择 method、headers 或 allowlist。底层 `safe_request` 继续在每个重定向跳重新解析全部地址、拒绝私网/环回/元数据/URL 凭据、固定实际连接 IP、拒绝 HTTPS 降级与跨域敏感头，并限制总时间、五次重定向和 1 MiB 响应。

七七的 `extract_webpage` 只接受 1～2048 字 URL，且仅处理 `text/html`、`application/xhtml+xml` 和 `text/plain`。HTML 在进入浏览器前会删除脚本、样式、iframe、表单、媒体与资源标签、注释和全部属性。共享浏览器 Context 对 `**/*` 安装无条件 abort 路由，只通过 `set_content()` 接收净化后的离线 HTML；六秒内不可用则使用 BeautifulSoup 的文章/主区域正文。日志不记录 URL、响应正文或异常消息。

## 验证与交付边界

定向测试覆盖：明确 @Bot 绕过、普通网页继续传播、确认媒体后阻断；`Thumbs.db`、伪装图片、符号链接、空文件、超大文件、候选后损坏和 v12 跳过；固定公网 GET 门面；HTML 主动内容/属性清除、浏览器零网络、浏览器降级、Content-Type 拒绝与引用 URL 查询脱敏。

本地证据（2026-09-01）：

- 七七网页工具、媒体路由与 QWeb 目录定向测试 `41 passed`；新文件 Ruff 与格式检查通过；同时验证工具已进入 Registered registry；HTTP 正文成功但不满足 HTTPS 引用契约时只省略 citation，不把正文改成失败；
- 插件网络、表情、runtime reload、v11/v12 发送降级联合定向测试 `91 passed`；
- Python 3.10、3.11、3.12、3.13 串行普通全量各 `3133 passed, 1 skipped`，各版本 Ruff 全绿；
- mandatory root sandbox `41 passed, 0 skipped`，JUnit 明确 `tests=41 skipped=0`；
- CI 静态目标 Ruff/format、Pyright、文档示例、148 个本地链接、13 项运行依赖、10 项开发依赖及 244 项协议资源检查通过；
- fresh wheel/sdist、Twine 与制品内容检查通过；当前尚无 0.26.6 实现提交，最终制品摘要应在提交冻结后重新生成并绑定精确 SHA，不能把临时工作树摘要冒充安装恢复点；
- Python 3.10/3.12 × wheel/sdist 四组包外加载均发布 generation 1，v11/v12 消息门面与 `38/31/175` 协议数量正确；3.10 两种制品均解析为 `pygtrie 2.5.0`；
- 对用户提供的 `https://blog.cczo.cc/archives/163/` 做一次隔离只读探测：安全门面返回 HTTP 200、87,949 字节 HTML；共享浏览器池只接收净化后的离线文档，提取标题“观殊同诗歌书 - LoCCaiの小窝”和 765 字正文后有界关闭。没有调用真实模型或 QQ API。

本文件描述的是开发工作区，不代表七七已安装或重启，也不代表目标博客、真实模型、NapCat 或 QQ 已线上验收。本阶段不修改七七的 `pyproject.toml`、`uv.lock`、`.venv`、配置或进程，不发送真实 QQ 动作，不合并 PR，不发布 PyPI。

四版本门禁于 2026-09-01 发现 `pygtrie 2.6.0` 在 Python 3.10 导入时直接引用不存在的 `typing.Self`，NoneBot 会在插件测试前崩溃。0.26.6 仅对 Python `<3.11` 增加 `pygtrie>=2.4.1,<2.6.0` 条件约束；它是对既有传递依赖的解析修复，不新增安装包，也不修改七七依赖声明或锁文件。

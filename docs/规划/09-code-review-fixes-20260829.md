---
title: 09-code-review-fixes-20260829
date: 2026-08-29T00:00:00+00:00
lastmod: 2026-08-29T00:00:00+00:00
---

# 核心修复包：全库代码审查修复记录（2026-08-29）

> 状态：**已完成，待环境内验证**。全部分支提交位于本地分支 `fix/analysis-fixes`（基线 `1043b0a…`，即 `feat/llm-runtime-backpressure` 分支头），共 12 个提交，未推送远端、未合并。本机没有可运行 pytest 的依赖环境，验证方式见[验证状态](#验证状态)一节。分析中发现的其余问题整理在[待修复问题清单](./10-pending-issues-backlog.md)。

## 背景与方法

在 `feat/llm-runtime-backpressure` 分支上对全部 110 个 Python 源文件做了一次三路并行深度审查（核心聊天链路 / 并发背压与资源生命周期 / 工具系统与基础设施），共产出约 40 项发现。全部高严重度发现与部分中严重度发现均经人工逐条核验源码确认后，挑选 11 项「高价值 + 低风险」的修复构成核心修复包，经确认范围后实施。

范围决策：只做用户可感知的 bug 与低风险安全加固；需要重构（connector 层、Redis 通知机制、spool 诊断增量化、大文件拆分）的项全部延后，见待修复清单。

## 修复清单

### A. 已验证的高价值 Bug（8 项）

| # | 问题 | 根因 | 修复 | 提交 |
| --- | --- | --- | --- | --- |
| A-1 | 「重置我的/清空上下文」永远清不掉个人历史 | reset 命令用裸 `event.user_id`（int），写入侧统一用 `event_user_id(event)`（str）；且 `BoundedDequeStore.__getitem__` 缺省创建、从不抛 `KeyError`，`MutableMapping.__contains__` 恒为 True，掩盖键类型错位并制造垃圾条目 | reset 改用 `event_user_id(event)`；两个 store 覆写 `__contains__` 直接查内部字典 | `0358805` |
| A-2 | SSE 流式解析把非 `data:` 行喂给 `json.loads`，坏行导致整轮请求重试，分段模式下已发送内容被重复发送 | `elif line.startswith(b"")` 恒真兜底；无空格 `data:[DONE]` 不匹配旧结束标记 | 新增 `_is_sse_done`（strip 后比较，兼容无空格变体）与 `_decode_sse_payload`（只放行 `data:` 行与裸 JSON/ndjson 行，其余返回 None 跳过）；解析失败记 debug 后 continue，不再中断流 | `5f43c43` |
| A-3 | 工具总结兜底请求无异常保护，网络抖动时异常穿透 Matcher：用户无回复、`post_process`/持久化被跳过 | summary 调用位于重试循环之外且无 try | 包 try/except（CancelledError 照常传播），失败降级为 `_build_empty_tool_summary_fallback()` | `971c746` |
| A-4 | 管理员取消时 `matcher.finish` 抛 `FinishedException` 替换 `CancelledError`，破坏上层取消语义（settle 任务、请求管理器误判正常结束） | 在 `except asyncio.CancelledError` 里调用 `finish` | 释放冷却后尽力 `matcher.send` 终止提示，然后重新 `raise` 保留取消传播 | `dabfaf4` |
| A-5 | Python 3.10 上 `compat.timeout` 抛内建 `TimeoutError`，而 `moe_llm` 捕获 `asyncio.TimeoutError`（3.10 上是两个类）：超时落进 `except Exception`，无友好文案、遥测记 FAILED、重试行为不同 | 3.11 起 `asyncio.TimeoutError` 才与内建类合一 | `compat` 导出统一别名 `TimeoutError = builtins.TimeoutError`，`moe_llm` 改用别名；`network_safety` 对 `asyncio.wait_for` 的捕获改为 `asyncio.TimeoutError`（3.10 上 wait_for 抛的正是该类）。包裹 stdlib 原语的 `audit_batch`/`local_spool`/`redis_admission` 保持不动（其捕获本就正确） | `69122bb` |
| A-6 | 成员名查询一次瞬时故障即把降级名（裸 QQ 号）按正常结果缓存整个 TTL（默认 600 秒） | `_fetch` 吞掉异常返回降级名，外层无条件写缓存 | `_fetch` 失败改为向上抛出；`get()` 失败路径返回降级名但不写缓存，下次请求直接重试 | `66cb4dd` |
| A-7 | 重试提示的 `bot.send` 位于 try 块之外，`ActionFailed`/`NetworkError` 直接穿透 `get_llm_chat`，中断已执行到一半的请求轮次 | 通知发送未隔离 | 通知发送包 try/except，失败仅记 warning | `8b27321` |
| A-8 | `format_message` 剥掉任何以 "ai" 开头文本的前两个字符（如 @bot "airpods 怎么样" → "rpods 怎么样" 进 LLM） | 前缀判断无词边界 | 仅当文本恰为 `ai`（不区分大小写）或 `ai` + 空格/全角空格时剥离唤醒词 | `ba22251` |

### B. 安全加固（3 项）

| # | 问题 | 风险 | 修复 | 提交 |
| --- | --- | --- | --- | --- |
| B-1 | Tavily 搜索请求带 `ssl=False`（全仓库唯一一处） | 请求携带 `Authorization` API key，且搜索结果拼入对话上下文；中间人可窃取密钥、注入 LLM 输入 | 移除 `ssl=False`，恢复证书校验 | `27826a1` |
| B-2 | `search_api` 默认占位符 `"your api"` 被原样放入请求头发往第三方 | 未配置时每次搜索都发送无效凭据 | `get_search` 入口检测空值/占位值（`your api`、`your_api` 等），直接返回友好提示，不发起网络请求 | `27826a1` |
| B-3 | 6 个布尔配置项不参与类型校验：`runtime_watch_enabled`、`generated_tools_enabled`、`fastai_enabled`、`emotions_enabled`、`private_chat_enabled`、`show_datetime` | 配置成字符串 `"false"` 按真值处理，功能无法关闭；拼错的配置键被静默忽略，用户以为生效 | 六项并入布尔校验列表（传字符串直接 `ValueError`）；未知键记 warning（仍保留在配置映射中，避免回写时删掉用户文件里的额外键，不做硬拒绝以兼容存量部署） | `7f83467` |

## 回归测试

提交 `a8bfeca`，共 326 行：

| 测试文件 | 覆盖 |
| --- | --- |
| `tests/test_state_store.py`（新） | `__contains__` 对缺失键返回 False 且不创建条目；str 键重置场景回归 |
| `tests/test_compat.py`（新） | 超时抛内建 `TimeoutError`；外部取消不被超时层吞掉 |
| `tests/test_llm_api.py`（新） | `[DONE]` 各变体；`event:`/`retry:`/注释行跳过；裸 JSON（ndjson）兼容 |
| `tests/test_config.py`（新） | 六个布尔字段拒绝字符串；未知键告警但不阻断加载、不影响已知键 |
| `tests/test_member_cache.py` | 查询失败不写缓存，第二次请求重新查询成功 |
| `tests/test_message_context.py` | `format_message` 对 "airpods"/"ai 你好"/"AI"/"ai助手" 的边界 |
| `tests/test_search.py` | 占位/空密钥不发请求；请求参数不再含 `ssl` |
| `tests/test_chat_runtime.py` | 取消契约更新：任务以 `CancelledError` 收场，提示经 `matcher.send` 送达，冷却仍释放 |

## 验证状态

本机无可运行 pytest 的依赖环境（缺 nonebot adapter 等依赖），已完成两层替代验证，**pytest 全量与 ruff 尚未在有依赖的环境运行**：

1. 语法编译：全部 19 个改动文件 `py_compile` 通过。
2. 冒烟验证：以 stub 隔离第三方依赖后真实导入 `compat`/`config`/`state_store`/`llm_api`，执行 36 项功能断言（超时语义、外部取消传播、布尔校验、未知键告警、store 成员判断、SSE 全部变体），**36/36 通过**。

进入正式环境后应执行：

```bash
python -m pytest tests/test_state_store.py tests/test_compat.py tests/test_llm_api.py \
  tests/test_config.py tests/test_member_cache.py tests/test_message_context.py \
  tests/test_search.py tests/test_chat_runtime.py -q
ruff check .
```

## 提交记录

```text
a8bfeca test: 为核心修复包补齐回归测试
7f83467 fix: 补全配置布尔校验并对未知配置键告警
27826a1 fix: 搜索请求恢复 TLS 校验并拦截占位符 API key
ba22251 fix: format_message 仅在独立唤醒词时剥离 ai 前缀
8b27321 fix: 重试通知发送失败不再中断进行中的请求
66cb4dd fix: 成员名查询失败不再把降级名缓存整个 TTL
69122bb fix: 统一聊天链路 TimeoutError 身份，修复 3.10 上的超时分流
dabfaf4 fix: 管理员取消时重新抛出 CancelledError 而非吞掉
971c746 fix: 工具总结兜底请求增加异常保护
5f43c43 fix: SSE 流式解析只处理 data/裸 JSON 行，坏行降级为跳过
0358805 fix: 重置命令使用 event_user_id 统一 str 键并修正 store __contains__ 恒真
（基线 1043b0a = feat/llm-runtime-backpressure 分支头）
```

## 明确不做（见待修复清单）

DNS rebinding/重定向防护（需 connector 层重构）、Redis admission 通知机制、spool 诊断 O(N) 增量化、代际资源租约获取时机、关闭/排空超时、`tool_manager.py` 拆分、AST walrus 追踪、死代码清理（batch queues / `is_repeat_ask_dict`）、分类缓存 single-flight、模型路由目录缓存。完整清单与建议批次见 [10-pending-issues-backlog](./10-pending-issues-backlog.md)。

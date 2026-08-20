---
title: 05-plan1-completion-audit
date: 2026-08-20T00:00:00+00:00
lastmod: 2026-08-20T00:00:00+00:00
---

# Plan 1 完成审计

本文把 [Plan 1](./01-plan-security-refactor.md) 与 [实施 Backlog](./04-implementation-backlog.md) 的 A-01～A-05、B-01～B-08、C-01～C-07 映射到当前开发工作树中的源码、具体测试 node 和发布门禁。它是审计索引，不把“代码已实现”“某次定向测试通过”“当前完整门禁通过”“已推送 GitHub”“已部署生产”混为一谈。

## 当前结论

- A、B、C 各项均已有实现和对应测试；C 的 canonical lifecycle 当前为 schema v3，并兼容读取 schema v2。
- 本轮已先完成 Milestone A 定向复核：Python 3.12.13 + NoneBot 2.4.4 下 31 个非沙箱验收 node 与 mandatory root 下 11 个 A 相关真实 Sandbox case 全部通过；Ruff 全量通过。
- Milestone B 定向复核为 24 个非沙箱 node 与 12 个真实 Sandbox case；C-01～C-06 定向复核为 38 个 case，均通过。
- 最新本地总门禁已完成：四个 Python 版本普通全量各 `347 passed, 1 skipped`，mandatory root Sandbox `40 passed, 0 skipped`，fresh build/Twine/checksum 与四组 checkout 外 package smoke 全部通过。
- 当前工作树仍未提交、推送或部署，远端 `release-gate` 与 required check 尚无首次 green 证据。
- 本轮发现并修复了 CPython 3.10 对 `MappingProxyType` frame builtins 执行 import 时的内部错误；3.10 使用拒绝公开变更入口的冻结 builtins dict 兼容层，3.11+ 保持 mapping proxy，四版本 worker 定向测试各 `14 passed`。

状态定义：

- `实现完成`：源码和定向测试均已落盘。
- `本地门禁完成`：最新未提交工作树已经通过本文矩阵，但还不是远端提交证据。
- `远端待验证`：必须由 GitHub Actions 对精确提交产生证据。

## 总门禁台账

| 门禁 | 当前要求 | 当前证据 |
| --- | --- | --- |
| Ruff | `ruff check nonebot_plugin_moellmchats tests` | 本轮最新共享树通过 |
| 普通矩阵 | Python 3.10、3.11、3.12、3.13，执行 `pytest -q --ignore=tests/test_sandbox_integration.py` | 四版本各 `347 passed, 1 skipped`；串行执行，避免共享 `tests/.data` 互扰 |
| NoneBot 兼容 | Python 3.12 + NoneBot 2.4.4 + OneBot 2.4.6 | `347 passed, 1 skipped` |
| Mandatory Sandbox | root 下执行完整 `tests/test_sandbox_integration.py`，JUnit `tests > 0` 且 `skipped=0` | `40 passed, 0 skipped`，JUnit 复核 `failures=0, errors=0` |
| Build | fresh sdist + wheel、Twine、checksum、来源 metadata | 通过；来源元数据标记 `local-uncommitted-worktree` |
| Package smoke | Python 3.10/3.12 × wheel/sdist，checkout 外安装、加载与 `reload("package-smoke")` | 四组全部 `PACKAGE_SMOKE_OK`，generation=1 |
| GitHub 聚合 | `release-gate` fail closed 聚合 test/sandbox/build/package | 未提交，远端待验证 |
| Promotion | job 列表完整；恰好一个精确名 `release-gate` 且 `completed/success`；下载原 run artifact | workflow 已实现，尚未对远端 run 执行 |

本轮分阶段证据（均针对未提交工作树，不代表远端或生产状态）：

| 阶段 | 环境 | 结果 |
| --- | --- | --- |
| Milestone A 非沙箱定向验收 | Python 3.12.13、NoneBot 2.4.4、OneBot 2.4.6 | `31 passed` |
| Milestone A mandatory Sandbox 定向验收 | Linux root、真实 namespace / UID drop / libseccomp 前提 | `11 passed` |
| Milestone B 非沙箱 / Sandbox 定向验收 | Python 3.12.13 + mandatory root | `24 passed` / `12 passed` |
| Milestone C-01～C-06 定向验收 | Python 3.12.13、NoneBot 2.4.4、OneBot 2.4.6 | `38 passed` |
| Plan 1 完整 mandatory Sandbox | Linux root、真实 namespace / UID drop / Landlock / libseccomp 前提 | `40 passed, 0 skipped` |

## Milestone A：0.25.0-rc1

### A-01 Generated Tool 默认网络隔离

- 源码：`nonebot_plugin_moellmchats/generated_tool_runner.py`、`generated_tool_isolation.py`、`generated_tool_worker.py`、`generated_tools.py`、`custom_tool_loader.py`。
- 关键 node：
  - `tests/test_generated_tool_runner_policy.py::test_generated_and_compatibility_entrypoints_force_network_isolation`
  - `tests/test_generated_tool_runner_policy.py::test_generated_execution_fails_closed_without_unshare`
  - `tests/test_generated_tool_runner_policy.py::test_isolation_command_has_mount_pid_and_kill_boundary`
  - `tests/test_generated_tool_runner_policy.py::test_preflight_uses_generated_policy_and_checks_network_namespace`
  - `tests/test_sandbox_integration.py::test_sandbox_uses_private_fixed_uts_identity`
  - `tests/test_sandbox_integration.py::test_sandbox_network_namespace_blocks_parent_loopback`
  - `tests/test_sandbox_integration.py::test_sandbox_parent_unix_socket_requires_both_capabilities`
  - `tests/test_sandbox_integration.py::test_sandbox_host_filesystem_false_blocks_af_vsock`
  - `tests/test_sandbox_integration.py::test_sandbox_blocks_non_unix_socketpair`
  - `tests/test_sandbox_integration.py::test_sandbox_blocks_unix_datagram_socketpair_reconnect_bypass`
- 验收语义：所有文件/生成工具进入 PID/mount/IPC/UTS namespace 并使用固定 hostname，`network=false` 时再进入 network namespace 并拒绝全部 `socket(2)`；仅联网但无 host-filesystem 时拒绝 AF_UNIX/AF_VSOCK，受限 `socketpair` 只保留 AF_UNIX/STREAM。缺少隔离前提即 fail closed。
- 状态：**本地门禁完成；远端提交与 `release-gate` 待验证。**

### A-02 PendingAction 二阶段确认

- 源码：`nonebot_plugin_moellmchats/pending_actions.py`、`llm_tools.py`、`tool_execution.py`、`__init__.py`。
- 关键 node：
  - `tests/test_llm_tools.py::test_mutating_tool_always_requires_separate_nonce_confirmation`
  - `tests/test_pending_actions.py::test_pending_action_is_bound_hashed_and_one_shot`
  - `tests/test_pending_actions.py::test_wrong_user_or_group_cannot_consume_action`
  - `tests/test_pending_actions.py::test_expiry_and_generation_change_fail_closed`
  - `tests/test_pending_actions.py::test_concurrent_confirmation_executes_at_most_once`
  - `tests/test_pending_actions.py::test_confirmation_executes_fixed_snapshot_arguments_and_rechecks_permission`
- 验收语义：首次 mutating 调用只生成 nonce；另消息确认受 Bot/adapter/user/session、参数 hash、generation、bundle digest、TTL 与一次性消费约束。
- 状态：**本地门禁完成；远端提交与 `release-gate` 待验证。**

### A-03 Generated Tool 默认 superuser

- 源码：`nonebot_plugin_moellmchats/tool_contracts.py`、`generated_tools.py`、`tool_artifacts.py`、`llm_tools.py`。
- 关键 node：
  - `tests/test_generated_tools.py::test_generated_permission_requires_persisted_human_policy`
  - `tests/test_generated_tools.py::test_generated_superuser_request_cannot_be_relaxed`
  - `tests/test_generated_tools.py::test_hash_change_and_failed_review_cannot_be_approved`
  - `tests/test_generated_tools.py::test_superuser_tools_are_filtered_from_catalog_and_schema`
- 验收语义：manifest permission 只是申请；人工 grant 绑定精确 bundle digest 和工具，损坏或版本变化均 fail closed 到 superuser。
- 状态：**本地门禁完成；远端提交与 `release-gate` 待验证。**

### A-04 Capability 基础结构

- 源码：`nonebot_plugin_moellmchats/tool_contracts.py`、`custom_tool_loader.py`、`generated_tools.py`、`tool_artifacts.py`、`generated_tool_runner.py`、`generated_tool_isolation.py`。
- 关键 node：
  - `tests/test_tool_contracts.py::test_generated_policy_defaults_to_workspace_only`
  - `tests/test_tool_contracts.py::test_policy_uses_strict_requested_and_admin_intersection`
  - `tests/test_tool_contracts.py::test_capability_mapping_rejects_unknown_or_non_boolean_values`
  - `tests/test_tool_contracts.py::test_host_filesystem_and_secrets_are_deny_by_default`
  - `tests/test_custom_tool_policy.py::test_custom_file_accepts_static_explicit_host_capabilities`
  - `tests/test_generated_tools.py::test_generated_capabilities_are_bounded_by_safe_policy`
  - `tests/test_sandbox_integration.py::test_sandbox_host_filesystem_false_blocks_host_xattrs`
  - `tests/test_sandbox_integration.py::test_secrets_capability_does_not_inject_host_environment`
- 验收语义：严格五个布尔字段 `network/process/workspace/host_filesystem/secrets`；effective 为 requested 与 admin ceiling 交集；Generated 上限仅 workspace，secrets 不注入宿主密钥。
- 状态：**本地门禁完成；远端提交与 `release-gate` 待验证。**

### A-05 Draft 文件权限修复

- 源码：`nonebot_plugin_moellmchats/private_files.py`、`config.py`、`generated_tools.py`、`model_selector.py`、`mcp_manager.py`。
- 关键 node：
  - `tests/test_private_files.py::test_config_storage_is_tightened_and_atomic_rewrites_stay_private`
  - `tests/test_private_files.py::test_protected_tree_rejects_symlink_with_path_diagnostic`
  - `tests/test_private_files.py::test_protected_path_rejects_foreign_owner_with_uid_diagnostic`
  - `tests/test_private_files.py::test_config_parser_rejects_symlink_instead_of_replacing_target`
  - `tests/test_generated_tools.py::test_generated_storage_permissions_are_private_and_versions_stay_immutable`
- 验收语义：配置目录 `0700`、敏感/草稿文件 `0600`，owner/no-follow 检查 fail closed，immutable version 为 `0500` / `0400`。
- 状态：**本地门禁完成；远端提交与 `release-gate` 待验证。**

## Milestone B：0.25.0-rc2

### B-01 ToolArtifact

- 源码：`nonebot_plugin_moellmchats/tool_artifacts.py`、`tool_contracts.py`、`custom_tool_loader.py`、`generated_tools.py`、`tool_manager.py`。
- 关键 node：
  - `tests/test_tool_artifacts.py::test_custom_artifact_detaches_contract_and_verifies_pinned_generation`
  - `tests/test_tool_artifacts.py::test_generated_artifact_binds_exact_manifest_tool_and_contract`
  - `tests/test_tool_artifacts.py::test_artifact_digest_binds_effect_permission_and_limits`
  - `tests/test_tool_artifacts.py::test_artifact_survives_runtime_snapshot_freeze_without_mappingproxy_copy`
- 验收语义：源码、Schema、ToolSpec、安全契约、generation 与 digest 形成不可变制品，loader 不发布半制品。
- 状态：**本地门禁完成；远端提交与 `release-gate` 待验证。**

### B-02 Custom Tool Source Snapshot

- 源码：`nonebot_plugin_moellmchats/custom_tool_loader.py`、`tool_artifacts.py`、`generated_tool_runner.py`、`runtime_reload.py`。
- 关键 node：
  - `tests/test_runtime_reload.py::test_runtime_generation_pins_custom_tool_source`
  - `tests/test_generated_tool_runner_policy.py::test_artifact_execution_uses_snapshot_and_fd3_not_live_path`
  - `tests/test_tool_artifacts.py::test_custom_artifact_detaches_contract_and_verifies_pinned_generation`
- 验收语义：候选 generation 只读一次源码 bytes；活动请求只执行其固定 artifact，不回读后来修改的路径。
- 状态：**本地门禁完成；远端提交与 `release-gate` 待验证。**

### B-03 Generated Bundle Runtime Digest

- 源码：`nonebot_plugin_moellmchats/tool_artifacts.py`、`generated_tools.py`、`generated_tool_runner.py`、`tool_runtime.py`。
- 关键 node：
  - `tests/test_tool_artifacts.py::test_generated_artifact_binds_exact_manifest_tool_and_contract`
  - `tests/test_tool_artifacts.py::test_bundle_digest_normalizes_crlf_like_existing_store`
  - `tests/test_generated_tool_runner_policy.py::test_artifact_entry_rejects_unpinned_digest_before_dispatch`
  - `tests/test_generated_tool_runner_policy.py::test_generated_artifact_rejects_wrong_bundle_digest_before_dispatch`
  - `tests/test_ast_policy_loaders.py::test_store_digest_matches_tool_artifact_canonical_crlf_and_unicode`
- 验收语义：Store、Artifact、Snapshot 与 runner 共享 canonical bundle digest，错误 generation/digest 在 dispatch 前 fail closed。
- 状态：**本地门禁完成；远端提交与 `release-gate` 待验证。**

### B-04 Runner Protocol FD 分离

- 源码：`nonebot_plugin_moellmchats/generated_tool_runner.py`、`generated_tool_worker.py`、`tool_execution.py`。
- 关键 node：
  - `tests/test_sandbox_integration.py::test_sandbox_artifact_fd3_and_uid_drop`
  - `tests/test_sandbox_integration.py::test_sandbox_log_stream_flood_is_bounded_and_cleaned_up`
  - `tests/test_sandbox_integration.py::test_sandbox_fd3_protocol_flood_is_bounded_and_cleaned_up`
  - `tests/test_generated_tools.py::test_runner_rejects_output_flood`
  - `tests/test_generated_tools.py::test_runner_rejects_fd3_protocol_result_flood`
- 验收语义：stdin 只传请求，FD3 传版本化结果，stdout/stderr 只作有界日志；三路读取、超时和取消都清理 FD 与进程组。FD3 不认证恶意代码。
- 状态：**本地门禁完成；远端提交与 `release-gate` 待验证。**

### B-05 Workspace File Count 限制

- 源码：`nonebot_plugin_moellmchats/generated_tool_runner.py`、`generated_tool_isolation.py`、`config.py`。
- 关键 node：
  - `tests/test_generated_tool_runner_policy.py::test_workspace_scanner_enforces_files_depth_bytes_and_links`
  - `tests/test_generated_tool_runner_policy.py::test_fast_process_is_rejected_by_final_workspace_scan`
  - `tests/test_sandbox_integration.py::test_sandbox_workspace_total_bytes`
  - `tests/test_sandbox_integration.py::test_sandbox_workspace_single_file_bytes`
  - `tests/test_sandbox_integration.py::test_sandbox_workspace_depth`
  - `tests/test_sandbox_integration.py::test_sandbox_workspace_file_count_uses_final_scan`
- 验收语义：总字节、单文件、条目数、深度、符号链接和特殊文件均有 fail-closed 限制，结束后强制复扫。
- 状态：**本地门禁完成；远端提交与 `release-gate` 待验证。**

### B-06 Workspace Scanner Async 化

- 源码：`nonebot_plugin_moellmchats/generated_tool_runner.py`。
- 关键 node：
  - `tests/test_generated_tool_runner_policy.py::test_workspace_scan_does_not_block_event_loop`
  - `tests/test_sandbox_integration.py::test_sandbox_pid_namespace_cleans_detached_descendant_before_final_scan`
- 验收语义：workspace 遍历移到 `asyncio.to_thread()`，运行期 watch 与最终 scan 不同步阻塞事件循环。
- 状态：**本地门禁完成；远端提交与 `release-gate` 待验证。**

### B-07 AST Policy Engine

- 源码：`nonebot_plugin_moellmchats/ast_policy.py`、`custom_tool_loader.py`、`generated_tools.py`、`tool_artifacts.py`。
- 关键 node：
  - `tests/test_ast_policy.py::test_generated_dynamic_features_and_process_calls_are_denied`
  - `tests/test_ast_policy.py::test_handler_reports_propagate_only_reachable_helper_effects`
  - `tests/test_ast_policy.py::test_http_request_method_effect_distinguishes_reads_and_writes`
  - `tests/test_ast_policy.py::test_file_open_and_stream_write_forms_are_conservatively_mutating`
  - `tests/test_ast_policy_loaders.py::test_generated_helper_mutation_promotes_declared_read_only_spec`
  - `tests/test_ast_policy_loaders.py::test_custom_capability_is_checked_for_each_handler_call_graph`
- 验收语义：模块、handler、可达 helper 和 tests 分开分析，输出 ALLOW/DENY/CAPABILITY_REQUIRED/RISK；保守提升 mutating，不替代 OS 隔离或人工审阅。
- 状态：**本地门禁完成；远端提交与 `release-gate` 待验证。**

### B-08 禁止 Generated Tool subprocess

- 源码：`nonebot_plugin_moellmchats/ast_policy.py`、`generated_tool_worker.py`、`generated_tool_runner.py`、`generated_tool_isolation.py`。
- 关键 node：
  - `tests/test_generated_tools.py::test_generated_runner_rejects_subprocess_before_execution`
  - `tests/test_generated_tools.py::test_process_false_blocks_exec_replacing_worker`
  - `tests/test_generated_tool_worker.py::test_process_isolation_fails_closed_without_libseccomp`
  - `tests/test_sandbox_integration.py::test_sandbox_process_false_denies_spawn_and_exec`
  - `tests/test_sandbox_integration.py::test_sandbox_process_true_executes_fixed_system_binary_only`
  - `tests/test_sandbox_integration.py::test_sandbox_blocks_inherited_session_keyring_for_all_capabilities`
- 验收语义：Generated AST 与 runner 都不能放宽 process；`process=false` 使用 NPROC + seccomp；Custom `process=true` 仍只得到固定 executable roots，并受其他 capability 与 PendingAction 约束。`add_key`/`request_key`/`keyctl` 在所有 capability 组合下无条件拒绝。
- 状态：**本地门禁完成；远端提交与 `release-gate` 待验证。**

## Milestone C：0.25 Stable

### C-01 Tool Lifecycle State Machine

- 源码：`nonebot_plugin_moellmchats/generated_tool_lifecycle.py`、`generated_tools.py`、`tool_authoring.py`。
- 关键 node：
  - `tests/test_generated_tool_lifecycle.py::test_schema_v3_round_trip_is_canonical_and_immutable`
  - `tests/test_generated_tool_lifecycle.py::test_schema_v2_is_upgraded_with_explicit_unverified_evidence`
  - `tests/test_generated_tool_lifecycle.py::test_evidenced_states_and_transitions_fail_closed_without_evidence`
  - `tests/test_generated_tool_lifecycle.py::test_explicit_draft_state_machine_and_failures`
  - `tests/test_generated_tools_lifecycle_integration.py::test_create_draft_only_records_draft_and_rejects_legacy_status`
  - `tests/test_generated_tools_lifecycle_integration.py::test_failure_entrypoints_persist_structured_canonical_evidence`
- 验收语义：schema v3 canonical，读取 v2 后显式生成迁移 evidence；DraftEvidence digest-bound；`create_draft()` 只创建 Draft，专用入口逐步推进。
- 状态：**本地门禁完成；远端提交与 `release-gate` 待验证。**

### C-02 Full Draft Review

- 源码：`nonebot_plugin_moellmchats/generated_tools.py`、`tool_authoring.py`、`__init__.py`。
- 关键 node：
  - `tests/test_generated_tools.py::test_complete_draft_review_is_canonical_lossless_and_bounded`
  - `tests/test_generated_tools.py::test_stale_parallel_approval_requires_fresh_review_stamp`
  - `tests/test_generated_tools_lifecycle_integration.py::test_review_uses_canonical_state_and_stamp_after_metadata_tamper`
  - `tests/test_generated_tools_lifecycle_integration.py::test_review_stamp_rejects_any_lifecycle_change_after_review`
  - `tests/test_generated_tools_lifecycle_integration.py::test_review_stamp_explicitly_binds_active_digest`
  - `tests/test_tool_authoring.py::test_authoring_uses_selected_then_summary_and_persists_review`
- 验收语义：七区段无损分页；完整 64 位 stamp 绑定 draft/digest、revision/state digest、active digest；批准必须复制三参数命令。
- 状态：**本地门禁完成；远端提交与 `release-gate` 待验证。**

### C-03 Watcher 外层恢复

- 源码：`nonebot_plugin_moellmchats/runtime_reload.py`、`runtime_snapshot.py`、`__init__.py`。
- 关键 node：
  - `tests/test_runtime_reload.py::test_watcher_recovers_from_initial_fingerprint_failure`
  - `tests/test_runtime_reload.py::test_watcher_recovers_from_config_read_failure`
  - `tests/test_runtime_reload.py::test_watcher_recovers_from_reload_failure`
  - `tests/test_runtime_reload.py::test_watcher_backoff_is_bounded_and_resets_after_success`
  - `tests/test_runtime_reload.py::test_unexpected_watcher_task_completion_is_logged`
  - `tests/test_runtime_reload.py::test_broken_resource_retains_snapshot_and_watcher_keeps_retrying`
- 验收语义：永久循环、取消单独传播、0.5～30 秒有界退避、异常可见，指纹 I/O 在线程中执行，失败保留旧 snapshot。
- 状态：**本地门禁完成；远端提交与 `release-gate` 待验证。**

### C-04 Lifecycle File Lock

- 源码：`nonebot_plugin_moellmchats/generated_tool_lifecycle.py`、`generated_tools.py`。
- 关键 node：
  - `tests/test_generated_tool_lifecycle.py::test_multiprocess_cas_allows_exactly_one_writer`
  - `tests/test_generated_tool_lifecycle.py::test_shared_exclusive_lock_contention_is_bounded`
  - `tests/test_generated_tool_lifecycle.py::test_process_crash_releases_fixed_flock`
  - `tests/test_generated_tool_lifecycle.py::test_post_replace_directory_fsync_retries_then_succeeds`
  - `tests/test_generated_tool_lifecycle.py::test_post_replace_directory_fsync_exhaustion_remains_uncertain`
  - `tests/test_generated_tools_lifecycle_integration.py::test_visible_after_never_resolves_unconfirmed_directory_durability`
- 验收语义：固定 flock + owner/no-follow + CAS；directory fsync 三次重试耗尽后保持 uncertain，只有 durability 已确认的回读不确定才精确调和 before/after。
- 状态：**本地门禁完成；远端提交与 `release-gate` 待验证。**

### C-05 Approve 原子事务

- 源码：`nonebot_plugin_moellmchats/generated_tools.py`、`runtime_reload.py`、`generated_tool_lifecycle.py`、`__init__.py`。
- 关键 node：
  - `tests/test_lifecycle_runtime_transaction.py::test_generated_change_builds_exact_after_state_and_override_snapshot`
  - `tests/test_lifecycle_runtime_transaction.py::test_candidate_failure_performs_no_durable_write_or_runtime_publish`
  - `tests/test_lifecycle_runtime_transaction.py::test_durable_cas_conflict_never_publishes_runtime_candidate`
  - `tests/test_lifecycle_runtime_transaction.py::test_successful_durable_commit_publishes_exact_lifecycle_stamp`
  - `tests/test_lifecycle_runtime_transaction.py::test_runtime_publish_failure_keeps_committed_canonical_state_for_retry`
  - `tests/test_lifecycle_runtime_transaction.py::test_repeated_cancellation_still_waits_for_durable_thread_completion`
- 验收语义：所有 active 管理命令只走 RuntimeReloader 的候选、durable CAS、runtime publish 三阶段；Store commit/publish 方法均为 internal。
- 状态：**本地门禁完成；远端提交与 `release-gate` 待验证。**

### C-06 Rollback 原子事务

- 源码：`nonebot_plugin_moellmchats/generated_tools.py`、`generated_tool_lifecycle.py`、`runtime_reload.py`、`runtime_snapshot.py`、`tool_manager.py`。
- 关键 node：
  - `tests/test_generated_tools_lifecycle_integration.py::test_rollback_requires_exact_readonly_owned_bundle_integrity`
  - `tests/test_generated_tool_lifecycle.py::test_durable_immutable_publish_and_commit_plan`
  - `tests/test_generated_tool_lifecycle.py::test_publish_rejects_manifest_bundle_mismatch_and_writable_existing_version`
  - `tests/test_lifecycle_runtime_transaction.py::test_real_generated_management_chain_and_second_watcher_converge`
  - `tests/test_generated_tools_lifecycle_integration.py::test_runtime_mutators_are_not_public_store_entrypoints`
- 验收语义：唯一、非 Archived、owner/no-follow、`0500` 目录、恰好三个 `0400` 普通文件、inode 稳定、完整 digest；权限/拒绝/停用/回滚复用同一私有三阶段事务。
- 状态：**本地门禁完成；远端提交与 `release-gate` 待验证。**

### C-07 Sandbox Integration CI

- 源码/门禁：`.github/workflows/ci.yml`、`.github/workflows/pypi-publish.yml`、`tests/test_sandbox_integration.py`。
- 关键 node：完整 `tests/test_sandbox_integration.py`，重点包括：
  - `test_sandbox_workspace_is_the_only_writable_mount`
  - `test_sandbox_uses_private_fixed_uts_identity`
  - `test_sandbox_host_filesystem_false_blocks_sensitive_reads`
  - `test_sandbox_host_filesystem_false_blocks_host_xattrs`
  - `test_secrets_capability_does_not_inject_host_environment`
  - `test_sandbox_parent_unix_socket_requires_both_capabilities`
  - `test_sandbox_host_filesystem_false_blocks_af_vsock`
  - `test_sandbox_blocks_non_unix_socketpair`
  - `test_sandbox_blocks_unix_datagram_socketpair_reconnect_bypass`
  - `test_sandbox_ipc_namespace_blocks_parent_sysv_shared_memory`
  - `test_sandbox_blocks_inherited_session_keyring_for_all_capabilities`
  - `test_sandbox_process_false_denies_spawn_and_exec`
  - `test_sandbox_pid_namespace_cleans_detached_descendant_before_final_scan`
- CI 语义：普通矩阵排除 root-only 文件；mandatory root job 对必要 OS 隔离 fail closed、禁止 skip；build 只产一次 artifact；package job 在 checkout 外验证四种组合；`release-gate` 聚合所有前置 job。隔离不使用 cgroup，也不是完整 syscall allowlist；`stat`/`lstat`/`readlink` 等宿主路径元数据残余可见是明确剩余边界。
- Promotion 语义：API 返回的 job 列表必须完整；必须恰好一个名称精确为 `release-gate` 的 job，且 `status=completed`、`conclusion=success`，之后只下载该 run 的原 artifact。
- 状态：**workflow、测试 node 与最新本地总门禁已完成；mandatory root 精确结果为 `40 passed, 0 skipped`。首次远端 green 与 required check 仍待验证。**

## 最终关闭条件

Plan 1 只有同时满足以下条件才可从“实现完成”改为“发布门禁完成”：

1. [x] 最新工作树 Ruff、Python 3.10～3.13 普通矩阵、Python 3.12 + NoneBot 2.4.4 全部通过。
2. [x] mandatory root Sandbox 在最新 UTS/socket/keyring/xattr 增量下 `40 passed, 0 skipped`。
3. [x] fresh sdist/wheel、Twine、checksum 与四组 checkout 外 package smoke 全部通过。
4. [ ] 仅提交计划内文件，`git diff --cached --check` 通过；未跟踪 `uv.lock` 不被误纳入。
5. [ ] GitHub 对精确提交产生唯一成功的 `release-gate`，并配置为 required check。
6. [ ] promotion 如执行，只能下载并验证该 CI run 的原 artifact；不得重新构建或自动发布 PyPI。
7. [x] 生产切换、Qiqi 依赖更新和进程重启仍需另行授权，不属于本文的代码/文档完成状态；本轮未操作生产。

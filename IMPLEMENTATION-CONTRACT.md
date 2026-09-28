# Runner v1 实现合同（v0.6 文档未写死部分的补齐）

范围：本文件补齐 `DESIGN.md` v0.6 与 `AUDIT-v0.6.md` 已定方向中**尚未写死、但实现通用框架必需**的细节：端点、JSON 字段、SQLite 表、封存事务顺序。
约束：本文只固定实现细节；凡涉及权限边界或与设计文档冲突处，一律取更严格行为（见 §9）。
本文不改变任何已定合同，也不授权真实设备/模拟器/ADB/Oracle 接入。

## 1. 传输与凭证

- 传输：`http.server` 上的版本化 HTTP/JSON，仅监听 `127.0.0.1`（默认 `127.0.0.1:8642`）。路径前缀 `/v1`。
- 鉴权：`Authorization: Bearer <token>`。控制器在创建会话时签发会话级限权凭证；库中只保存 `sha256(salt || token)`。
  凭证绑定 `session_id`、允许方法集合、对象归属与截止时间。请求体中的 `session_id` 仅作一致性校验；不一致 → `403 session_mismatch`。
- 传输元数据（`schema_version`、`reason_code` 集合版本、字节计费信息）走响应头，不进入业务字段：
  `X-Runner-Schema-Version: 1.0`，`X-Runner-Response-Bytes: <n>`，`X-Runner-Truncated: true|false`。
  因此 `begin_attempt` 的 JSON body 只有 `status` / `attempt_id` / `reason_code` 三个业务字段。
- 幂等：所有变更请求必须带 `idempotency_key`（20≤长度≤128，`[A-Za-z0-9._:-]`）。读取请求不需要。
- 只允许 `POST`（变更/查询动作）与 `GET`（纯读取）。任何路径不在下表内 → `404 not_found`；方法不符 → `405 method_not_allowed`。
- **不提供**任何通用 shell / PowerShell / ADB / 模拟器 console / 文件读写 / 任意 Intent 入口。`/v1/exec`、`/v1/shell`、`/v1/adb`、`/v1/devices`、`/v1/files` 等一律 404 并记拒绝事件。

### 1.1 协议错误（进入业务回执之前）

`{"error":{"code":"<code>","message":"<净化短句>"}}`，HTTP 状态码映射见括号。受限集合：

`malformed_request`(400)、`unauthenticated`(401)、`session_mismatch`(403)、`permission_denied`(403)、
`not_found`(404)、`method_not_allowed`(405)、`idempotency_conflict`(409)、`payload_too_large`(413)、
`unsupported_capability`(422)、`environment_mismatch`(409)、`rate_limited`(429)、`internal_error`(500)。

`message` 只含稳定短句，不含内部路径、SQL、堆栈、判据或耗时侧信道。协议错误不消耗任何预算计数器，
但会写拒绝事件并受限流约束（同会话 60 秒内 >240 次拒绝 → `rate_limited`）。

### 1.2 端点表

| 方法 | 路径 | 语义 | 幂等键 | 主要返回字段 |
| --- | --- | --- | --- | --- |
| GET | `/v1/health` | 无需鉴权的存活探测 | 否 | `status`,`service_version`,`schema_version` |
| GET | `/v1/capabilities` | `get_capabilities` | 否 | `session_id`,`actions[]`,`artifact_kinds[]`,`unavailable[]` |
| GET | `/v1/session` | `get_session` | 否 | `session_id`,`state`,`can_continue`,`sample_id`,`resource_roles`,`budget_remaining`,`active_attempt_id`,`finalized_candidate_id` |
| GET | `/v1/operations/{operation_id}` | `get_operation` | 否 | `operation_id`,`method`,`state`,`result`,`reason_code`,`error` |
| POST | `/v1/operations/{operation_id}/cancel` | `cancel_operation` | 是 | `operation_id`,`cancel_requested`,`state` |
| POST | `/v1/candidates` | `submit_candidate` | 是 | `operation_id`,`candidate_id`,`tree_hash`,`file_count`,`source_bytes` |
| POST | `/v1/builds` | `build_candidate` | 是 | `operation_id`,`build_id`,`state`,`apk_artifact_id`,`apk_sha256`,`package_name`,`entry_component`,`reason_code` |
| POST | `/v1/attempts` | `begin_attempt` | 是 | **仅** `status`,`attempt_id`,`reason_code` |
| GET | `/v1/attempts/{attempt_id}` | `get_attempt` | 否 | `attempt_id`,`status_version`,`stages{}`,`device_health`,`remaining_budget`,`truncated`,`truncation_reason` |
| POST | `/v1/attempts/{attempt_id}/actions` | `perform_action`（白名单：`launch_candidate`） | 是 | `action_id`,`action_name`,`state`,`operation_id`,`reason_code` |
| POST | `/v1/attempts/{attempt_id}/finish` | `finish_attempt` | 是 | `attempt_id`,`state`,`accepted` |
| POST | `/v1/attempts/{attempt_id}/abort` | `abort_attempt` | 是 | `attempt_id`,`state`,`observation_window_complete` |
| GET | `/v1/attempts/{attempt_id}/tasks` | `list_tasks` | 否 | `tasks[]`,`cursor`,`truncated` |
| GET | `/v1/tasks/{task_id}` | `get_task` | 否 | `task_id`,`state`,`readiness`,`exit_reason`,`deadline_utc`,`required`,`dependencies[]`,`evidence_refs[]` |
| POST | `/v1/tasks/{task_id}/wait` | `wait_task` | 是 | `task_id`,`condition`,`satisfied`,`state`,`reason_code` |
| POST | `/v1/tasks/{task_id}/stop` | `stop_task` | 是 | `task_id`,`stop_requested`,`exit_reason` |
| GET | `/v1/feedback` | `read_feedback` | 否 | `candidate_id`,`page_index`,`entries[]`,`cursor`,`diagnostics_incomplete`,`truncated`,`truncation_reason` |
| POST | `/v1/finalize` | `finalize_candidate` | 是 | `finalized_candidate_id`,`build_id`,`recipe_hash`,`state` |
| POST | `/v1/session/close` | `close_session` | 是 | `session_id`,`state`,`close_reason` |

`read_feedback` 查询参数：`candidate_id`（必填）、`build_id`（可选，构建诊断）、`attempt_id`（运行诊断必填）、`cursor`（可选）、`kind`（`build|runtime`）。
`get_attempt` 查询参数：`wait_for_status_version`（可选，有界等待 ≤20s）。

管理操作（创建/激活/关闭会话、配置装载）**不在** HTTP 接口内，只经本地管理入口（`python -m runner admin ...` 或进程内函数），Agent 不可达。

### 1.3 阶段、健康与预算投影

- 阶段固定七个：`intake`(接收) `build` `install` `permission_check` `launch` `observe` `cleanup`；状态 `pending|running|completed|failed|skipped|unknown`。
- `device_health ∈ ready|degraded|unavailable|unknown`。
- `remaining_budget = {wall_seconds_remaining, distinct_candidates_remaining, builds_remaining, effective_attempts_remaining}`（整数；`null` 表示该计数器未设上限）。
- `observe=completed` 只表示观察窗口与采集流程结束，不表示触发或漏洞成立。

### 1.4 `status_version`

每次 attempt 的阶段状态/健康/状态机变化 `+1`（同库事务内自增）。`get_attempt?wait_for_status_version=N` 在版本 >N 或超时时返回；
无论版本是否变化，**每个实际返回的响应都计 1 次会话读取**。

## 2. 反馈投影与字节计费

- `get_attempt`：单响应 UTF-8 JSON ≤8192 B，每会话 ≤120 次。超限时最后一个可交付响应带 `truncated=true`+`truncation_reason`。
- `read_feedback`：单页 ≤16384 B；按冻结 `tree_hash` 每候选 ≤4 页且 ≤65536 B；全会话 ≤262144 B。
- 计费对象 = **实际送达的 UTF-8 JSON body 字节数**（含游标、`truncated` 元数据）；重复游标、相同页、空页均计费。
- 计数与字节在写出前于同一 SQLite 事务内预留；预留失败 → 只返回 `{"truncated":true,"truncation_reason":...}` 形态的额度错误，不返回任何状态或诊断内容。
- `truncation_reason ∈ {STATUS_RESPONSE_BYTES_LIMIT, STATUS_CALL_LIMIT, DIAGNOSTIC_PAGE_BYTES_LIMIT, DIAGNOSTIC_CANDIDATE_PAGE_LIMIT, DIAGNOSTIC_CANDIDATE_BYTES_LIMIT, DIAGNOSTIC_SESSION_BYTES_LIMIT, DIAGNOSTIC_READ_BUDGET_EXHAUSTED}`。它是读取额度元数据，**不是** `reason_code`。
- 诊断白名单：
  - 构建：`relative_candidate_source_path`、`line`、`column`、`sanitized_summary`（相对路径为冻结清单内路径；无绝对路径、无命令行、无模板/依赖内部信息）。
  - 运行：仅同 attempt、同候选包名 **且** 同候选实际 UID 的候选自有日志行与候选进程错误。
  - 系统侧：只转成结构化安全事件并按事件类型白名单过滤；`asus_identity`、`fixed_viewer_identity`、`uri`、`intent_content`、`non_candidate_uid` 一律清除；无法归属或净化 → 丢弃该事件并置 `diagnostics_incomplete=true`。
- Oracle 结论、观察结果、ASUS/viewer/URI/Intent、修复态结果、原始全量 logcat **不进入任何** Agent 可见投影（含状态、错误、日志、artifact 引用）。

## 3. SQLite 表

单一 `control.db`（WAL，`foreign_keys=ON`，`busy_timeout=15000`）。所有写入走 `BEGIN IMMEDIATE`。

| 表 | 主键 | 关键列 | 用途 |
| --- | --- | --- | --- |
| `schema_migrations` | `version` | `applied_utc` | 迁移 |
| `sessions` | `session_id` | `state`,`sample_id`,`config_sha256`,`feedback_profile`,`credential_hash`,`credential_salt`,`created_utc`,`activated_utc`,`wall_deadline_utc`,`closed_utc`,`close_reason`,`status_version`,`active_attempt_id`,`finalized_candidate_id`,`finalized_build_id`,`recipe_hash`,`resource_roles_json` | 会话 |
| `counters` | `(session_id,counter_key)` | `limit_value`,`consumed_value`,`reserved_value` | 预算 |
| `counter_ledger` | `entry_id` | `session_id`,`counter_key`,`delta_reserved`,`delta_consumed`,`reason`,`ref_type`,`ref_id`,`created_utc` | 追加式预算流水 |
| `idempotency` | `(session_id,scope,idempotency_key)` | `request_hash`,`state`,`operation_id`,`response_json`,`response_sha256`,`http_status`,`created_utc`,`completed_utc` | 幂等 |
| `operations` | `operation_id` | `session_id`,`method`,`state`,`result_json`,`reason_code`,`error_code`,`attempt_id`,`candidate_id`,`build_id`,`task_id`,`created_utc`,`updated_utc`,`deadline_utc` | 操作 |
| `candidates` | `candidate_id` | `session_id`,`build_profile`,`tree_hash`,`file_manifest_json`,`manifest_sha256`,`path_policy_version`,`file_count`,`source_bytes`,`state`,`created_utc` | 候选 |
| `candidate_files` | `(candidate_id,relative_path)` | `size_bytes`,`sha256`,`ordinal` | 逐文件清单 |
| `tree_hashes` | `(session_id,tree_hash)` | `candidate_id`,`first_seen_utc` | 不同候选计数 |
| `builds` | `build_id` | `session_id`,`candidate_id`,`tree_hash`,`state`,`application_id`,`template_*`,`toolchain_*`,`dependency_lock_sha256`,`signing_profile`,`exit_code`,`log_artifact_id`,`apk_artifact_id`,`apk_sha256`,`package_name`,`version_code`,`signer_fingerprint`,`manifest_sha256`,`permissions_json`,`entry_component`,`build_provenance`,`failure_json`,`created_utc`,`completed_utc` | 构建 |
| `attempts` | `attempt_id` | `session_id`,`attempt_no`,`candidate_id`,`build_id`,`replay_of`,`state`,`execution_status`,`recovery_status`,`device_health`,`effective_agent_attempt`,`infrastructure_retry`,`stages_json`,`status_version`,`launch_dispatch_utc`,`launch_dispatch_monotonic`,`launch_confirmed_monotonic`,`observation_deadline_monotonic`,`prepare_deadline_utc`,`launch_deadline_utc`,`reason_code`,`created_utc`,`closed_utc` | 尝试 |
| `attempt_actions` | `action_id` | `attempt_id`,`session_id`,`action_name`,`params_json`,`params_sha256`,`state`,`operation_id`,`result_json`,`created_utc` | 动作 |
| `tasks` | `task_id` | `attempt_id`,`session_id`,`role`,`state`,`readiness`,`exit_reason`,`required`,`dependencies_json`,`deadline_utc`,`handle`,`evidence_refs_json`,`created_utc`,`updated_utc` | 任务 |
| `artifacts` | `artifact_id` | `session_id`,`kind`,`content_sha256`,`size_bytes`,`media_type`,`state`,`staging_path`,`relative_object_path`,`source`,`provenance`,`attempt_id`,`candidate_id`,`build_id`,`created_utc`,`sealed_utc` | 内容寻址对象 |
| `seal_groups` | `seal_id` | `session_id`,`attempt_id`,`kind`,`state`,`member_hashes_json`,`group_sha256`,`created_utc`,`sealed_utc` | 证据封存组 |
| `events` | `event_id` | `session_id`,`event_type`,`node`,`node_boot_id`,`node_seq`,`utc`,`monotonic`,`event_version`,`candidate_id`,`build_id`,`attempt_id`,`action_id`,`task_id`,`resource_id`,`payload_json` | 追加式事件 |
| `diagnostics` | `diagnostic_id` | `session_id`,`candidate_id`,`tree_hash`,`build_id`,`attempt_id`,`kind`,`ordinal`,`payload_json`,`sanitized`,`created_utc` | 诊断条目（已净化） |
| `feedback_ledger` | `(session_id,tree_hash)` | `pages_used`,`bytes_used` | 诊断计费 |
| `read_ledger` | `(session_id,counter_key)` | `count_used`,`bytes_used` | 状态页计费 |
| `rejections` | `rejection_id` | `session_id`,`scope`,`reason_code`,`error_code`,`detail_sanitized`,`created_utc` | 拒绝事件 |
| `device_leases` | `resource_role` | `session_id`,`attempt_id`,`device_id`,`generation`,`acquired_utc`,`released_utc` | 资源租约 |
| `oracle_evaluations` | `evaluation_id` | `attempt_id`,`session_id`,`oracle_id`,`oracle_version`,`criteria_version`,`verdict`,`evaluation_status`,`reason_private`,`evidence_refs_json`,`created_utc`,`completed_utc` | **私有**判定 |
| `supervisor_state` | `supervisor_id` | `node_boot_id`,`started_utc`,`heartbeat_utc`,`last_sweep_utc` | 监督器 |

对象的可见性由**代码白名单投影**保证：`oracle_evaluations` 与原始证据永远不经过任何 Agent 投影函数。

## 4. 内容寻址与封存事务顺序

目录：`<root>/staging/`、`<root>/objects/<sha2前2位>/<sha256>`、`<root>/quarantine/`、`<root>/control.db`。

写入（`stage_object`）：
1. 事务 A：`INSERT artifacts(state='staged', content_sha256=<写入时计算的哈希>, staging_path=...)`；先落库，后落盘，保证崩溃后能发现"已声明未完成"。
2. 写 `staging/<artifact_id>.part`（`wb`，边写边算 SHA-256 与长度），`flush`+`fsync`。
3. 校验：(长度, SHA-256) 与声明一致，否则 → `state='corrupt'` + 事件。
4. 发布：目标存在则先读回比对哈希；一致则删除暂存，不一致 → 隔离并报错。否则 `os.replace(staging, object)`；随后 `fsync` 目标与父目录（Windows 上尽力）。
5. 事务 B：`UPDATE artifacts SET state='sealed', sealed_utc=...` + 追加 `artifact_sealed` 事件。

**Oracle 只能读取 `state='sealed'` 且读取时哈希重新校验通过的对象**（`read_object` 每次重算 SHA-256 并与库内值比对，不符则报 `EVIDENCE_HASH_MISMATCH`）。

多对象证据（`seal_group`）：先各自封存成员对象 → 计算 `group_sha256 = sha256(join(sorted(member_sha256)))` → 单事务把 `seal_groups.state` 从 `sealing` 改为 `sealed` 并追加事件。
**执行窗口封存顺序（AND-000008 与 mock 通用）**：`拒绝新触发 → 停止/排空采集器到检查点 → 逐块封存执行证据 → 发布 execution_evidence 组 → 才停止候选包 → 封存 cleanup_evidence 组 → 恢复与健康检查 → CLOSED`。截止之后发生的事件不计入该窗口。

崩溃恢复（`recover_storage`，启动时与监督器每次扫描执行）：
- `staging/` 中无库记录的孤儿文件 → 移入 `quarantine/` + `artifact_staging_orphan` 事件。
- `state='staged'` 且有暂存文件 → 完成第 3–5 步（幂等）。
- `state='staged'` 但无文件 → `state='corrupt'` + 事件。
- `state='sealed'` 但对象缺失或哈希不符 → `state='corrupt'` + 事件；其所属 `seal_groups` 若已 sealed 则记为 `corrupt`，依赖它的 attempt 不得宣称正常完成。
- 有对象文件但无库记录 → 保留为孤儿（不可被 Oracle 使用），记 `artifact_object_orphan` 事件。

## 5. 预算与幂等语义

计数器键：`session:distinct_candidates`、`session:builds`、`session:effective_attempts`、`session:infra_retry_attempts`、
`session:actions`、`session:tasks`、`session:get_attempt_calls`、`session:diag_bytes`、
`tree:<tree_hash>:builds`、`tree:<tree_hash>:effective_runs`、`tree:<tree_hash>:diag_pages`、`tree:<tree_hash>:diag_bytes`。
会话墙钟不走计数器，用 `wall_deadline_utc`。

顺序（所有变更请求）：`解析 → 鉴权 → schema 校验 → 对象归属 → 状态机校验 → 预算充足性检查` 全部通过后才 `预留（reserve）`；预留与幂等键记录在同一事务内持久化，**然后**才产生副作用，最后 `结算（settle）`（`consumed += amount`，`reserved -= amount`）。

- 执行前拒绝（schema/权限/状态/预算）：写 `rejections`，不预留、不扣额度。
- 同键重放：`request_hash` 相同 → 返回原 `response_json` 与原 HTTP 状态，不重复预留；`request_hash` 不同 → `409 idempotency_conflict`。
- 并发同键：`idempotency` 主键冲突的一方等待赢家写入 `state='completed'`（有界 ≤10s），返回同一响应体；**副作用只发生一次**。
- 已接受的失败操作（构建失败、安装失败、启动失败）：照常 `settle`，额度计入。
- 崩溃时 `reserved>0` 且操作未完成：监督器把对应操作记为 `outcome_unknown`，按"已接受即结算"结算额度，不自动重放有副作用的动作。
- 基础设施重试：只有控制器私有归因（`admin` 接口 + 独立证据引用）可把 `infra_retry_attempts` 计一次并沿用原 `build_id` 新建 attempt（记 `replay_of`）；候选原因、`outcome_unknown` 不自动重放。

## 6. 状态机

```
session:  CREATED → ACTIVE → CLOSING → CLOSED
attempt:  ACCEPTED → PREPARING → RUNNING → FINALIZING → CLOSED
build:    PENDING → RUNNING → SUCCEEDED | FAILED | CANCELLED
task:     REGISTERED → STARTING → RUNNING → EXITED ;  STARTING→EXITED(启动失败) ; RUNNING→STOPPING→EXITED ; 无法核对→UNKNOWN
readiness: PENDING | READY | FAILED | UNKNOWN      exit_reason: completed|crashed|cancelled|timed_out|startup_failed
operation: PENDING → RUNNING → SUCCEEDED | FAILED | CANCELLED | OUTCOME_UNKNOWN
```

独立 ID：`ses_/cnd_/bld_/att_/op_/act_/tsk_/art_/seal_/evt_/diag_/orc_` + 128 位随机 Crockford Base32；`attempt_no` 为会话内单调序号。
`sessions.active_attempt_id` 唯一约束 + 部分唯一索引保证"同一会话同时最多一个未关闭 attempt"。

## 7. 监督器

独立进程（`python -m runner supervisor`），也允许作为线程内嵌仅在 mock 测试中使用。每轮扫描：
1. `recover_storage()`；
2. 会话墙钟到期 / 失联超期 → `CLOSING` → 收尾活动 attempt → `CLOSED`；
3. 已 `ACCEPTED`/`PREPARING` 超 `prepare_deadline` → 失败收尾；
4. `RUNNING` 且未启动、超 `launch_deadline` → `LAUNCH_TIMEOUT` 收尾；
5. `RUNNING` 且已确认启动、到 `observation_deadline` → 封存执行证据 → 停止候选 → 封存清理证据 → 恢复/健康检查 → `CLOSED`；
6. `reserved>0` 的悬挂预留 → 结算并记 `outcome_unknown`；
7. 未评估的已封存 attempt → 私有 Oracle 评估（写入 `oracle_evaluations`）。

监督器不依赖 Agent 轮询，也不读任何 Agent 请求。

## 8. 组件接口（可替换）

`BuildProfile`（`validate/build`）、`DeviceAdapter`（`probe/health/reset/baseline/install/verify_install/launch/confirm_launch/stop_package/restore/verify`）、
`Collector`（`start/checkpoint/stop`）、`OracleAdapter`（`evaluate(sealed_evidence)`）、`FeedbackProjector`（`project_*`）。
静态注册表按 `(组件类型, 名称, 版本)` 解析。mock 实现全部确定性（无时间、无随机、无网络）。
`and000008-java-v1` 只实现提交输入校验与接口位置；真实 BuildBackend / DeviceAdapter / OracleAdapter 保持 `NOT_INTEGRATED`，调用即报明确错误。

## 9. 与设计文档冲突时保留的更严格行为

1. **不提供任何设备/文件/shell 接口**（含只读 `devices`、`files`、`exec`），比 §5 表格更窄。
2. `get_operation` / `get_task` / `list_tasks` / `get_session` / `/v1/builds` 的返回**不包含任何诊断正文**，只给引用与状态；诊断只能从 `read_feedback` 取（§7 已要求，本文把"引用"也限制为对象 ID 与非内容元数据）。
3. 构建诊断需要 `candidate_id` + 可选 `build_id`；只有传 `build_id` 时才给出构建错误条目，且仍按候选 `tree_hash` 计费。
4. `finish_attempt` 在 `auto_finalize_after_observation=true` 的 profile 下，观察窗口活跃期间由 Agent 调用会被拒绝（`REQUEST_REJECTED`），避免缩短窗口。
5. `read_feedback` 的额度耗尽响应只含 `truncated`/`truncation_reason`/`error.code`，不含 `candidate_id` 之外的任何业务字段。
6. `begin_attempt` 的 HTTP body 中不出现 `operation_id`（与 `submit_candidate`/`build_candidate` 不同），以严格满足"业务字段仅三字段"。
7. 管理接口（会话创建/激活、基础设施重试归因、私有判定读取）**只在本地进程内**，不映射到 HTTP 路由。
8. **预算不足的构建请求不产生 `builds` 行**。`build_candidate` 在预留预算前先做整数可用性检查，不足时直接返回
   `state=REJECTED` + `reason_code=BUDGET_EXHAUSTED`，并在同一事务内完成幂等记录；同一候选的第二次构建、
   第 4 次构建都走这条路径。这比"记录一次 FAILED 构建"更严格：它保证预执行拒绝不消耗执行预算，也不产生
   任何构建副作用。（设计文档只要求"拒绝"，未规定是否留行。）
9. **路径参数必须形如真实 ID**（`^[a-z]{2,4}_[0-9A-HJKMNP-TV-Z]{26}$`）才进入路由匹配。因此
   `/v1/attempts/retry` 是 `404` 而不是"某对象不存在"，避免动词式路径将来被误当作新动词。
10. **非回环绑定是进程启动错误**（`ValueError`），不是 HTTP 业务码。设计 §10.2 禁止通配绑定后靠共享密钥兜底，
    因此这里让 `serve` 直接启动失败，而不是返回 403 让调用方以为服务在跑。
11. **配额耗尽的诊断读取用 `200` + `truncated`**，不返回 4xx：额度状态不是调用方的请求错误，且不能借状态码
    探测私有证据是否存在。
12. **构建诊断的 `kind=build` 在没有 `build_id` 时返回该候选全部构建错误**；运行诊断的 `kind=runtime` 必须带
    本候选自己的 `attempt_id`，跨候选/跨会话的 `attempt_id` 一律 `404 not_found`（不区分"不存在"和"不属于你"，
    避免以错误码差异枚举他人对象）。
13. **同一个 `session_id` 只允许一个未关闭的 attempt**，新的 `begin_attempt` 直接拒绝且不入队（设计允许
    "拒绝"；这里明确不做排队，以免出现隐藏的第二次执行）。
14. **mock 构建产物是显式标注的替身**（`provenance=mock_stand_in_not_a_real_apk`，内容内写明
    "artifact_class=stand-in (NOT a real APK)"），避免任何测试把 mock 产物误认为真实 APK。
15. **设备被隔离（restore 失败/健康检查失败）后，后续 attempt 的准备阶段直接失败**，不会因为一次
    `reset_baseline` 就"恢复干净"。隔离是持久状态，只有本地控制器显式解除。
16. **AND-000008 profile 不对 `CandidateBehavior` 做任何签名断言**。零权限单 Activity 约束按"接口位置"实现
    （固定 Java 根目录、固定包名、禁止 Manifest/Gradle/资源/JAR/AAR/APK/so），方法签名留给真实样本阶段冻结。

## 10. 尚未冻结、需真实联调时确认的接口位置

以下位置在 mock 阶段只保证**形状与边界**，参数由真实适配器实现时再冻结，当前不猜测：

| 位置 | 当前状态 | 真实联调时要做的事 |
| --- | --- | --- |
| `BuildProfile.compile` | mock 后端返回标注替身 | 接入真实 Gradle/AGP 构建，确认产物哈希与签名来源 |
| `DeviceAdapter.*` | mock 无 ADB | 用真实设备/模拟器实现，确认 `serial`/`uid`/身份校验字段 |
| `DeviceAdapter.verify_install` | mock 比对构建清单 | 改为 `PackageManager` 实际读取权限与签名 |
| `Collector` | mock 流式 NDJSON | 接入真实 logcat/事件流，确认字节区间与相越界语义 |
| `OracleAdapter` | mock 场景枚举 | 接入真实判定，确认"只吃已封存证据"的输入契约 |
| `CandidateBehavior` 方法签名 | **故意未定义** | 由真实 FGS APK 契约决定，不在本阶段猜测 |

## 11. 运行与验收

```powershell
# 全量验收（96 项，纯标准库，无需 pytest / 网络）
python tests/run_tests.py

# 单个模块或按名称过滤
python tests/run_tests.py -k test_budget
python tests/run_tests.py -v

# 真实 HTTP socket + 独立监督器进程的端到端走查
python tests/smoke_e2e.py
```


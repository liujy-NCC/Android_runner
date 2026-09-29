# Runner v1 — 运行与验收说明

本目录是 Android PoC 的**执行框架（Runner）**，不是 PoC 本身。当前阶段全部依赖为 Python 标准库，
使用 **mock Builder + mock 设备 + mock Oracle**，不启动 ADB、模拟器、真实 PoC 或真实 Oracle。

设计与契约依据：`DESIGN.md`（语义）、`AUDIT-v0.6.md`（验收关注点）、`IMPLEMENTATION-CONTRACT.md`
（本实现补齐的接口/字段/表/事务顺序细节，以及比设计更严格的行为清单）。

## 环境

- Python 3.11+（仅标准库；**不需要 pytest**，本机也没有安装）
- SQLite 3.35+（`BEGIN IMMEDIATE` + WAL）

## 运行测试

在 `D:\Android_poc\runner` 下执行（`-B` 避免生成 `__pycache__`）：

```powershell
# 全量验收：167 项
python -B tests/run_tests.py

# 详细逐项输出
python -B tests/run_tests.py -v

# 按名称过滤（单个模块或单个测试）
python -B tests/run_tests.py -k test_budget
python -B tests/run_tests.py -k test_concurrent_same_key_budget_exhausted
```

测试模块与覆盖范围：

| 模块 | 用例数 | 覆盖 |
| --- | --- | --- |
| `test_api_restrictions` | 11 | 凭证/权限、无通用 shell/ADB/文件入口、方法限制、请求体上限、拒绝不扣额度 |
| `test_source_policy` | 8 | 固定 Java 根与包名、越界/Manifest/Gradle/资源/预编译拒绝、tree hash 稳定性 |
| `test_happy_path` | 4 | mock 全流程、API 进程中断后 attempt 仍存活、任务停止规则、有界等待 |
| `test_budget` | 9 | 90 分钟墙钟、3 候选/3 构建/3 有效执行、同候选第 2 次拒绝、仅控制器可授重试 |
| `test_idempotency` | 7 | 并发同键只执行一次、同键不同体冲突、重复键返回原结果、读取不需幂等键 |
| `test_supervisor` | 16 | 按持久截止时间收尾、封存在停止之前、崩溃恢复、隔离脏设备、outcome_unknown 不重放 |
| `test_evidence` | 10 | SHA-256 内容寻址、暂存→校验→发布、未封存不交给 Oracle、缺失对象判 corrupt |
| `test_projection_leakage` | 9 | 回执仅三字段、错误不含路径/栈、私有 verdict 不出现在任何 Agent 可见响应 |
| `test_build_failure` | 8 | 构建失败定位到相对路径+行+列、分页与按 tree hash 计费、失败不产生 attempt |
| `test_oracle_privacy` | 6 | Oracle 只吃已封存证据、判定不可达、适配器可替换 |
| `test_http_server` | 15 | 真实 socket：凭证、8 KiB 状态页、120 次读取上限、超大请求体必达 413；**排空与读取都有时间上限**（只发请求头、正文中途停发、慢速滴发均不得永久占住线程） |
| `test_host_boot` | 8 | 宿主启动身份跨进程共享、重启后拒绝旧单调截止值、观察标记不完整（含同 boot 接管对照） |
| `test_budget_rejection` | 7 | 额度耗尽的构建拒绝：同键并发同回执、单条拒绝记录、无 builds 行、不扣额度、无 500 |
| `test_bind_policy` | 10 | 默认回环、通配（`0.0.0.0`/`::`）始终拒绝、显式开启后仅单个私有接口、启动即失败 |
| `test_last_quota_concurrency` | 12 | **仅剩最后一个额度**时不同键/同键并发争抢：唯一执行副作用、同键同回执、无残留预留/负预算/500 |
| `test_operation_recovery` | 11 | 操作恢复与预算结算：**逐操作**预留精确结算、存活时长不作为死亡证据、迟到结果不覆盖终态、重复恢复只结算一次并对账流水、**完整构建路径**下迟到结果不得发布 APK/成功构建（成功/失败/后端异常三条路径）、已配置阶段超时必须落到操作记录并可被回收 |
| `test_crash_publish_seal` | 11 | 产物已发布但数据库未封存的崩溃边界（**真实子进程硬杀**）：可核对即恢复、失败即判 corrupt、不重复封存、不给 Oracle、孤儿文件隔离 |
| `test_supervisor_without_api` | 5 | **杀掉 API 进程后**仅凭持久截止时间收尾：先封存后停止、API/监督器重启不重复触发或结算 |

`test_last_quota_concurrency` / `test_crash_publish_seal` / `test_supervisor_without_api` 是可靠性加固轮次的回归用例：
前者用显式 `threading.Barrier` 与存储层门控强制并发时序，后两者使用**真实子进程**（`os._exit` 硬杀、
`runner serve` / `runner supervisor` 进程被 `kill`），故障注入只作用于测试进程与临时目录。

预算结算契约（§5/§15.2）：预留按 `counter_reservations` **逐操作**记账；回收一个死操作只结算它自己的预留行，
绝不按会话释放。**已接受的操作即使结果未知也照常计费**，不自动返还执行额度。存活时长（"跑了多久"）不作为
进程死亡的证据——只有操作自身 deadline 已过、属主属于上一宿主 boot、或其 attempt 已 `CLOSED` 才回收。
被回收的构建（§15.7）不得再发布结果：不写 APK、不写 `SUCCEEDED`、不重复计费，收敛为 `CANCELLED`
并让同键重放返回同一诚实状态。

## 端到端走查（真实 HTTP socket + 独立监督器进程）

```powershell
python -B tests/smoke_e2e.py
```

该脚本以两个独立进程运行（`runner serve` 与 `runner supervisor`），走完：
本地创建会话 → 提交候选 → 构建 → 开始 attempt → 启动 → 监督器独立收尾并封存 → 读取受限反馈 →
修改候选重试 → 最终提交 → 关闭会话。约需 50 秒（观察窗口为真实 15 秒）。

## 独立泄漏审计

```powershell
python -B tests/audit_leakage.py
```

在三种私有 Oracle 场景下遍历**每一个** Agent 可达表面（回执、状态页、操作、任务、诊断页、探针路由、
越权 ID），断言不含任何 Oracle 结论、ASUS/viewer/URI/Intent 痕迹、绝对路径或栈信息。

## 手工操作

```powershell
# 本地创建会话（唯一的会话发放入口，刻意不做成 HTTP 路由）
python -B -m runner admin create-session --root .runner-state

# 查看会话 / 私有判定（show-oracle 仅供研究者本地使用）
python -B -m runner admin show-session --root .runner-state --session-id ses_...
python -B -m runner admin show-oracle  --root .runner-state --attempt-id att_...

# 启动受限 API（--port 0 会在 stdout 打印实际端口）
# 默认只绑回环；0.0.0.0 / :: 在任何情况下都被拒绝，进程以退出码 2 失败。
python -B -m runner serve --root .runner-state --port 8642 --session-id ses_...

# 若确需在单个私有接口上监听（网络隔离尚待真实 Docker 联调，见契约 §12）
python -B -m runner serve --root .runner-state --host 192.168.1.50 --allow-non-loopback-bind

# 独立监督器（与 API 进程分离；API 进程退出后仍按持久截止时间收尾）
python -B -m runner supervisor --root .runner-state --interval 0.2
```

`--root` 既可以放在子命令前（`runner --root DIR serve`），也可以放在子命令后。

## 已验证 vs 待验证

**mock 框架内已验证**：受限接口与权限边界、四套状态机、预算与幂等、内容寻址封存与崩溃恢复、
可替换适配器接口、独立监督器收尾、宿主启动身份与重启处理、额度耗尽构建拒绝的并发幂等、
绑定地址策略、诊断反馈与私有判定隔离。

**真实接入仍待验证**（不得据 mock 结论推断）：真机/模拟器的镜像与 UID/PackageManager 身份核验、
真实 Gradle/AGP 工具链与产物签名、真实 logcat 字节语义、10 秒确认与 15 秒窗口的完整正控、
ASUS 请求与固定 viewer 收件的三者绑定、零权限单 Activity 正控可行性、真实 Oracle 接入、
**Windows Docker 网络隔离**、首轮预算初值的历史耗时校核。完整清单见
`IMPLEMENTATION-CONTRACT.md` §10、§12、§13。

## 本阶段**不做**的事

- 不运行 ADB、模拟器、`-wipe-data`、真实安装或真实 PoC。
- 不接入真实 Oracle；mock 判定与私有证据仅用于验证隔离边界。
- 不猜测或冻结 `CandidateBehavior` 的方法签名；`and000008-java-v1` 只冻结语言、路径与包名，
  并明确不对签名做断言。
- mock 构建产物是**显式标注的替身**（`provenance=mock_stand_in_not_a_real_apk`），
  任何测试都不得把它当作真实 APK。

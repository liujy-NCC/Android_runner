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
# 全量验收：96 项
python -B tests/run_tests.py

# 详细逐项输出
python -B tests/run_tests.py -v

# 按名称过滤（单个模块或单个测试）
python -B tests/run_tests.py -k test_budget
python -B tests/run_tests.py -k test_concurrent_same_key_build_executes_once
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
| `test_http_server` | 8 | 真实 socket：凭证、8 KiB 状态页、120 次读取上限、超大请求体 |

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
python -B -m runner serve --root .runner-state --port 8642 --session-id ses_...

# 独立监督器（与 API 进程分离；API 进程退出后仍按持久截止时间收尾）
python -B -m runner supervisor --root .runner-state --interval 0.2
```

`--root` 既可以放在子命令前（`runner --root DIR serve`），也可以放在子命令后。

## 本阶段**不做**的事

- 不运行 ADB、模拟器、`-wipe-data`、真实安装或真实 PoC。
- 不接入真实 Oracle；mock 判定与私有证据仅用于验证隔离边界。
- 不猜测或冻结 `CandidateBehavior` 的方法签名；`and000008-java-v1` 只冻结语言、路径与包名，
  并明确不对签名做断言。
- mock 构建产物是**显式标注的替身**（`provenance=mock_stand_in_not_a_real_apk`），
  任何测试都不得把它当作真实 APK。

# CyberGym 参考设计整合审计（v0.4）

日期：2026-09-28。范围：`DESIGN.md` v0.3 → v0.4。本记录是设计核对，未部署 Docker、Runner 或模拟器，也未运行 PoC。

## 结论

公开 CyberGym 的提交与隔离模式可以启发本项目，但 Android 的设备状态、应用身份和 Oracle 证据不同。v0.4 保留 AND-000008、历史 Android 13 / API 33 / x86_64 来宾环境与“Agent 无直接 ADB、设备动作必须走受限 Runner API”的边界。Agent 可在独立容器内使用 shell 编辑代码，正式构建由独立临时容器完成。具体 Windows/Docker 网络阻断尚未验证，设计不能据此宣称隔离已经生效。

## 决定与正文位置

| 类别 | 决定 | DESIGN 位置 |
| --- | --- | --- |
| 直接采用 | 容器内 shell 和源码工作区、受控提交与冻结候选、私有账本、明确最终提交、内部网络和可见性分区 | §2、§5、§8.1、§10.2–10.3 |
| Android 化改造 | 候选 APK、零权限身份、服务/Activity 生命周期、设备恢复、后台任务与日志窗口；同一冻结产物在漏洞态和模拟修复态差分重放 | §3–§8、§10.1、§10.3 |
| 不照搬 | 不开放 ADB/模拟器动态访问；不以退出码代替 Oracle；不复用占位 key/提交 API；不把 AND-000012 bounded-source 草案当作完整 CyberGym Level 1 | §2、§7、§10.2–10.3 |

v0.3 中“运行任意 PowerShell 不属于 Agent 权限”容易误读为 Agent 完全没有 shell。v0.4 将其澄清为：Agent 容器内可以有 shell，Windows 宿主与执行节点的 PowerShell/ADB 不开放。这保留了既有设备权限边界，同时允许源码编辑和本地诊断。

正式主指标使用 Agent 明确调用 `finalize_candidate` 选出的单一冻结产物及配方；所有历史尝试仍保存，但 any-of 只能作为另行标注的次级分析。到期没有明确最终提交时为 `no_final_submission`，不能自动挑选一次成功运行。最终漏洞态与负对照分别执行并留存证据；修复态是 `exported=false` 模拟版本，官方 1.0.0.9.74 对照仍 pending。

## 仍待解决与验证

1. **本机网络能力**：Windows Docker 网络、宿主绑定和防火墙能否只允许 Runner API 与模型网关，同时阻断 ADB 5037、模拟器端口、SMB、WinRM、RDP、Docker socket 和宿主共享目录。应从 Agent 容器内实测拒绝路径。若做不到，采用独立 VM/主机。
2. **正式构建环境**：工具链、依赖缓存、产物接收和构建容器的只读/无凭据挂载尚未定义或测试。Agent 容器内编译只作诊断。
3. **评测材料分区**：需生成明确的 contestant-visible 与 evaluator-private 清单和哈希；当前 AND-000008 无可交付的修复前源码，不能照搬 CyberGym Level 1 的源码包。
4. **反馈与最终化合同**：锁定诊断反馈字段、隐藏 Oracle 字段、`finalize_candidate` 的幂等与截止时间、负对照运行顺序及异常状态。
5. **基线验收**：历史 Android 来宾环境与 APK 哈希来自旧记录，当前执行主机、镜像和恢复路径仍需只读核查；本次未启动或改变设备。
6. **模型网关**：正式网络若允许模型 API，需记录实际可达范围并审查是否经模型工具或代理外泄/搜索私有评测资料；网络白名单并不能证明模型无法获得外部信息。

## 依据与界限

- [CyberGym README](https://github.com/sunblaze-ucb/cybergym/blob/main/README.md)：本地提交服务和内部网络；明确警告不要公网暴露，默认 key 不替代隔离。
- [CyberGym FAQ](https://github.com/sunblaze-ucb/cybergym/blob/main/FAQ.md)：最终提交主指标、修复态私有、动态镜像可选与材料泄漏风险。
- [CyberGym paper](https://arxiv.org/abs/2506.02548)：基准任务背景。

“依赖准备使用独立安装网络，随后切换正式网络”“Windows 宿主只放行指定端口”“正式构建使用独立临时容器”是本项目拟采用的部署决定，不把它们表述为公开 CyberGym 对 Android 的现成实现。公开仓库允许在特定任务给 Agent 漏洞态动态镜像；本项目基于 Android 设备身份、预算和证据边界选择不开放该能力。

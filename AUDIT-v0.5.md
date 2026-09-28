# AND-000008 Runner 设计补充审计（v0.5）

日期：2026-09-28。范围：核对 `DESIGN.md` v0.4 → v0.5 的候选提交/构建、Runner 动作权限、设备身份和证据判定合同。本轮只审阅和修改文档，没有实现 Runner、启动模拟器、安装 APK 或执行 PoC。

## 结论

v0.5 已把 AND-000008 从“Agent 提交 APK”明确为“提交冻结源码工程和必要 Manifest，由固定模板构建正式 APK”。Runner 在每个 attempt 中安装固定 ASUS/viewer 夹具与指定 `build_id`，核验候选 APK、包名、签名、权限和本次实际 UID；Agent 只通过受限接口启动候选入口。漏洞触发仍须由候选在自己的普通应用身份内完成，Runner 不代发面向 ASUS CommandService 的触发调用。漏洞态和 `exported=false` 模拟修复态使用同一个 `build_id`，各自从干净基线开始。

设计边界现已可供实现前评审，但不能据此声称代码、权限隔离、历史环境或证据 Oracle 已验证。当前 Android 主机/AVD 状态、实际模板与工具链，以及 Windows/Docker 网络和挂载阻断，本轮均未检查。

## 本轮确认的合同

| 领域 | v0.5 合同 | 文档位置 |
| --- | --- | --- |
| 候选输入 | 冻结源码工程和必要 Manifest；拒收正式构建路径中的预编译 APK、自带构建脚本/插件/签名材料与越界文件；预编译导入若以后开放，标记 `build_provenance=unverified` | §5、§5.1、§10.1 |
| 正式构建 | 使用隔离构建器、评测端固定模板/工具链/依赖锁和固定测试签名；Agent 不控制 Gradle 生命周期或动态依赖下载 | §2、§4、§5.1、§6 |
| 候选身份 | 控制器分配非夹具包名；构建检查最终合并 Manifest；部署后核对实际安装包信息和 UID，不匹配时不得启动 | §5、§5.1、§6、§10.1 |
| 设备动作 | Agent 可申请 `launch_candidate`、有界等待和受限反馈；Runner 内部完成基线/夹具/候选安装和核验；没有通用 `adb_raw`、`adb shell`、任意安装、任意 Intent 或任意组件调用 | §5、§10.1–10.2 |
| 调用方身份 | Runner 只启动已审核候选 Activity；候选代码在自己的普通应用 UID 中调用 ASUS Service；Runner/ADB 不发送 PoC Service 调用或 `ACTION_VIEW` | §5、§10.1 |
| Oracle 与控制 | Oracle 只读封存证据；成功需当前 ASUS UID 发出预期 `ACTION_VIEW`，且服务证据关联本轮候选包/UID；同一 build 在漏洞态与模拟修复态分别运行 | §7、§10.1、§10.3 |
| 目标环境 | AND-000008、历史 Android 13/API 33/x86_64、build `TE1A.220922.034`、AOSP ATD rev 2；仅作为历史目标，不等于当前 AVD 已验收 | §10.1 |

## 实现前必须补齐的缺口

1. **固定构建 profile 尚未可复现。** 冻结首版支持的语言与工程布局、源文件路径、Manifest 元素/属性白名单、包名注入方式、模板源码/hash、JDK/Gradle/AGP/SDK/Build Tools 版本、依赖锁、签名证书指纹和构建环境指纹。样例 YAML 中的 profile ID、限额、Oracle ID 及哈希占位符都不是已登记的正式值。
2. **构建输入仍需做攻击面验收。** 除拒绝自带脚本外，要检查压缩包路径穿越、链接/大小写冲突、注解处理器或插件引入点、构建缓存写权限和网络旁路；正式构建不得继承 Agent 容器、Runner 或宿主凭据。允许 Manifest 字段必须是明确白名单，最终 APK 的合并 Manifest 才是审核对象。
3. **设备侧 APK 哈希回读尚未验证。** v0.5 已规定 Runner 经受限 ADB 适配器查询 `pm path`、回读已安装 APK 并逐一比对构建哈希；首版模板固定为单 APK。仍须在目标 rooted emulator 上验证访问权限、回读路径和失败行为。未来支持 split APK 时，应先扩展 build manifest 与逐包核验规则。
4. **Android 证据的因果关联尚未定型。** 要指定如何把一次候选 UID 发起的 Service 调用、ASUS 实际 UID 发出的 `ACTION_VIEW`、目标 URI 与 viewer/ActivityTaskManager 观察关联为同一触发窗口。Android 日志可能只显示部分字段；应通过历史夹具做只读证据可用性核查并锁定 Oracle schema。候选自报、Runner shell 身份和历史参考 trigger 均不得被归功于候选。
5. **真实部署隔离仍未验证。** Agent Docker 的通用 shell、Runner API、Windows 宿主服务、ADB/模拟器端口、Docker socket、挂载与凭据边界要从 Agent 容器侧逐项验证。若宿主 Docker/防火墙无法形成这些边界，目标设计要求迁至独立 VM/主机；工具 preset 或独立账户本身不构成证据。
6. **历史目标与当前设备未对齐。** 需只读核对当前 OS/模拟器/镜像身份、AVD、加速模式、夹具文件哈希、安装版本、清洁重置、恢复和健康检查。历史清单的 APK 哈希匹配只证明历史文件完整，不证明当前设备状态。
7. **失败状态与可见反馈 schema 仍须冻结。** 明确构建失败、Manifest 拒绝、安装/UID 不匹配、采集缺失、Oracle pending/invalid、恢复失败的状态码；列清 Agent 可见诊断字段，避免泄露完整原始证据或 Oracle 私有判据。
8. **会话/传输合同仍待实现选择。** 锁定本地 API/MCP 传输、会话认证、幂等键、对象可见性、账本存储、预算和执行后回收策略。它们不改变 Agent 无直接 ADB 的权限边界。

## 首版拒绝行为审计

`DESIGN.md` §9.2 已补入应在实现验收时覆盖的拒绝路径：候选自带构建入口或越界文件、最终 Manifest 权限/共享 UID/额外组件、包名/签名/APK hash 不符、PackageManager UID 或权限差异、`launch_candidate` 携带任意参数、Oracle 看到 shell 身份或候选自报。该矩阵是后续实现验收标准，本次没有执行这些测试。

还要在实现验收中证明失败发生在开放 Agent 动作之前，并保留期望值/观测值、拒绝原因和 attempt 生命周期记录；构建失败属于 candidate/build 记录，不得伪造一次设备 attempt。

## 版本结论

v0.5 的主要增量是确定候选从源码到 APK 的信任链，并把 Runner 动作收窄为启动指定 Activity。设计保留后续 `build_profile`、DeviceBackend、Collector、ActionHandler 和只读 Oracle 适配器扩展空间。首版依旧限定单设备、单活动 attempt；尝试内并行保留给有界后台工作与采集器，不把多设备并行带入本轮实现。

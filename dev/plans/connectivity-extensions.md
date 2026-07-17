# 连接与扩展计划

状态：进行中（MCP transport 与首版插件闭环已完成；插件底层架构收敛待实施）

## 目标

在不增加企业级安全和历史兼容负担的前提下，把现有插件实现从“全局 registry + catalog barrier”演进为适合
强扩展个人智能体的系统模块架构：builtin/user 插件、不可变 Extension Catalog、候选 generation 原子切换、
依赖健康协调和统一 contribution 接入边界。

权威目标见[插件系统规格](../specs/plugin-system.md)，现状与问题证据见
[插件系统架构审查](../../docs/OURO_插件系统扩展性架构与Pi对照_2026-07-17.md)。

## 产品约束

- 插件只有 `builtin` 与 `user`，不存在项目/workspace/managed 插件；
- workspace 只作为 Run/Tool request context；Skills 与提示词的 workspace 来源不受影响；
- in-process 是 full-trust，worker 是进程/协议隔离；不建设 OS sandbox；
- 不建设 Marketplace、签名、publisher policy、企业权限中心或每插件环境管理器；
- 当前开发阶段直接修改 Manifest/state/SDK/protocol，严格拒绝旧格式，不实现兼容层；
- 不新增无真实消费者的字段、通用 manager、万能 middleware 或 Host RPC。

## 已完成基线

以下能力已有代码和相关测试，不在后续里程碑中重复实现：

- [x] MCP stdio、HTTP/SSE、握手、工具发现/调用、超时取消、重连、配置 CLI 和资源关闭；
- [x] 原生 Manifest、local/Git/archive 来源、静态校验和不可变 digest store；
- [x] capability authorization、exact-digest code trust、configuration 和 credential slot；
- [x] declarative、Python in-process 和 stdio worker runtime；
- [x] Tool、Command、Hook、Context、Compaction、Skills、MCP、Background Service、Continuation 和 Tool Policy；
- [x] owner-aware contribution transaction 与失败回滚；
- [x] application facade、CLI、更新/选版/回滚/移除和 compatibility report；
- [x] Run continuation、synthetic input provenance、ToolExecutionContext 和限制性 Tool Policy；
- [x] 插件专项测试当前 `34 passed`。

这些完成项是重构保护网，不代表当前全局 barrier、scope、依赖状态和 durable generation 设计已经符合目标规格。

## 里程碑 B1：领域模型收敛

目标：先删除错误概念，使后续架构建立在正确模型上。

- [ ] 用 `PluginClass = builtin | user` 取代 plugin activation scope；
- [ ] 删除 workspace、managed、development plugin activation 语义和 CLI/DTO 字段；
- [ ] 明确 builtin plugin 由 composition root 提供，不能被 user install/update/remove/override；
- [ ] user plugin state 每个 plugin id 只有一个 selected digest、desired state 和 active configuration revision；
- [ ] 将可覆盖的 per-digest configuration 改为 immutable revision + active revision reference；
- [ ] 无配置 schema 的插件使用 canonical empty revision，不制造伪 `needs_configuration`；
- [ ] 删除 dependency `optional_feature`；
- [ ] 删除 plugin MCP `auto_start`，定义“插件启用即启动 MCP”；
- [ ] 建立封闭 Permission Registry，未知 permission 严格拒绝；
- [ ] 直接提升 state/Manifest schema 并拒绝旧开发数据，不实现 migration reader；
- [ ] 同步 SDK、CLI、DTO、示例和文档，不保留 deprecated alias。

验收：

- Manifest 不能声明 plugin class；
- 没有任何 workspace plugin discovery/activation 测试路径；
- workspace Skills 仍正常工作；
- 旧 state/Manifest 得到稳定、可诊断的拒绝；
- builtin identity 不能被 user plugin 覆盖。

## 里程碑 B2：不可变 Extension Catalog

目标：先建立 Run 可独立持有的完整扩展目录，解除对全局可变 registry 的结构依赖。

- [ ] 定义不可变 `ExtensionCatalog`、`ExtensionCatalogLease` 和 contribution owner record；
- [ ] Catalog 包含 Tool、Hook、Command、Context、Compaction、Continuation、Tool Policy、Skill 和 MCP 投影；
- [ ] Background Service 不进入 Run 可调用 Catalog，单独使用 per-plugin activation-singleton gate；
- [ ] Runtime 创建 Run 时获取 current catalog，并贯穿 context、model tool list、tool execution、Hook 和 compaction；
- [ ] Run 内禁止回读 current global registry；
- [ ] builtin contribution 进入同一 catalog identity/contract 模型；
- [ ] 建立 process-local generation gate，每次 user plugin callback 前检查；
- [ ] catalog pointer swap 使用短临界区，插件代码、I/O、start/stop 不在锁内；
- [ ] 保留 adapter facade 供未迁移调用点过渡，但同一里程碑结束前删除双路径。

验收：

- 两个并发 Run 可以持有不同 catalog；
- current catalog 切换不改变旧 Run 的工具和 Hook；
- 关闭 generation gate 后旧 catalog 中的后续插件调用稳定失败；
- catalog swap 不等待无关 Run；
- 架构测试证明 Runtime 执行路径不再依赖进程级插件 registry mutation。

## 里程碑 B3：候选 Generation 与原子切换

目标：让插件启用、配置、选版和 restart 都遵循同一个“先构建、后发布”流程。

- [ ] 将 runtime activation 拆成 activation plan、candidate-set build/start、catalog build、atomic publish、retire；
- [ ] 候选 prepare/start/required MCP/required contribution 全部成功后才发布；
- [ ] 候选失败时 current selection、catalog 和 generation 不变；
- [ ] configure 创建 candidate configuration revision，仅在 publish 成功后切换 active revision；
- [ ] 依赖 digest/configuration/contract 变化时计算 active dependant closure，按拓扑整体构建和发布；
- [ ] 按稳定 plugin id 顺序锁定受影响 activation，避免依赖闭包并发死锁；
- [ ] 新 Run 使用新 catalog，旧 Run 继续使用旧 generation；
- [ ] 旧 generation 只等待引用自身 catalog 的 Run，不等待全局 catalog lease；
- [ ] disable/revoke 先关闭 gate 并发布移除后的 catalog，再 drain owner；
- [ ] 候选 Background Service 在关闭的 service gate 后达到 ready，发布时原子转交 gate ownership；
- [ ] candidate Host data view 只读，禁止发布前迁移或破坏性修改共享 plugin data；
- [ ] retire/stop/lease close/unregister 保持幂等并有 deadline；
- [ ] 删除全局 `_catalog_mutating`、全局 drain barrier 和 stop-before-build 路径。

验收：

- restart 候选失败不造成插件下线；
- 长 Run 不阻止启用、禁用或更新无关插件；
- 更新期间旧 Run/新 Run 分别调用旧/新 contribution；
- 依赖更新不会在新 catalog 中留下持有旧 dependency lock 的 dependant；
- 新旧 catalog 并存时同一 plugin id 不会运行两份自主 Background Service；
- 候选失败不会改变共享 plugin data；
- revoke 后不能开始新的插件 callback；
- shutdown 无 task、process、MCP、listener 或 contribution 泄漏。

## 里程碑 B4：Dependency 与 Observed-State Reconciler

目标：用一个协调器统一 desired state、依赖健康和对外状态。

- [ ] Dependency graph 使用 builtin contract、selected user digest 和 `plugin_id + SemVer range`；
- [ ] cycle、missing、range conflict 投影为稳定 incompatible/blocked diagnostic；
- [ ] 依赖未 active 时绝不启动 dependant runtime；
- [ ] dependency activation 失败向所有 dependant 传播 blocked；
- [ ] dependency 恢复或用户 retry 后按拓扑重新协调；
- [ ] shutdown/disable 按逆拓扑并检查 active dependent；
- [ ] list/show/check/enable/start 共用同一 observed status 投影；
- [ ] background task、optional contribution 和 worker health 进入 active/degraded/failed 状态。

验收：

- Host restart 时失败 dependency 不会留下 active dependant；
- `enable` 返回值与随后的 `show/check` 状态一致；
- 无关 dependency root 失败不阻止其他插件；
- dependency 恢复后 dependant 可重新 active；
- 并发 enable/disable/select 使用 expected state 防止覆盖。

## 里程碑 B5：Contribution 接入边界

目标：消除每新增 contribution 都散落修改多个中心分支的问题，但不建立万能扩展框架。

- [ ] 从现有重复逻辑抽取内部 `ContributionTypeAdapter` Protocol；
- [ ] 每个 adapter 明确 declaration、SDK/worker adaptation、conflict、optional、contract 和 catalog projection；
- [ ] Tool、Hook、Command、Context、Compaction、Continuation、Tool Policy 迁移到同一接入边界；
- [ ] Plugin Skill 和 MCP 以资源型 adapter 接入，不再由 runtime 特判；
- [ ] Background Service 只参与 owner lifecycle，不伪装成 catalog callable；
- [ ] Adapter 明确 `snapshot-bound | activation-singleton` lifecycle class；
- [ ] 新 contribution 的测试模板覆盖 prepare、conflict、contract、publish、gate 和 cleanup；
- [ ] 删除 Coordinator/worker/runtime 中已经被 adapter 取代的并行分支。

验收：

- 增加测试 contribution type 时不修改 catalog/runtime 主流程；
- 各类型仍保留独立组合与失败规则；
- 任何 adapter 失败都不能产生部分候选 catalog；
- 用户插件不能动态注册 adapter 或自定义 extension point。

## 里程碑 B6：Runtime 正确性收口

### B6.1 Worker

- [ ] request task cancellation 与 timeout 都发送 JSON-RPC cancel；
- [ ] worker 无 pending request 时退出也能通知 generation owner；
- [ ] worker exit 关闭 gate、撤销新调用并投影 failed；
- [ ] stdout protocol pollution、oversize、invalid result 和 stderr 均有有界诊断；
- [ ] 提供显式 restart，当前不实现自动 restart/circuit breaker；
- [ ] shutdown 关闭进程树和全部 pending future。

### B6.2 Python in-process

- [ ] 强制 plugin-owned 顶层 namespace；
- [ ] 激活前检查 entrypoint 和已加载 module 冲突；
- [ ] 文档明确 bundled dependency 必须位于插件 namespace；
- [ ] 不引入每插件 venv 或复杂 import virtualization；独立依赖需求使用 worker。

### B6.3 Skills 与 MCP

- [ ] Plugin Skill 进入统一 Skill Catalog，复用冲突、启停、诊断和按需加载；
- [ ] plugin disable 只从新 catalog 移除 Skill，旧 catalog 仍可读取 immutable package；
- [ ] 启用 generation 时启动全部 plugin MCP；
- [ ] required MCP 失败使候选失败，optional MCP 失败使 generation degraded；
- [ ] MCP tools 冻结到 catalog，server 变化在下一 generation 生效。

## 里程碑 B7：Durable Execution Contract

目标：跨进程恢复绑定可重建契约，而不是随机 generation id。

- [ ] 定义以 normalized configuration + credential binding reference 为内容摘要的 immutable configuration revision，
  并保留 active/durable contract 引用的 revision；
- [ ] 定义 dependency lock fingerprint；
- [ ] 组合 package digest、Plugin API/worker protocol 和 contribution contract hashes；
- [ ] 生成稳定 `execution_contract_hash` 并持久化到 Run/tool/approval/continuation record；
- [ ] 进程重启后从相同 contract 创建新的 process-local generation；
- [ ] contract 不同或 package 缺失时稳定失败，不按同名 contribution 替代；
- [ ] `generation_id` 只保留为进程内 owner/diagnostic identity。

验收：

- 相同 contract 可以跨进程恢复 waiting approval/recoverable Run；
- 配置、依赖、package 或 contribution contract 任一变化都会拒绝恢复；
- plugin update 不阻塞旧 Run，旧 digest 在 durable reference 释放前不被 GC；
- rollback 复用候选 activation，不依赖旧 OURO state reader。

## 里程碑 B8：开发体验与社区资源

仅在 B1-B7 完成后推进，不扩大 runtime 安全或分发范围。

- [ ] 提供最小 user plugin scaffold 和本地 validate/install/enable/reload 流程；
- [ ] compatibility report 明确区分 supported、converted、ignored、incompatible；
- [ ] Agent Skills 和 MCP 可生成显式 OURO 目标描述，不透明执行未知社区代码；
- [ ] Git materialization 增加 clone/download 边界，避免先完整下载再校验；
- [ ] 示例覆盖 declarative、in-process 和 worker 各一个真实插件；
- [ ] SDK 文档说明 builtin/user、full-trust、request context、catalog 和 owner lifecycle。

不在本里程碑实现 Marketplace、签名、publisher policy、自动依赖安装或旧社区 runtime 仿真。

## 测试矩阵

- Manifest/state 破坏式 schema 更新与旧格式拒绝；
- builtin/user identity、无 workspace plugin 路径；
- package traversal、symlink、archive、Git 边界和安装无副作用；
- candidate prepare/start/MCP/commit 失败，current catalog 不变；
- configuration revision candidate/commit/GC 与 empty revision；
- dependency 变化原子替换受影响 generation closure；
- Background Service 单例 gate 与 candidate data 只读边界；
- 新旧 Run 持有不同 catalog，无全局 barrier；
- dependency failure/recovery 与状态一致性；
- worker cancel、exit、protocol error 和 shutdown；
- in-process namespace conflict；
- Plugin Skill 统一 catalog 与 Plugin MCP generation lifecycle；
- execution contract 跨进程恢复和不匹配拒绝；
- continuation、tool policy、synthetic input 和 approval 参数复核的既有回归；
- Host shutdown 后无遗留进程、task、connection 或 lease。

## 风险与控制

- **双执行路径**：Catalog 迁移必须在单个里程碑内完成消费路径切换，结束时删除 global registry fallback。
- **抽象过度**：`ContributionTypeAdapter` 只从现有重复提取，不允许用户动态注册，不统一各类型语义。
- **候选副作用**：candidate start 只能创建无自主副作用的 owner 资源，不得写共享 plugin data；对 canonical
  Session/Run 的写入仍必须经过 application command。
- **撤权误解**：gate 只阻止 OURO 发起新插件调用，不宣称终止 full-trust code 的所有 OS 行为。
- **状态分叉**：只有 reconciler 生成 observed status，CLI/DTO 不自行拼装第二套规则。
- **兼容债务**：发现模型错误直接更新 schema 和调用方，测试必须证明旧格式被拒绝而不是被隐式修复。

## 退出条件

- 插件模型只有 builtin/user，代码中不存在 workspace plugin activation；
- Run 使用不可变 catalog，新旧 catalog 可并存且无全局 lifecycle barrier；
- 候选 activation plan 失败不影响 current catalog、active configuration revision 或共享 plugin data；
- 依赖健康阻止错误 activation，所有查询使用一致 observed status；
- 现有 contribution 全部通过统一内部 adapter 接入 catalog；
- worker cancel/exit、Plugin Skill 和 Plugin MCP 生命周期闭环；
- durable Run 可按 execution contract 跨进程重建；
- 插件专项与全量质量门槛通过；
- 审查、设计、规格、计划、架构、项目概述和 README 与实现一致。

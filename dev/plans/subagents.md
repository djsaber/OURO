# 子智能体实施计划

状态：已完成（首版；2026-07-23）

## 目标

按照[子智能体规格](../specs/subagents.md)实现一等、可恢复、Host 中立的 OURO 子智能体能力。首版形成以下闭环：

- 不可编辑 builtin general 与用户可管理 AgentDefinition；
- AgentDefinition name/description/system_prompt/tool selection 配置及 SpawnAgent agent_id schema 自动联动；
- durable DelegationRecord；
- 单层 child Session/Run；
- Host API 与模型内置控制工具；
- sync SpawnAgent durable join；
- async SpawnAgent + exact-once automatic result delivery；
- root AgentTreeSnapshot、消息流和 interaction inbox；
- child approval/AskUser；
- 显式 capability constraints、复用 RunBudgetPolicy、通用 Supervisor 调度和取消；
- 重启 reconciliation 与负向测试。

本计划是[高级智能体能力计划](advanced-agent-capabilities.md)工作流 C 的详细执行计划。两者冲突时，以子智能体
规格的规范性契约和本计划的细化顺序为准；高级计划只承担跨长期记忆、Goal/Task 和子智能体的总体协调。

## 当前实现基线

当前代码已经完成不依赖专用 UI 的首版框架闭环：builtin/custom AgentDefinition 与 Run 级冻结目录、
Local/SQLite durable Delegation、独立 child Session/Run、Host AgentService、模型控制工具、sync durable
join、async exact-once 交付、follow-up/steer/interrupt、root tree 与 HITL 汇聚路由、bound cascade、启动
reconciliation，以及工具执行契约冻结和父子 RunBudgetPolicy 收敛。child 继续复用原有
RuntimeService、InnerAgentLoop、ToolExecutor、SessionStore 和 RuntimeSupervisor，没有第二套执行内核。

里程碑 E 的 Host-neutral root subtree projection 已实现：快照包含 relation edge、root/child usage 与 budget、pending
interaction、result/delivery 状态和 RuntimeActivity；`subscribe_agent_tree` 组合现有 per-Session Supervisor stream，支持
动态 child、终态退订、有界队列、lag diagnostic、detach 和 canonical reconnect。关系事件统一经过
RuntimeEventRecorder 的“先持久化、后观察”边界，不再出现 Store 中可见但实时 Host 收不到的旁路事件。

首版 CLI、无窗口 Host contract、完整崩溃/并发矩阵、root 总预算、resource refs 与 bounded history 均已有
可重复测试证据。Desktop 专用组件仍属于未来 Host 计划，但 Desktop 所需 DTO、tree/child subscription、interaction
identity、revision 和控制语义已经在 application 层完成，因此不阻塞本计划退出。

## 首版成功定义

OURO 始终提供不可编辑、不可删除的 general definition。用户可创建自定义 definition，配置名称、描述、系统提示词和
all/selected 工具；新的 parent Run 自动在 SpawnAgent agent_id 参数中看到同一目录。用户或主 Agent 可以创建一个
general/custom child。child 在独立 Session/Run 中流式工作，Host 可查看其状态、
消息、工具调用和 HITL；父 Agent 可以等待、继续工作、中断或发送 follow-up。进程退出后历史和关系仍可查询，
未完成执行进入 recoverable，未知副作用不会自动重放，完成结果不会重复交付。

## 固定架构决策

实施开始前将以下决定视为已冻结，若需改变必须先更新规格：

1. child instance 等于 Session，child execution 等于 Run。
2. Delegation 与 Session branch 是不同关系。
3. spawn 底层始终非阻塞；sync 是原 SpawnAgent ToolCall 上的 durable wait。
4. AgentService 属于 application，不属于插件；不新增大一统 application facade。
5. 模型使用 product-owned 内置控制工具；工具不直接递归 RuntimeService。
6. child 复用现有 InnerAgentLoop、ToolExecutor、Approval、AskUser 和 SessionStore。
7. waiting_child 不占 Supervisor execution permit。
8. child interaction 保存在 child，根 Host 只做汇聚投影和准确路由。
9. 权限先求 hard capability intersection，再应用 auto/approval/deny。
10. async result 使用 Host system_synthetic provenance 和 exact-once delivery。
11. 首版 max_depth=1、bound 默认、detached 禁用；并发只受 root active child 总量限制。
12. 首版不承诺崩溃后自动重启旧 child 执行。
13. child Run 是执行状态唯一权威；Delegation 不镜像 running/waiting/terminal 状态。
14. SpawnAgent(sync) 的 durable 请求保存在原 ToolBatchCall，不新增重复 Run checkpoint。
15. DelegationRepository 同时由 LocalSessionStore 和 SQLiteSessionStore 实现并通过同一契约测试。
16. 标准 Agent-capable Host 统一经 RuntimeSupervisor；裸 RuntimeService 不暴露不可执行的 Agent 工具。
17. agent_id 是稳定 identity，name 只用于展示；builtin/user definition 只在 editable 管理属性上不同。
18. 每个 parent Run 冻结并持久化 AgentCatalogSnapshot；配置 CRUD 从 next_run 生效。
19. tool_selection=all 只选择当前 Catalog 候选工具，不能突破 parent cap、hard deny、ToolPolicy 或 depth。

## 非目标与延后项

本计划首版不实现：

- nested depth 大于 1；
- AgentSwarm、CSV map 或通用 DAG；
- durable Task/Workflow 自动调度；
- per-child git worktree 和自动合并；
- remote worker 或分布式队列；
- AgentDefinition plugin contribution；
- detached child 的公开模型工具入口；
- 自动重放 replay-safe child；
- Desktop 专用 UI 组件。
- child_session/subtree/root_session 记忆型审批授权；
- 通用 capability algebra、独立 AgentBudget 引擎和新的公平调度器。
- 模型侧 AgentDefinition CRUD 工具和可扩权的 per-dispatch prompt/tool/model/workspace override。

首版必须提供足够 application DTO 和无窗口 Host 测试，使后续 Desktop 接入不需要修改领域状态机。

## 当前基础

已具备并应复用：

- SessionRecord 的 root_session_id、parent_session_id 和 branch lineage；
- 单 Session 单 active Run、session run lock 和 RunLifecycle；
- RuntimeSupervisor 的跨 Session 队列、并发、abort、snapshot 和 subscription；
- ToolCallBatch、ApprovalRecord、AskUser、ToolExecutor 和 ToolPolicy；
- interactive/unattended interaction mode；
- host/plugin queued input provenance 和 exact-once queue consumption；
- queued/running Run 到 recoverable、running tool 到 unknown_outcome 的恢复规则；
- RuntimeActivitySnapshot 和 RuntimeEventView；
- ResourceService 的 Session 可达性与不可变 resource identity；
- plugin 对 RuntimeService/SessionStore 的既有限制。
- config/config.json 的类型化 manager、跨进程 mutation lock 和原子 replace；
- ToolRegistry canonical names 与每 Run ExtensionCatalogLease。

不得复制上述能力到新的 subagents package。

## 总体依赖与实施顺序

    A. 领域模型与状态
            |
            v
    B. Delegation 持久化与布局
            |
            v
    C. Application 编排与 Host API
            |
            v
    D. 内置控制工具与 sync join
            |
            v
    E. Root 事件树与 HITL
            |
            v
    F. Background、结果交付与生命周期
            |
            v
    G. 恢复、权限、预算与安全加固
            |
            v
    H. CLI/Host 验收、默认启用与规格转正

每个里程碑必须保持主分支现有测试通过。不得先注册模型可见的 SpawnAgent，再补权限、幂等或恢复。

## 建议代码归属

预期新增或修改位置：

| 位置 | 责任 |
|---|---|
| ouro/core/agents.py | AgentDefinition/ToolSelection、AgentCatalogSnapshot、Delegation、Result/delivery、control/wait request |
| ouro/core/state.py | session_kind、delegation_id、waiting_child、subagent_join |
| ouro/core/events.py | 关系事件类型 |
| ouro/core/tool_batches.py | waiting_child ToolCall/Batch 状态 |
| ouro/config/agents.py | agents.definitions user 配置、revision、原子 CRUD 和 strict validation |
| ouro/application/agent_definitions.py | AgentDefinitionService、builtin/user 合并、工具兼容性和管理 DTO |
| ouro/application/configuration_models.py | agents configuration overview/change DTO 与 effective_from=next_run |
| ouro/sessions/base.py | 组合进 SessionStore 的 DelegationRepository 端口 |
| ouro/sessions/local_files.py | delegation atomic JSON 与查询 |
| ouro/sessions/sqlite.py | delegation 事务、revision、查询与 schema migration |
| ouro/application/agents.py | AgentService、Run-frozen catalog 解析、spawn reconciliation 和窄 RunScheduler port |
| ouro/application/agent_models.py | Host-facing DTO |
| ouro/application/agent_recovery.py | 仅当 reconciliation 形成独立变化轴时再拆分；首版默认留在 AgentService |
| ouro/application/supervisor.py | attribution-aware submit、parked wait 与恢复调度 |
| ouro/application/runtime_activity.py | child wait/activity 投影 |
| ouro/tools/builtin_agents.py | 从 AgentCatalogSnapshot 投影 reserved Agent control tool definitions |
| ouro/application/tool_calls.py | AgentControlRequest、AgentWaitRequest 和恢复 |
| ouro/local_application.py | AgentDefinitionService、AgentService/Supervisor 和 builtin tool 有界装配 |
| tests/config/test_agents.py | user definition CRUD、revision、builtin 保护和字段上限 |
| tests/core/test_agents.py | 模型与状态约束 |
| tests/sessions/test_delegations.py | 文件/SQLite repository contract、布局与 migration |
| tests/application/test_agents.py | application 编排 |
| tests/application/test_agent_recovery.py | 崩溃窗口与恢复 |
| tests/application/test_agents.py | root tree、subscription 与 application contract |
| tests/tools/test_builtin_agents.py | schema、权限和工具结果 |
| tests/architecture/test_boundaries.py | 依赖、插件与 Host 边界 |

具体文件可随现有模块规模调整，但职责和依赖方向不能改变。

## 里程碑 A：领域模型与状态骨架

### A1. Agent 领域类型

- [x] 新增单一不可变 AgentDefinition 值对象，包含稳定 agent_id、name、description、system_prompt、ToolSelection、
  继承策略和 contract hash；builtin/user 与 Delegation 冻结副本使用同一类型。
- [x] 新增 ToolSelection(mode=all|selected, ToolRegistry public names)，selected 空集表示无工具。
- [x] 新增 EffectiveToolRef(public name、canonical identity/owner、execution contract hash)。
- [x] 新增有界 AgentCatalogSnapshot、definition compatibility、missing_tool_names 和 catalog contract hash。
- [x] source/editable 只存在于管理 DTO，不进入 contract hash、prompt、capability 或运行分派条件。
- [x] 新增 DelegationRecord、SpawnState、ControlState、ExecutionMode、LifecycleBinding；不复制 child Run status。
- [x] 新增显式 DelegatedConstraints；复用 RunBudgetPolicy，不新增 AgentBudget/通用 capability blob。
- [x] 新增 DelegationResult 与 ResultDelivery。
- [x] 新增 AgentHandle、AgentWaitRequest/Resolution、completion condition；请求保存在 ToolBatchCall。
- [x] 为全部类型添加严格 Pydantic validation 和 schema version。

约束：

- primary Session 不能携带 delegation_id；
- subagent Session 必须携带 root/parent/delegation；
- detached 与未授权 policy 组合在模型层拒绝；
- terminal child 状态从 child Run 读取，不在 Delegation 重复保存；
- child 初始 prompt 只保存在 child canonical message，Delegation 只保存 initial_message_id/prompt_digest；
- result 文本只保存在 child canonical message，DelegationResult 只保存 result_message_id/refs/usage/delivery；
- delivered result 必须有 delivered_at 和目标 identity；
- pending delivery 不能声称已进入父消息。

### A2. Session/Run/Tool 状态

- [x] SessionRecord 增加 session_kind 和 delegation_id。
- [x] parent Run 的 durable execution contract 保存完整 AgentCatalogSnapshot 或等价可恢复内容。
- [x] RunStatus 增加 waiting_child，并纳入 active status。
- [x] RunPhase 增加 subagent_join。
- [x] ToolCallStatus 增加 waiting_child。
- [x] ToolBatchPhase 增加 awaiting_child 或语义等价状态。
- [x] 共享 Run transition plan 将 waiting_child 投影为 running。
- [x] RuntimeActivity 增加 waiting_child，但 SessionStatus 不扩张为 UI 枚举。
- [x] 更新 exhaustive match、序列化和 SDK/public DTO 边界。

### A3. 事件与 diagnostic identity

- [x] 增加 agent_spawned。
- [x] 增加 agent_result_ready、agent_result_delivered。
- [x] 增加 agent_closed；interrupt 继续使用 child Run 事件。
- [x] 定义 relation event 必填 payload，不在事件中保存 task prompt 全文或敏感 capability。
- [x] 验证 relation event 不复制 child running/waiting/terminal 状态。
- [x] 为恢复 diagnostic 预留稳定 code。

### A 验收

- [x] 新旧 primary Session round-trip。
- [x] 非法 parent/root/delegation 组合被拒绝。
- [x] waiting_child 保持 active Run，Session 投影为 running。
- [x] terminal/result/delivery 时间和 identity validator 完整。
- [x] 现有 Session、Run、tool batch 测试全部通过。

退出条件：只有类型和状态骨架变化，不创建或执行 child。

## 里程碑 B：Delegation 持久化与布局

### B1. Repository 端口

- [x] 定义 DelegationRepository 的 create/load/save/list 端口，并组合进 SessionStore。
- [x] 支持按 root、direct parent、child_session_id 查询；首版 max_depth=1 不增加无语义的 ancestor API。
- [x] 支持 expected revision compare-and-save。
- [x] 支持 pending delivery 和 incomplete spawn 扫描。
- [x] 支持 archive/trash descendant 检查。
- [x] query DTO 不泄漏本地路径。

### B2. 本地布局

- [x] 在 root Session 下创建 delegations/<delegation_id>.json。
- [x] 使用同目录 temporary、flush/fsync、atomic replace。
- [x] 校验 delegation/root id 后再拼接路径。
- [x] 文件后端直接扫描 root-local canonical relation；不为首版规模建立冗余 lookup/index。
- [x] 无 relation 派生索引，因此不增加无生产者的 dirty marker。
- [x] 单 Session export 保持 Session 边界；完整 relation tree 通过脱敏 AgentTree DTO 单独导出/查询。

### B3. SQLite 实现

- [x] SQLiteSessionStore 在数据库事务中实现同一 DelegationRepository 契约。
- [x] 表、外键、唯一 idempotency key、revision 和 child_session_id 反查索引只存在于 adapter 内。
- [x] pending delivery、incomplete spawn 和 descendant 查询不旁路到文件存储。
- [x] 文件与 SQLite 运行同一组 repository contract tests。

### B4. 版本与迁移

- [x] 明确 data_layout_version 从 3 到下一版本。
- [x] 实现一次性、单向、可重复运行的迁移，或按开发期政策明确拒绝旧布局。
- [x] 不在正常 load 路径散布兼容分支。
- [x] child Session trash/root trash 的文件归属有确定规则。
- [x] migration 中断后可重试，不产生半个 relation index。
- [x] SQLite 使用独立 schema version 和事务 migration，不复用文件 data_layout_version。

### B5. 创建协议的持久化基础

- [x] parent_tool_call_id/Host request_id 唯一约束。
- [x] 同一 idempotency key 重试返回同一 Delegation。
- [x] spawn_state=requested 可以没有 child_session_id；child_created/submitted 阶段校验更严格。
- [x] relation revision 保护 spawn_state/control_state/result/delivery 并发写。
- [x] 建立 reconciliation 所需的阶段标记。

### B 验收

- [x] atomic write 各失败点测试。
- [x] Windows sharing conflict 沿用本地 I/O 原语。
- [x] index 删除后可从 canonical files 重建。
- [x] 重复 create/save 不重复 identity。
- [x] corrupt、mismatch、missing child 查询产生类型化错误。
- [x] layout migration/拒绝路径有测试。
- [x] SQLite schema migration/rollback 路径有测试。
- [x] 文件与 SQLite 的相同操作产生语义等价记录和错误。
- [x] archive/trash 不遗留不可发现 relation。

退出条件：可独立创建、查询和恢复 DelegationRecord，但还不运行 child。

## 里程碑 C：Application 编排与 Host API

### C1. Managed AgentDefinition catalog

- [x] 在 composition root 显式装配 `agent_id=general` 的 code-owned name/description/system_prompt，
  tool_selection=all、can_spawn=false，并生成稳定 contract hash。
- [x] builtin general 永远存在，不扫描 workspace/user plugin，不写入 agents.definitions。
- [x] 实现 agents.revision + agents.definitions 的严格 config manager 和 expected revision mutation。
- [x] 实现 user definition list/get/create/update/delete 与 copy_builtin；agent_id 创建后不可改，name 可改。
- [x] 首版编辑字段覆盖 name/description/system_prompt/tool_selection；其余 policy 使用安全产品默认，不接受任意 JSON。
- [x] builtin/user management view 只以 editable 区分；builtin update/delete/覆盖返回类型化错误。
- [x] AgentDefinitionService 合并 builtin/user，使用同一 validator 和 contract hasher，不建两套 registry。
- [x] 从当前 ExtensionCatalog/ToolRegistry 提供 canonical tool options 与 compatibility diagnostics。
- [x] save 时拒绝当前未知 selected tool；后续工具缺失时保留配置、标 unavailable，不静默缩小工具集合。
- [x] all/selected 都计算 exact EffectiveToolRefs；Agent control tools 按 can_spawn/depth 排除。
- [x] system_prompt 追加在 OURO base instructions 之后，不可替换平台规则。
- [x] 配置变更标记 effective_from=next_run。
- [x] 新 parent Run 冻结并持久化 AgentCatalogSnapshot；active Run 不热更新。
- [x] 新 Delegation 保存不可变 definition 和 EffectiveToolRefs；definition 更新/删除不改变旧 Delegation。

### C2. AgentService 与 supervised composition

- [x] 实现 spawn_agent(request)。
- [x] 实现 list_agents/read_agent。
- [x] 实现 direct child/root tree 查询。
- [x] spawn 前校验模型请求的 active parent 或 Host 请求的 completed parent，以及 workspace、model、depth、budget 和 capability。
- [x] 按规格顺序写 requested relation、child Session、child Run 和 spawn_state；不镜像 child Run status。
- [x] 通过 RuntimeSupervisor 提交已有 child Session。
- [x] 定义窄 RunScheduler Protocol，由现有 RuntimeSupervisor 实现，不创建第二个 scheduler/worker pool。
- [x] LocalApplication 显式装配 RuntimeService、RuntimeSupervisor、AgentDefinitionService 和 AgentService，不提供通用
  reverse host.call。
- [x] child Run 使用 DelegatedTaskInput、稳定 delegation_id、明确 origin/initiator/parent_run provenance；task 为 user role。
- [x] 标准 Agent-capable Host 路径统一经过 Supervisor；裸 RuntimeService 缺少装配时不注册 Agent 工具或 fail closed。
- [x] 任何异常保留可 reconciliation 的 durable 状态。

### C3. Host application service 与 DTO

- [x] 从 ouro.application 导出 AgentDefinitionService、AgentService 和各自 DTO；不新增 ouro/application/facade.py。
- [x] provider-independent `open_local_agent_definitions` 暴露 definition CRUD/catalog facet；Agent-capable
  LocalApplication 额外提供基于 live ExtensionCatalog 的 tool options/compatibility 与 Run snapshot。
- [x] definition DTO 包含 agent_id/name/description/tool_selection/source/editable/revision/compatibility，只有显式 get/edit
  响应返回 system_prompt 全文。
- [x] definition management 暴露 tool options、missing ids 和 copy builtin，不要求 Host 读取 ToolRegistry。
- [x] definition mutation 使用 expected revision；runtime control mutation 使用稳定 request_id 或 relation identity。
- [x] DTO 不返回完整 child prompt、secret 或内部 file path。
- [x] Host API 与模型工具共享同一 service，不复制校验。
- [x] 无窗口 fake Host 可以创建 child 并等待 terminal。

### C4. 最小 capability cap

在模型可见工具出现前完成：

- [x] 从 parent 当前可下放权限计算显式 tool/workspace/model/network/MCP 约束。
- [x] 与冻结 AgentDefinition 约束求交，不建设通用 capability algebra。
- [x] workspace read/write 上限。
- [x] spawn agent_id allow/approval/deny。
- [x] max_depth=1。
- [x] 工具能力不参与派遣并发；合法派遣统一提交现有 Supervisor。
- [x] detached 强制拒绝。
- [x] child session approval/interaction mode 固化。

### C 验收

- [x] Host spawn builtin general 与 user custom 都完成相同的独立 Session/Run 路径。
- [x] builtin general 不可编辑/删除/覆盖，自定义 copy 得到新 agent_id。
- [x] 修改 name 不破坏引用，修改/删除 definition 不改变 active Run 和旧 Delegation。
- [x] selected tool 缺失时新 Run 不把 definition 暴露给 SpawnAgent，但配置仍可编辑。
- [x] child 不复制完整父 history。
- [x] duplicate request 返回同一 handle。
- [x] 父没有 write 能力时不能创建 write-capable child。
- [x] unknown/unavailable agent_id、超 depth、预算不足有类型化结果。
- [x] child failure 不破坏父 Session canonical history。
- [x] Plugin 或 Host 不能绕过 AgentService 直接创建合法 Delegation。

退出条件：通过 application API 可完成单层 child 闭环，但模型尚看不到 Agent 工具。

## 里程碑 D：内置控制工具与 Foreground Join

### D1. 控制工具协议

- [x] 定义 AgentControlRequest 和 AgentControlResult。
- [x] 定义 AgentWaitRequest，不能复用 ToolInputRequest 假装用户交互。
- [x] 沿用现有类型化 control intent 模式，不为 Agent 工具建立通用 ToolDefinition kind 层级。
- [x] ToolExecutor 继续负责 schema、权限、ToolPolicy、approval。
- [x] application/tool_calls 负责执行 control intent。
- [x] 工具层不得 import RuntimeService、RuntimeSupervisor 或 AgentService 实现。

### D2. Builtin tools

- [x] SpawnAgent（Host display name 可为“派遣子智能体”）。
- [x] ListAgents。
- [x] SendAgentMessage。
- [x] InterruptAgent。
- [x] read_agent/close_agent 先保留为 Host API；模型结果查询并入 ListAgents。
- [x] 名称进入 builtin reserved set，插件/MCP 冲突明确失败。
- [x] 从 parent Run 的同一 AgentCatalogSnapshot 生成 SpawnAgent.agent_id enum/oneOf 及有界 name/description 映射。
- [x] SpawnAgent handler 按同一 snapshot 解析 agent_id，不重读 current config。
- [x] SpawnAgent 不接受自由 system_prompt/tool/model/workspace 扩权覆盖。
- [x] system_prompt、完整 tool refs 和敏感 policy 不进入模型可见的派遣工具描述。
- [x] tool invocation/Run 保存 agent catalog contract hash。
- [x] 工具输出有界，默认不返回完整 child history。
- [x] lifecycle mutation 使用 sequential/exclusive batch behavior；SpawnAgent 保持兼容批次语义。

### D3. Durable sync wait

- [x] SpawnAgent(sync) 把 AgentWaitRequest/Resolution 保存在原 ToolBatchCall，不新增 Run 级 AgentWaitCheckpoint。
- [x] ToolCall 进入 waiting_child。
- [x] parent Run 进入 waiting_child/subagent_join。
- [x] RuntimeService 返回 waiting outcome，不保持 tool coroutine。
- [x] Supervisor 释放 worker/permit。
- [x] child terminal 触发原 ToolCall wait resolution。
- [x] child result 作为原 SpawnAgent ToolResult 提交。
- [x] parent Run 在 session lock 内恢复，再由 Supervisor 重新调度。
- [x] Host `wait_agents()` 保留多 handle 等待；模型侧每个 sync Spawn 只等待自己的 child。
- [x] 结果顺序按 wait request，而不是 child completion 时间。

### D4. Permit 与调度

- [x] 明确 active execution permit 与 durable active Run 分离。
- [x] waiting_child/waiting_user/waiting_approval 不占 permit。
- [x] max_concurrency=1 可运行 parent -> child -> parent。
- [x] 多 parent 同时等待不会耗尽 worker。
- [x] parent resume 与新 child submit 经过现有跨 Session FIFO 队列，不新增公平调度层。
- [x] shutdown 不把 parked wait 标 completed。

### D 验收

- [x] 模型可 spawn general/custom definition 并通过同一 SpawnAgent 调用得到 ToolResult。
- [x] 模型可派遣 builtin general 和 user custom，除 definition 内容外经过相同调用路径。
- [x] create/update/delete 后下一 Run 的 agent_id schema 自动变化，active Run schema 和解析保持冻结。
- [x] schema 中没有 unavailable definition；general 始终存在于目录，但可被显式 Agent allowlist 从 Run schema 排除。
- [x] max_concurrency=1 无死锁。
- [x] wait all/any/timeout。
- [x] parent abort while waiting 的 ToolCall wait request 与 child cascade 状态正确。
- [x] Tool approval rejection 形成 error ToolResult，不创建 child。
- [x] illegal tool args 不创建 relation。
- [x] SpawnAgent(sync) 恢复后同一 tool_call_id/turn_id 不变。
- [x] provider continuation 与 tool result 顺序保持现有协议。

退出条件：模型侧 sync 子智能体完整可用，且等待不占 worker。

## 里程碑 E：Root 事件树、消息流与 HITL

### E1. AgentTreeSnapshot

- [x] 定义 AgentTreeNode、AgentTreeEdge、AgentTreeSnapshot。
- [x] node 组合 Delegation 与 child RuntimeActivitySnapshot。
- [x] 汇总 active/queued/waiting/terminal、usage 和 pending interaction。
- [x] root projection revision 独立于 per-session event sequence。
- [x] snapshot 可从 Store/Session/Run 重建，不依赖内存 registry。

### E2. Root subtree subscription

- [x] subscribe_agent_tree(root_session_id)。
- [x] 初始 snapshot 后发送增量 notification。
- [x] 新 child 动态加入订阅。
- [x] terminal/close child 停止 live delta 但保留历史节点。
- [x] child text/reasoning/tool events 可按 child stream 打开。
- [x] root stream 默认只发有界状态/关系摘要，不复制完整 transcript。
- [x] 有界队列、lag diagnostic、detach/reconnect。
- [x] Host detach 不 abort child。

### E3. Interaction inbox projection

- [x] 汇聚 descendant ApprovalRecord。
- [x] 汇聚 descendant ToolInputRequest。
- [x] 只从 child canonical interaction 重建；不新增 store、broker 或第二套状态机。
- [x] DTO 标注 agent/task/session/run/tool/source。
- [x] resolve request 验证 child session/run/tool identity。
- [x] 错答到 sibling/parent 明确拒绝。
- [x] sync child HITL 时 parent 继续 waiting_child。
- [x] async child HITL 不改变 parent Run。
- [x] simultaneous child requests 不丢失、不串答。
- [x] headless/unattended 使用确定性 policy，不无限等待。

### E4. Runtime activity

- [x] waiting_child activity 和 parent-child cause。
- [x] child waiting approval/user 在 tree 中可见。
- [x] terminal result ready 与 delivered 分开显示。
- [x] reconnect 后 UI 不需从相邻 delta 猜状态。
- [x] redaction 覆盖 task、tool args 和 delegated constraints。

### E 验收

- [x] fake Desktop adapter 可实时显示 tree 和打开 child stream。
- [x] 两个并行 child 事件互不串线。
- [x] child 审批在根 inbox 显示来源并正确恢复。
- [x] AskUser answer 不能提交到错误 child。
- [x] 慢 subscriber 不阻塞 runtime。
- [x] detach/reconnect 后 snapshot 与 canonical 一致。
- [x] child stream 可显示 text/reasoning/tool/HITL/terminal。

退出条件：任意 Host 可完整感知和控制 child，不需要读取本地文件。

## 里程碑 F：Background、结果交付与生命周期

### F1. Async spawn

- [x] execution_mode=async。
- [x] SpawnAgent 立即返回 handle。
- [x] parent Run 可继续调用模型/工具。
- [x] async child 受相同 capability、通用调度和 HITL 规则。
- [x] 模型工具说明禁止轮询，List/Read 仍可显式查询。
- [x] child completion 立即产生 result_ready。

### F2. Exact-once result delivery

- [x] DelegationResult 原子提交，只保存 result_message_id/refs/usage/delivery，不复制最终消息文本。
- [x] delivery_id 稳定。
- [x] Host 直接派遣使用 notify_only。
- [x] async 固定 next_boundary，在 active parent 映射 Host steer、idle parent 创建后续 Run。
- [x] resume_waiter 只供 sync。
- [x] 使用 producer=host、system_synthetic、稳定 host_source_id。
- [x] queue/message 提交后才确认 delivered。
- [x] crash before/after queue/message/delivery update 的所有窗口可恢复。
- [x] 绝不写 role=user。

### F3. Send、resume、interrupt、close

- [x] active child SendAgentMessage 使用 steer。
- [x] idle child follow-up 创建新 child Run。
- [x] resume 同一 child Session，不覆盖旧 Run。
- [x] interrupt queued/running/waiting child。
- [x] close 只关闭控制生命周期，不删除 history。
- [x] terminal child 可由 Host read_agent 或模型 ListAgents 获取结构化 result refs。
- [x] late event 不能把 closed/terminal relation 恢复为 running。

### F4. Bound cascade

- [x] parent abort/failed 级联 bound child。
- [x] cascade 有 deadline 和 diagnostic。
- [x] child unknown outcome 仍保留。
- [x] root archive/trash 拒绝 active descendants 或显式级联。
- [x] direct parent close 不删除 descendant history。
- [x] 首版 detached 对模型和普通 Host 配置保持禁用。

### F 验收

- [x] parent work 与 async child 同时运行。
- [x] async result 恰好一次进入父上下文。
- [x] 进程在每个 delivery 窗口退出后恢复不重复。
- [x] send active/idle 两条路径。
- [x] interrupt/close/resume identity 不混淆。
- [x] parent abort cascade。
- [x] result ready 但未 delivered 可查询且可重试。
- [x] Host 只订阅状态也不会丢失最终结果。

退出条件：sync/async、控制和结果联动形成完整闭环。

## 里程碑 G：恢复、权限、预算与安全加固

### G1. Startup reconciliation

- [x] relation 无 child。
- [x] child 无 relation或 mismatch。
- [x] relation 有 child 无 Run。
- [x] child active 无 live job。
- [x] child terminal 无 result。
- [x] result pending delivery。
- [x] parent ToolCall wait request 与 child terminal 不一致。
- [x] reconciliation 幂等且产生稳定 diagnostic。
- [x] 不可安全修复时 recoverable/failed，不删除证据。

### G2. 权限完整化

- [x] platform/parent/definition/workspace/revocation 的显式约束求交。
- [x] agent_id spawn permission patterns。
- [x] tool_selection=all/selected 先解析 exact EffectiveToolRefs，再与 parent/hard cap 求交。
- [x] all 不跳过 approval/deny/ToolPolicy，不包含 depth 禁止的 Agent control tools。
- [x] sync/async/lifecycle 权限。
- [x] write child、network、MCP、external path 下放限制。
- [x] 复用逐 ToolCall ApprovalRecord 和 child Session approval_mode。
- [x] 一次审批不能传播到 sibling、root 或未来 descendant。
- [x] child 运行期间收紧权限，未执行调用重新求值。
- [x] user/builtin plugin 不能提升 child capability。
- [x] auto mode 不能突破 hard deny。
- [x] Spawn approval 与 child tool approval 独立。

### G3. 上下文与资源

- [x] child 文本上下文固定为 base instructions、AgentDefinition system_prompt 与 SpawnAgent task。
- [x] SpawnAgent 不接受 canonical message ids，父 history/compaction 不投影到 child。
- [x] resource refs 验证 parent/child 可达性并显式复制引用。
- [x] 不自动复制 credential、approval answer、插件私有 context。
- [x] child result resource refs 在进入父 Session 前重新验证。
- [x] ContextView 预算包含 child 注入内容。
- [x] custom system_prompt 位于 base instructions 之后，只进入目标 child，不进入 SpawnAgent description/root tree/events。

### G4. 预算、配额与现有调度

- [x] per-child 直接复用 RunBudgetPolicy 的 model round/token/time 限制。
- [x] root usage 从 child Run usage 聚合，不新增 AgentBudget 引擎。
- [x] max active/queued children。
- [x] max depth=1。
- [x] writer concurrency=1。
- [x] budget exhaustion paused/aborted，不标 completed。
- [x] completed result 在预算耗尽后仍可交付。
- [x] 复用现有 FIFO 与通用并发上限；全部合法派遣进入调度，不新增 root-aware 公平调度器。
- [x] usage 汇总不重复计算 resume/follow-up。

### G5. 安全与架构

- [x] Agent control tools 不能被 plugin/MCP 覆盖。
- [x] architecture test 禁止 tools/plugins/Host import application implementation。
- [x] plugin 没有 AgentService handle。
- [x] child prompt、event、error、DTO 执行脱敏。
- [x] AgentDefinition list DTO 默认不返回 system_prompt 全文；get/edit 显式返回。
- [x] definition source/editable 不进入 AgentService 执行分支。
- [x] current config 漂移不改写旧执行；Run AgentCatalogSnapshot、Delegation definition 与 child contract mismatch 有
  fail-closed diagnostic。
- [x] workspace path 和 shell 继续走现有 ToolExecutionContext。
- [x] 并行写冲突通过标准工具失败结果回到 child，不与调度配额耦合。
- [x] unknown_outcome 不自动重放。
- [x] root delete/archive 明确显示 descendant scope。

### G 验收

- [x] 按规格完整故障注入矩阵。
- [x] parent hard deny 越权负向测试。
- [x] unattended/headless 无悬挂。
- [x] 敏感父历史不进入 child。
- [x] resource 不产生悬空引用。
- [x] 并发派遣接受语义与 FIFO 压力测试。
- [x] 全量重启恢复不重复 spawn、tool 或 delivery。
- [x] 架构依赖测试保护所有边界。

退出条件：安全、恢复和资源治理达到当前 Runtime/Tool 同等级别。

## 里程碑 H：CLI/Host 验收与发布收口

### H1. CLI/Application 控制面

建议提供：

- [x] agent definition list/show/create/update/delete/copy。
- [x] agent definition tools/compatibility。
- [x] agent list/show。
- [x] agent spawn。
- [x] agent wait。
- [x] agent send。
- [x] agent interrupt。
- [x] agent close。
- [x] agent tree。
- [x] agent interactions。
- [x] JSON 输出稳定 DTO 和错误码。
- [x] 非交互模式遇到未授权审批确定性失败。

命令名称可按现有 CLI 风格调整，但 definition 管理必须调用 AgentDefinitionService，运行控制调用 AgentService；两者都
不读取 config/canonical files。

### H2. Host adapter contract

- [x] 无窗口 adapter 测试 root snapshot/subscription。
- [x] child stream 增量加载。
- [x] interaction source badge 所需字段完整。
- [x] stop/open/send 控制返回最新 revision。
- [x] refresh/reconnect 不重复内容。
- [x] UI 不需要自己实现 cascade、permission 或 delivery。
- [x] definition editor 显示 source/editable、all/selected tool picker、missing tools、revision 和 next_run 生效提示。
- [x] builtin general 的编辑/删除控件禁用，但可复制；Host UI 限制之外后端仍强制校验。
- [x] Interactive 以非阻塞输入持续消费 root inbox；sync/async subagent 的工具审批和 AskUser 都由主窗口处理，
  sync 继续等待原父 Run，终端不展开 subagent 行动轨迹。

### H3. 配置与启用

- [x] 完成版未保留临时 composition-root gate 或半成品双路径。
- [x] 完成前移除临时双路径和兼容分支。
- [x] 标准 LocalApplication 默认注册 Agent control tools；裸 RuntimeService fail closed。
- [x] 配置 max_depth、budget、allowed agent ids；预算复用通用 RunBudgetPolicy，并发复用 Supervisor 通用配置。
- [x] 首版 detached/nested 明确拒绝而不是静默忽略。
- [x] builtin general 可查询、不可编辑/删除/被 user 或 plugin 覆盖；custom 可编辑/删除。
- [x] definition change 返回 next_run，runtime policy 返回 next_runtime；active Run 不热切 schema。

### H4. 文档转正

- [x] 更新 core-runtime 的 waiting_child 当前行为。
- [x] 更新 sessions-persistence 的 layout/recovery。
- [x] 更新 tools-hitl 的 Agent control tool 和 HITL。
- [x] 更新 resources-supervisor 的 parked permit/root scheduling。
- [x] 更新 CLI/Host 规格。
- [x] 子智能体规格由“目标”改为“当前”。
- [x] future-platform 移除已完成占位，只保留后续能力。
- [x] 高级智能体能力计划更新状态。
- [x] 项目概述和开发架构只描述已经实现的能力。

### H 验收

- [x] builtin general sync 端到端。
- [x] selected-tools custom permission approval 端到端。
- [x] all-tools custom 仍受 hard deny/ToolPolicy 端到端。
- [x] definition CRUD -> next Run SpawnAgent schema 自动联动端到端。
- [x] async exact-once 端到端。
- [x] child AskUser 根路由端到端。
- [x] interrupt/restart/recoverable 端到端。
- [x] CLI 与 fake Desktop 对同一 tree 显示一致状态。
- [x] 全局质量门槛通过。
- [x] 文档链接和状态无“目标/当前”冲突。

退出条件：功能可以默认提供，规格、实现和可重复测试一致。

## 测试矩阵

### 正常路径

- Host spawn、模型 SpawnAgent；
- sync SpawnAgent durable join；
- async next-boundary 自动交付；
- child stream；
- child follow-up/resume；
- completed result/read/close。

### Definition 配置与目录

- builtin general 始终存在、editable=false；
- builtin update/delete/同 id user create 拒绝；
- copy builtin -> 新 user agent_id；
- user create/get/update/delete 与 expected revision；
- name rename 不改变 agent_id；
- all、selected、selected-empty；
- 当前未知 selected tool 在保存时拒绝；
- 后续 tool/plugin/MCP 缺失时配置保留、compatibility=unavailable；
- active Run 冻结旧 catalog，next Run 自动取得新 SpawnAgent schema；
- 删除 custom 后旧 Delegation resume，新的 Run 不再显示；
- builtin/user 相同 spawn/permission/HITL/recovery parameterized tests；
- product count/length/tool-count limits 与 schema/context size budget。

### 并发

- max_concurrency=1；
- 多 root、多 child；
- parent 与 async child 并行；
- 多 child 同时完成；
- 多 child 同时 approval/AskUser；
- duplicate spawn request；
- result delivery 与 parent turn boundary 竞争；
- interrupt 与 terminal 竞争。

### 恢复

- requested relation 后崩溃；
- child Session 创建后崩溃；
- child Run queued/running 后崩溃；
- parent waiting_child 时崩溃；
- child tool running 时崩溃；
- result ready 前后崩溃；
- queue committed 前后崩溃；
- delivered flag 前后崩溃；
- subscription detach/reconnect；
- index/dirty marker 重建。

### 权限与安全

- parent deny vs child allow；
- auto vs hard deny；
- agent_id deny；
- all tools 不能越过 parent deny/approval/ToolPolicy/depth；
- write/network/MCP/external path；
- spawn approval 与 tool approval；
- approval 不传播到 sibling/root/descendant；
- policy runtime tightening；
- plugin attempt to elevate；
- reserved tool name conflict；
- sensitive context/result redaction；
- malformed/mismatched identifiers。

### 生命周期

- parent completed/failed/aborted；
- child completed/failed/aborted/recoverable；
- bound cascade；
- close active rejection；
- archive/trash active descendant；
- resume from terminal/recoverable；
- budget paused/exhausted；
- shutdown wait/abort。

## 实施纪律

- 每个里程碑先提交失败测试和类型契约，再实现行为。
- 领域/持久化变更与 Host UI 变更分开。
- 不在一个提交中同时提升 data layout、注册模型工具并启用默认配置。
- 不以 mock child loop 代替真实 RuntimeService 集成测试。
- 不以 sleep/polling 测试等待；使用事件、可控 fake adapter 和确定性 barrier。
- 所有故障注入测试验证 canonical files/records，而不只验证返回异常。
- 每个新增 mutable registry 都必须说明 process-local 身份和关闭 owner。
- 不手写第二份 SpawnAgent agent_id enum；schema 必须由 AgentCatalogSnapshot 投影。
- 恢复逻辑不得依赖日志文本、标题或 synthetic summary 反推 identity。
- 实现中发现规格需要改变时先修改规格，再继续代码。
- 一次只推进一个可以独立退出的里程碑。

## 风险与控制

### Worker/permit 死锁

风险：父 Run 等 child 时占住所有 Supervisor worker。

控制：waiting_child 必须让 RuntimeService 返回 durable waiting outcome；恢复作为新调度工作进入队列。将
max_concurrency=1 作为强制验收。

### 跨 Session 半事务

风险：relation、child Session 和 child Run 分开写，进程中断留下半状态。

控制：稳定 idempotency key、requested 阶段、单向提交协议、revision 和启动 reconciliation；不假装跨目录原子事务。

### 权限提升

风险：child definition、auto mode 或插件规则扩大父能力。

控制：capability intersection 先于审批策略；hard deny 负向测试；spawn 与 child tool 两次独立权限检查。

### Definition 配置与派遣 schema 漂移

风险：active Run 发送给模型的 agent_id schema、执行时 current config 和 child 实际 prompt/tools 不一致。

控制：新 Run 持久化 AgentCatalogSnapshot；schema 与 handler 从同一 snapshot 读取；配置 effective_from=next_run；
Delegation 再冻结最终 definition/EffectiveToolRefs。

### “全部工具”误当授权

风险：custom definition 选择 all 后获得父 Agent 没有的 shell、network、MCP 或 Agent control 权限。

控制：all 只枚举 Run Catalog 候选工具；之后仍与 parent cap、workspace、depth、revocation、approval/deny 和 ToolPolicy
求交，并对 all/selected 运行相同负向测试。

### Builtin/user 双轨漂移

风险：内置 general 使用硬编码 spawn handler，自定义 definition 走另一套配置路径，长期行为不一致。

控制：两者归一化为同一 AgentDefinition、contract hash 和 AgentCatalogSnapshot；source/editable 只在管理 API 使用；
以同一 parameterized integration suite 验证运行路径。

### 自定义 prompt 与 schema 膨胀

风险：自定义 system_prompt 覆盖平台指令，或大量 definition/description 使模型工具 schema 和 child context 无界增长。

控制：base instructions 始终优先；custom prompt 只进入目标 child 的专用边界；派遣 schema 只投影有界
agent_id/name/description；集中限制 definition 数量和字段长度。

### 结果重复

风险：child 完成与 Host/进程重试造成父上下文重复结果或重复 follow-up。

控制：稳定 delivery_id、queued input id、canonical message 去重和 result/delivery 分离。

### 上下文污染

风险：复制父历史或回灌 child 中间日志使主上下文膨胀。

控制：child 文本上下文固定为 base/AgentDefinition instructions 与 task，不提供父消息选择或摘要投影；根 tree
只投影状态，父上下文只接收结构化结果。

### 插件耦合

风险：把调度实现为 builtin plugin，为访问 RuntimeService 引入反向 host.call。

控制：AgentService 固定属于 application；插件首版不能贡献 definition 或取得 service。

### 状态组合过多

风险：同时实现 nested、detached、swarm、Goal/Task 和 worktree，使恢复矩阵不可控。

控制：首版 max_depth=1、bound only、无 swarm/Goal integration；延后能力逐项独立设计。

### 共享工作区冲突

风险：多个 writer child 修改相同文件。

控制：首版 writer concurrency=1；并行探索优先使用只读 Agent；worktree 在后续单独实现。

## 全局质量门槛

每个里程碑至少运行相关测试；H 退出前运行：

    python -m pytest
    python -m ruff check ouro tests
    python -m ruff format --check ouro tests
    python -m mypy ouro
    python -m compileall -q ouro
    git diff --check

还必须通过：

- architecture dependency tests；
- data layout/migration tests；
- fault injection/recovery tests；
- permission negative tests；
- resource reachability tests；
- cancellation/process cleanup tests；
- event redaction tests；
- AgentDefinition config/catalog/schema snapshot contract tests；
- LocalSessionStore/SQLiteSessionStore repository contract tests；
- documentation link/state checks。

## 首版完成条件

只有同时满足以下条件，计划才可标记完成：

- Session/Run/Delegation/Result 都有 durable identity。
- child Run 是执行状态唯一事实源，ToolBatchCall 是 wait 状态唯一事实源。
- builtin general 不可编辑/删除且始终可用；custom definition 完成安全 CRUD。
- builtin/user 只在 editable 管理属性上不同，运行时使用同一 AgentDefinition 与 spawn pipeline。
- SpawnAgent agent_id schema 与执行解析来自同一 durable AgentCatalogSnapshot，配置从 next_run 生效。
- all/selected tool selection 都不能突破父级权限、depth 或现有 ToolPolicy。
- sync/async 使用同一 spawn 原语。
- waiting parent 不占 execution permit。
- child stream、root tree 和 HITL 可由任意 Host 使用。
- 权限、workspace、预算不超过父级上限。
- async result exact-once。
- resume context 不伪装 execution resurrection。
- restart、cancel、failure 不产生重复执行、孤儿 child 或悬空 resource。
- 插件和前端均未获得 Runtime/Supervisor 内部控制权。
- 文件和 SQLite 后端实现同一 DelegationRepository 契约。
- 未引入 facade.py、第二套 scheduler/event bus/HITL/store、通用 capability algebra 或独立 AgentBudget。
- 所有强制规格条款有当前代码或自动化测试证据。
- 相关“当前”规格、项目概述和开发架构已更新为真实行为。

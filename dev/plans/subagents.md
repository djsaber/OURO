# 子智能体实施计划

状态：未开始

## 目标

按照[子智能体规格](../specs/subagents.md)实现一等、可恢复、Host 中立的 OURO 子智能体能力。首版形成以下闭环：

- product-owned AgentDefinition；
- durable DelegationRecord；
- 单层 child Session/Run；
- Host API 与模型内置控制工具；
- foreground spawn + wait；
- background spawn + exact-once result delivery；
- root AgentTreeSnapshot、消息流和 interaction inbox；
- child approval/AskUser；
- capability intersection、预算和取消；
- 重启 reconciliation 与负向测试。

本计划是[高级智能体能力计划](advanced-agent-capabilities.md)工作流 C 的详细执行计划。两者冲突时，以子智能体
规格的规范性契约和本计划的细化顺序为准；高级计划只承担跨长期记忆、Goal/Task 和子智能体的总体协调。

## 首版成功定义

用户或主 Agent 可以创建一个 explorer/worker child。child 在独立 Session/Run 中流式工作，Host 可查看其状态、
消息、工具调用和 HITL；父 Agent 可以等待、继续工作、中断或发送 follow-up。进程退出后历史和关系仍可查询，
未完成执行进入 recoverable，未知副作用不会自动重放，完成结果不会重复交付。

## 固定架构决策

实施开始前将以下决定视为已冻结，若需改变必须先更新规格：

1. child instance 等于 Session，child execution 等于 Run。
2. Delegation 与 Session branch 是不同关系。
3. spawn 底层始终非阻塞；foreground 是 durable wait。
4. AgentOrchestrationService 属于 application，不属于插件。
5. 模型使用 product-owned 内置控制工具；工具不直接递归 RuntimeService。
6. child 复用现有 InnerAgentLoop、ToolExecutor、Approval、AskUser 和 SessionStore。
7. waiting_child 不占 Supervisor execution permit。
8. child interaction 保存在 child，根 Host 只做汇聚投影和准确路由。
9. 权限先求 hard capability intersection，再应用 auto/approval/deny。
10. background result 使用 Host system_synthetic provenance 和 exact-once delivery。
11. 首版 max_depth=1、bound 默认、detached 禁用、并行 writer 限制为 1。
12. 首版不承诺崩溃后自动重启旧 child 执行。

## 非目标与延后项

本计划首版不实现：

- nested depth 大于 1；
- AgentSwarm、CSV map 或通用 DAG；
- durable Goal/Task 自动调度；
- per-child git worktree 和自动合并；
- remote worker 或分布式队列；
- AgentDefinition plugin contribution；
- detached child 的公开模型工具入口；
- 自动重放 replay-safe child；
- Desktop 专用 UI 组件。

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
    D. 内置控制工具与 foreground join
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
| ouro/core/agents.py | AgentDefinition snapshot、Delegation、Result、WaitCheckpoint 等稳定模型 |
| ouro/core/state.py | session_kind、delegation_id、waiting_child、subagent_join |
| ouro/core/events.py | 关系事件类型 |
| ouro/core/tool_batches.py | waiting_child ToolCall/Batch 状态 |
| ouro/sessions/base.py | DelegationStore 端口 |
| ouro/sessions/local_files.py 或等价实现 | delegation atomic JSON 与查询 |
| ouro/application/agents.py | AgentOrchestrationService |
| ouro/application/agent_models.py | Host-facing DTO |
| ouro/application/agent_recovery.py | reconciliation |
| ouro/application/supervisor.py | parked wait、恢复调度和 root fairness |
| ouro/application/runtime_activity.py | child wait/activity 投影 |
| ouro/application/facade.py | Host 控制面 |
| ouro/tools/builtin_agents.py | reserved Agent control tool definitions |
| ouro/application/tool_calls.py | AgentControlRequest、AgentWaitRequest 和恢复 |
| ouro/local_application.py | definition registry、service 和 builtin tool 装配 |
| tests/core/test_agents.py | 模型与状态约束 |
| tests/sessions/test_delegations.py | 本地持久化与布局 |
| tests/application/test_agents.py | application 编排 |
| tests/application/test_agent_recovery.py | 崩溃窗口与恢复 |
| tests/application/test_agent_events.py | root tree/subscription |
| tests/tools/test_agent_tools.py | schema、权限和工具结果 |
| tests/architecture/test_boundaries.py | 依赖、插件与 Host 边界 |

具体文件可随现有模块规模调整，但职责和依赖方向不能改变。

## 里程碑 A：领域模型与状态骨架

### A1. Agent 领域类型

- [ ] 新增 AgentDefinition、AgentDefinitionSnapshot 和 contract hash。
- [ ] 新增 DelegationRecord、DelegationStatus、ExecutionMode、LifecycleBinding。
- [ ] 新增 DelegatedCapabilitySnapshot、AgentBudget、ContextPolicy。
- [ ] 新增 DelegationResult 与 ResultDelivery。
- [ ] 新增 AgentHandle、AgentWaitCheckpoint、completion condition。
- [ ] 为全部类型添加严格 Pydantic validation 和 schema version。

约束：

- primary Session 不能携带 delegation_id；
- subagent Session 必须携带 root/parent/delegation；
- detached 与未授权 policy 组合在模型层拒绝；
- terminal status 必须有 ended_at；
- delivered result 必须有 delivered_at 和目标 identity；
- pending delivery 不能声称已进入父消息。

### A2. Session/Run/Tool 状态

- [ ] SessionRecord 增加 session_kind 和 delegation_id。
- [ ] RunStatus 增加 waiting_child，并纳入 active status。
- [ ] RunPhase 增加 subagent_join。
- [ ] ToolCallStatus 增加 waiting_child。
- [ ] ToolBatchPhase 增加 awaiting_child 或语义等价状态。
- [ ] session_status_for_run 将 waiting_child 投影为 running。
- [ ] RuntimeActivity 增加 waiting_child，但 SessionStatus 不扩张为 UI 枚举。
- [ ] 更新 exhaustive match、序列化和 SDK/public DTO 边界。

### A3. 事件与 diagnostic identity

- [ ] 增加 agent_spawn_requested、agent_spawned、agent_status_changed。
- [ ] 增加 agent_result_ready、agent_result_delivered。
- [ ] 增加 agent_interrupted、agent_closed。
- [ ] 定义 relation event 必填 payload，不在事件中保存 task prompt 全文或敏感 capability。
- [ ] 为恢复 diagnostic 预留稳定 code。

### A 验收

- [ ] 新旧 primary Session round-trip。
- [ ] 非法 parent/root/delegation 组合被拒绝。
- [ ] waiting_child 保持 active Run，Session 投影为 running。
- [ ] terminal/result/delivery 时间和 identity validator 完整。
- [ ] 现有 Session、Run、tool batch 测试全部通过。

退出条件：只有类型和状态骨架变化，不创建或执行 child。

## 里程碑 B：Delegation 持久化与布局

### B1. Store 端口

- [ ] 定义 create/load/save/list Delegation 端口。
- [ ] 支持按 root、direct parent、ancestor、child_session_id 查询。
- [ ] 支持 expected revision compare-and-save。
- [ ] 支持 pending delivery 和 incomplete spawn 扫描。
- [ ] 支持 archive/trash descendant 检查。
- [ ] query DTO 不泄漏本地路径。

### B2. 本地布局

- [ ] 在 root Session 下创建 delegations/<delegation_id>.json。
- [ ] 使用同目录 temporary、flush/fsync、atomic replace。
- [ ] 校验 delegation/root id 后再拼接路径。
- [ ] 建立可重建 lookup/index；索引失败不回滚 canonical relation。
- [ ] 增加 dirty marker 或复用现有 Session index repair 机制。
- [ ] 导出 Session tree 时包含 relation manifest，不包含敏感 capability 原文。

### B3. 布局版本

- [ ] 明确 data_layout_version 从 3 到下一版本。
- [ ] 实现一次性、单向、可重复运行的迁移，或按开发期政策明确拒绝旧布局。
- [ ] 不在正常 load 路径散布兼容分支。
- [ ] child Session trash/root trash 的文件归属有确定规则。
- [ ] migration 中断后可重试，不产生半个 relation index。

### B4. 创建协议的持久化基础

- [ ] parent_tool_call_id/Host request_id 唯一约束。
- [ ] 同一 idempotency key 重试返回同一 Delegation。
- [ ] requested relation 可以没有 child_session_id，其余阶段校验更严格。
- [ ] relation revision 保护 status/result/delivery 并发写。
- [ ] 建立 reconciliation 所需的阶段标记。

### B 验收

- [ ] atomic write 各失败点测试。
- [ ] Windows sharing conflict 沿用本地 I/O 原语。
- [ ] index 删除后可从 canonical files 重建。
- [ ] 重复 create/save 不重复 identity。
- [ ] corrupt、mismatch、missing child 查询产生类型化错误。
- [ ] layout migration/拒绝路径有测试。
- [ ] archive/trash 不遗留不可发现 relation。

退出条件：可独立创建、查询和恢复 DelegationRecord，但还不运行 child。

## 里程碑 C：Application 编排与 Host API

### C1. Product-owned AgentDefinition registry

- [ ] 在 composition root 显式装配 explorer、planner、worker、reviewer。
- [ ] registry 不扫描 workspace 或 user plugin。
- [ ] definition 生成稳定 contract hash。
- [ ] 新 Delegation 保存不可变 definition snapshot。
- [ ] definition 更新不改变已创建 Delegation。
- [ ] worker 初始能力保持保守；explorer/planner/reviewer 强制只读。

### C2. AgentOrchestrationService

- [ ] 实现 spawn_agent(request)。
- [ ] 实现 list_agents/read_agent。
- [ ] 实现 direct child/root tree 查询。
- [ ] spawn 前校验 parent active Run、workspace、model、depth、quota 和 capability。
- [ ] 按规格顺序写 requested relation、child Session、child Run 和 relation status。
- [ ] 通过 RuntimeSupervisor 提交已有 child Session。
- [ ] child Run 使用明确 origin、initiator 和 parent_run provenance。
- [ ] 任何异常保留可 reconciliation 的 durable 状态。

### C3. Host facade 与 DTO

- [ ] Application facade 暴露 spawn/list/read。
- [ ] mutation 使用 request_id。
- [ ] DTO 不返回完整 child prompt、secret 或内部 file path。
- [ ] Host API 与模型工具共享同一 service，不复制校验。
- [ ] 无窗口 fake Host 可以创建 child 并等待 terminal。

### C4. 最小 capability cap

在模型可见工具出现前完成：

- [ ] parent capability snapshot。
- [ ] AgentDefinition cap 求交。
- [ ] workspace read/write 上限。
- [ ] spawn agent_type allow/approval/deny。
- [ ] max_depth=1。
- [ ] root active children 上限。
- [ ] detached 强制拒绝。
- [ ] child session approval/interaction mode 固化。

### C 验收

- [ ] Host spawn explorer 完成独立 Session/Run。
- [ ] child 不复制完整父 history。
- [ ] duplicate request 返回同一 handle。
- [ ] 父没有 write 能力时不能创建 write-capable child。
- [ ] unknown agent type、超 depth、超 quota 有类型化结果。
- [ ] child failure 不破坏父 Session canonical history。
- [ ] Plugin 或 Host 不能绕过 AgentOrchestrationService 直接创建合法 Delegation。

退出条件：通过 application API 可完成单层 child 闭环，但模型尚看不到 Agent 工具。

## 里程碑 D：内置控制工具与 Foreground Join

### D1. 控制工具协议

- [ ] 定义 AgentControlRequest 和 AgentControlResult。
- [ ] 定义 AgentWaitRequest，不能复用 ToolInputRequest 假装用户交互。
- [ ] ToolDefinition 增加 agent_control kind/effect 或语义等价类型。
- [ ] ToolExecutor 继续负责 schema、权限、ToolPolicy、approval。
- [ ] application/tool_calls 负责执行 control intent。
- [ ] 工具层不得 import RuntimeService、RuntimeSupervisor 或 AgentOrchestrationService 实现。

### D2. Builtin tools

- [ ] SpawnAgent。
- [ ] WaitAgent。
- [ ] ListAgents。
- [ ] ReadAgent。
- [ ] SendAgentMessage。
- [ ] InterruptAgent。
- [ ] CloseAgent。
- [ ] 名称进入 builtin reserved set，插件/MCP 冲突明确失败。
- [ ] 工具输出有界，默认不返回完整 child history。
- [ ] WaitAgent 和 lifecycle mutation 使用 sequential/exclusive batch behavior。

### D3. Durable foreground wait

- [ ] WaitAgent 保存 AgentWaitCheckpoint。
- [ ] ToolCall 进入 waiting_child。
- [ ] parent Run 进入 waiting_child/subagent_join。
- [ ] RuntimeService 返回 waiting outcome，不保持 tool coroutine。
- [ ] Supervisor 释放 worker/permit。
- [ ] child terminal 触发 checkpoint resolution。
- [ ] child result 作为原 WaitAgent ToolResult 提交。
- [ ] parent Run 在 session lock 内恢复，再由 Supervisor 重新调度。
- [ ] all/any/timeout 语义。
- [ ] 结果顺序按 wait request，而不是 child completion 时间。

### D4. Permit 与调度

- [ ] 明确 active execution permit 与 durable active Run 分离。
- [ ] waiting_child/waiting_user/waiting_approval 不占 permit。
- [ ] max_concurrency=1 可运行 parent -> child -> parent。
- [ ] 多 parent 同时等待不会耗尽 worker。
- [ ] parent resume 与新 child submit 经过同一公平队列。
- [ ] shutdown 不把 parked wait 标 completed。

### D 验收

- [ ] 模型可 spawn explorer 并 WaitAgent 得到 ToolResult。
- [ ] max_concurrency=1 无死锁。
- [ ] wait all/any/timeout。
- [ ] parent abort while waiting 的 checkpoint 与 child cascade 状态正确。
- [ ] Tool approval rejection 形成 error ToolResult，不创建 child。
- [ ] illegal tool args 不创建 relation。
- [ ] WaitAgent 恢复后同一 tool_call_id/turn_id 不变。
- [ ] provider continuation 与 tool result 顺序保持现有协议。

退出条件：模型侧 foreground 子智能体完整可用，且等待不占 worker。

## 里程碑 E：Root 事件树、消息流与 HITL

### E1. AgentTreeSnapshot

- [ ] 定义 AgentTreeNode、AgentTreeEdge、AgentTreeSnapshot。
- [ ] node 组合 Delegation 与 child RuntimeActivitySnapshot。
- [ ] 汇总 active/queued/waiting/terminal、usage 和 pending interaction。
- [ ] root projection revision 独立于 per-session event sequence。
- [ ] snapshot 可从 Store/Session/Run 重建，不依赖内存 registry。

### E2. Root subtree subscription

- [ ] subscribe_agent_tree(root_session_id)。
- [ ] 初始 snapshot 后发送增量 notification。
- [ ] 新 child 动态加入订阅。
- [ ] terminal/close child 停止 live delta 但保留历史节点。
- [ ] child text/reasoning/tool events 可按 child stream 打开。
- [ ] root stream 默认只发有界状态/关系摘要，不复制完整 transcript。
- [ ] 有界队列、lag diagnostic、detach/reconnect。
- [ ] Host detach 不 abort child。

### E3. InteractionInbox

- [ ] 汇聚 descendant ApprovalRecord。
- [ ] 汇聚 descendant ToolInputRequest。
- [ ] DTO 标注 agent/task/session/run/tool/source。
- [ ] resolve request 验证 child session/run/tool identity。
- [ ] 错答到 sibling/parent 明确拒绝。
- [ ] foreground child HITL 时 parent 继续 waiting_child。
- [ ] background child HITL 不改变 parent Run。
- [ ] simultaneous child requests 不丢失、不串答。
- [ ] headless/unattended 使用确定性 policy，不无限等待。

### E4. Runtime activity

- [ ] waiting_child activity 和 parent-child cause。
- [ ] child waiting approval/user 在 tree 中可见。
- [ ] terminal result ready 与 delivered 分开显示。
- [ ] reconnect 后 UI 不需从相邻 delta 猜状态。
- [ ] redaction 覆盖 task、tool args 和 capability snapshot。

### E 验收

- [ ] fake Desktop adapter 可实时显示 tree 和打开 child stream。
- [ ] 两个并行 child 事件互不串线。
- [ ] child 审批在根 inbox 显示来源并正确恢复。
- [ ] AskUser answer 不能提交到错误 child。
- [ ] 慢 subscriber 不阻塞 runtime。
- [ ] detach/reconnect 后 snapshot 与 canonical 一致。
- [ ] child stream 可显示 text/reasoning/tool/HITL/terminal。

退出条件：任意 Host 可完整感知和控制 child，不需要读取本地文件。

## 里程碑 F：Background、结果交付与生命周期

### F1. Background spawn

- [ ] execution_mode=background。
- [ ] SpawnAgent 立即返回 handle。
- [ ] parent Run 可继续调用模型/工具。
- [ ] background child 受相同 capability、quota 和 HITL 规则。
- [ ] 模型工具说明禁止轮询，List/Read 仍可显式查询。
- [ ] child completion 立即产生 result_ready。

### F2. Exact-once result delivery

- [ ] DelegationResult 原子提交。
- [ ] delivery_id 稳定。
- [ ] notify_only。
- [ ] next_boundary 映射 Host steer。
- [ ] follow_up 映射 Host follow-up。
- [ ] resume_waiter 只供 foreground。
- [ ] 使用 producer=host、system_synthetic、稳定 host_source_id。
- [ ] queue/message 提交后才确认 delivered。
- [ ] crash before/after queue/message/delivery update 的所有窗口可恢复。
- [ ] 绝不写 role=user。

### F3. Send、resume、interrupt、close

- [ ] active child SendAgentMessage 使用 steer。
- [ ] idle child follow-up 创建新 child Run。
- [ ] resume 同一 child Session，不覆盖旧 Run。
- [ ] interrupt queued/running/waiting child。
- [ ] close 只关闭控制生命周期，不删除 history。
- [ ] terminal child 可 ReadAgent 获取结构化 result。
- [ ] late event 不能把 closed/terminal relation 恢复为 running。

### F4. Bound cascade

- [ ] parent abort/failed 级联 bound child。
- [ ] cascade 有 deadline 和 diagnostic。
- [ ] child unknown outcome 仍保留。
- [ ] root archive/trash 拒绝 active descendants 或显式级联。
- [ ] direct parent close 不删除 descendant history。
- [ ] 首版 detached 对模型和普通 Host 配置保持禁用。

### F 验收

- [ ] parent foreground work 与 background child 同时运行。
- [ ] background result 恰好一次进入父上下文。
- [ ] 进程在每个 delivery 窗口退出后恢复不重复。
- [ ] send active/idle 两条路径。
- [ ] interrupt/close/resume identity 不混淆。
- [ ] parent abort cascade。
- [ ] result ready 但未 delivered 可查询且可重试。
- [ ] Host 只订阅状态也不会丢失最终结果。

退出条件：foreground/background、控制和结果联动形成完整闭环。

## 里程碑 G：恢复、权限、预算与安全加固

### G1. Startup reconciliation

- [ ] relation 无 child。
- [ ] child 无 relation或 mismatch。
- [ ] relation 有 child 无 Run。
- [ ] child active 无 live job。
- [ ] child terminal 无 result。
- [ ] result pending delivery。
- [ ] parent wait checkpoint 与 child terminal 不一致。
- [ ] reconciliation 幂等且产生稳定 diagnostic。
- [ ] 不可安全修复时 recoverable/failed，不删除证据。

### G2. 权限完整化

- [ ] platform/parent/definition/workspace/revocation intersection。
- [ ] agent_type spawn permission patterns。
- [ ] foreground/background/lifecycle 权限。
- [ ] write child、network、MCP、external path 下放限制。
- [ ] once/child_session/subtree/root_session approval scope。
- [ ] 默认 child_session。
- [ ] child 运行期间收紧权限，未执行调用重新求值。
- [ ] user/builtin plugin 不能提升 child capability。
- [ ] auto mode 不能突破 hard deny。
- [ ] Spawn approval 与 child tool approval 独立。

### G3. 上下文与资源

- [ ] task_only 默认。
- [ ] summary/selected_messages/bounded_history。
- [ ] bounded_history 强制 message/token limit。
- [ ] resource refs 验证 parent/child 可达性并显式复制引用。
- [ ] 不自动复制 credential、approval answer、插件私有 context。
- [ ] child result resource refs 在进入父 Session 前重新验证。
- [ ] ContextView 预算包含 child 注入内容。

### G4. 预算与公平性

- [ ] per-child turn/token/time/tool budget。
- [ ] root aggregate budget。
- [ ] max active/queued children。
- [ ] max depth=1。
- [ ] writer concurrency=1。
- [ ] budget exhaustion paused/aborted，不标 completed。
- [ ] completed result 在预算耗尽后仍可交付。
- [ ] 多 root 调度公平，不由单一 root 占满 worker。
- [ ] usage 汇总不重复计算 resume/follow-up。

### G5. 安全与架构

- [ ] Agent control tools 不能被 plugin/MCP 覆盖。
- [ ] architecture test 禁止 tools/plugins/Host import application implementation。
- [ ] plugin 没有 AgentOrchestrationService handle。
- [ ] child prompt、event、error、DTO 执行脱敏。
- [ ] workspace path 和 shell 继续走现有 ToolExecutionContext。
- [ ] 并行 writer 冲突策略明确。
- [ ] unknown_outcome 不自动重放。
- [ ] root delete/archive 明确显示 descendant scope。

### G 验收

- [ ] 按规格完整故障注入矩阵。
- [ ] parent hard deny 越权负向测试。
- [ ] unattended/headless 无悬挂。
- [ ] 敏感父历史不进入 child。
- [ ] resource 不产生悬空引用。
- [ ] root quota/fairness 压力测试。
- [ ] 全量重启恢复不重复 spawn、tool 或 delivery。
- [ ] 架构依赖测试保护所有边界。

退出条件：安全、恢复和资源治理达到当前 Runtime/Tool 同等级别。

## 里程碑 H：CLI/Host 验收与发布收口

### H1. CLI/Application 控制面

建议提供：

- [ ] agent list/show。
- [ ] agent spawn。
- [ ] agent wait。
- [ ] agent send。
- [ ] agent interrupt。
- [ ] agent close。
- [ ] agent tree。
- [ ] agent interactions。
- [ ] JSON 输出稳定 DTO 和错误码。
- [ ] 非交互模式遇到未授权审批确定性失败。

命令名称可按现有 CLI 风格调整，但必须调用 application facade，不读取 canonical files。

### H2. Host adapter contract

- [ ] 无窗口 adapter 测试 root snapshot/subscription。
- [ ] child stream 增量加载。
- [ ] interaction source badge 所需字段完整。
- [ ] stop/open/send 控制返回最新 revision。
- [ ] refresh/reconnect 不重复内容。
- [ ] UI 不需要自己实现 cascade、permission 或 delivery。

### H3. 配置与启用

- [ ] 开发期间可用临时 composition-root gate 避免暴露半成品工具。
- [ ] 完成前移除临时双路径和兼容分支。
- [ ] 决定完成版是否默认注册 Agent control tools。
- [ ] 配置 max_depth、max children、budget、allowed agent types。
- [ ] 首版 detached/nested 明确拒绝而不是静默忽略。
- [ ] builtin AgentDefinition 可查询但不可被 user plugin 覆盖。

### H4. 文档转正

- [ ] 更新 core-runtime 的 waiting_child 当前行为。
- [ ] 更新 sessions-persistence 的 layout/recovery。
- [ ] 更新 tools-hitl 的 Agent control tool 和 HITL。
- [ ] 更新 resources-supervisor 的 parked permit/root scheduling。
- [ ] 更新 CLI/Host 规格。
- [ ] 子智能体规格由“目标”改为“当前”。
- [ ] future-platform 移除已完成占位，只保留后续能力。
- [ ] 高级智能体能力计划更新状态。
- [ ] 项目概述和开发架构只描述已经实现的能力。

### H 验收

- [ ] explorer foreground 端到端。
- [ ] worker permission approval 端到端。
- [ ] background exact-once 端到端。
- [ ] child AskUser 根路由端到端。
- [ ] interrupt/restart/recoverable 端到端。
- [ ] CLI 与 fake Desktop 对同一 tree 显示一致状态。
- [ ] 全局质量门槛通过。
- [ ] 文档链接和状态无“目标/当前”冲突。

退出条件：功能可以默认提供，规格、实现和可重复测试一致。

## 测试矩阵

### 正常路径

- Host spawn、模型 SpawnAgent；
- foreground wait all/any；
- background notify/next-boundary/follow-up；
- child stream；
- child follow-up/resume；
- completed result/read/close。

### 并发

- max_concurrency=1；
- 多 root、多 child；
- parent 与 background child 并行；
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
- agent type deny；
- write/network/MCP/external path；
- spawn approval 与 tool approval；
- approval scopes；
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

### 结果重复

风险：child 完成与 Host/进程重试造成父上下文重复结果或重复 follow-up。

控制：稳定 delivery_id、queued input id、canonical message 去重和 result/delivery 分离。

### 上下文污染

风险：复制父历史或回灌 child 中间日志使主上下文膨胀。

控制：task_only 默认、显式选取 context、根 tree 只投影状态、父上下文只接收结构化结果。

### 插件耦合

风险：把调度实现为 builtin plugin，为访问 RuntimeService 引入反向 host.call。

控制：AgentOrchestrationService 固定属于 application；插件首版不能贡献 definition 或取得 service。

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
- documentation link/state checks。

## 首版完成条件

只有同时满足以下条件，计划才可标记完成：

- Session/Run/Delegation/Result 都有 durable identity。
- foreground/background 使用同一 spawn 原语。
- waiting parent 不占 execution permit。
- child stream、root tree 和 HITL 可由任意 Host 使用。
- 权限、workspace、预算不超过父级上限。
- background result exact-once。
- resume context 不伪装 execution resurrection。
- restart、cancel、failure 不产生重复执行、孤儿 child 或悬空 resource。
- 插件和前端均未获得 Runtime/Supervisor 内部控制权。
- 所有强制规格条款有当前代码或自动化测试证据。
- 相关“当前”规格、项目概述和开发架构已更新为真实行为。

# 子智能体规格

状态：目标

## 目标与范围

本规格定义 OURO 子智能体的领域模型、运行语义、持久化、控制面、事件投影、HITL、权限和恢复契约。
子智能体是 application/runtime 的一等平台能力：模型可通过内置控制工具派遣，Host 也可通过 application
facade 直接操作；CLI、Desktop、API 或其他 Host 不拥有子智能体业务状态机。

本规格尚未由当前代码完整实现。实现完成前，不得据此宣称 OURO 已支持子智能体。

## 设计目标

- 子智能体复用现有 Session、Run、Message、Event、ToolBatch、Approval、AskUser 和恢复语义。
- 同一套非阻塞 spawn 原语覆盖前台等待、后台运行、并行派遣和后续批量编排。
- 父子委派具有 durable identity、明确来源、权限上限、预算和结果交付状态。
- child 的完整消息与工具流可独立订阅；根 Host 可展示有界的整棵 Agent 活动树。
- Host detach、进程退出、审批等待和失败不会伪造完成、重复副作用或丢失 canonical 状态。
- 子智能体权限不超过父级可下放能力；审批只能处理 ask，不能突破 hard deny。
- 新能力保持 core、application、tools、plugins 和 Host 的既有依赖方向。

## 非目标

首版不包括：

- 分布式队列、远程 worker 集群或跨设备接管；
- 宿主进程崩溃后透明复活原 Python coroutine、子进程或未知外部副作用；
- 通用 DAG、工作流 DSL、长期 Goal/Task 调度器或任意 swarm 语言；
- 默认复制完整父 Session history；
- 默认允许多个 child 在共享工作区并发修改相同文件；
- 让 user plugin 直接取得 RuntimeService、RuntimeSupervisor、SessionStore 或 AgentOrchestrationService；
- 把子智能体结果或 Host 控制消息伪装为真实用户输入；
- 为子智能体建立第二套模型循环、工具执行器、审批系统、事件协议或 SessionStore。

## 核心术语

### Agent definition

AgentDefinition 是可选择的能力模板，不是运行实例。它描述 agent type、说明、指令、模型策略、上下文策略、
工具能力上限、workspace 策略、预算和是否允许继续派遣。

### Delegation

Delegation 表示父 Session/Run 因一个明确任务创建或继续一个 child Session 的 durable 关系。
Delegation 不是 Session branch：

- branch 复制父历史以探索另一条会话分支；
- delegation 创建隔离的工作上下文，并把结果交回父级；
- forked_from 字段只表达 branch lineage；
- delegation_id 只表达派遣关系，不能从标题、消息文本或 fork lineage 反推。

### Child session 与 child run

- 每个 child 是普通 Session，拥有稳定 session_id 和独立 canonical history。
- 每次执行是普通 Run；同一 child Session 可通过新的 Run 接收 follow-up 或显式 resume。
- 恢复同一 child context 不会改写旧 Run，也不表示旧执行协程仍然存活。

### Agent handle

AgentHandle 是 Host 或模型控制面使用的有界 DTO，至少含 delegation_id、child_session_id、当前
child_run_id 和状态。Handle 是控制引用，不是 canonical 运行事实。

## 强制架构不变量

1. 子智能体执行不得在 Tool handler 内递归调用 RuntimeService。
2. child 继续使用正常 RuntimeService/InnerAgentLoop；不得创建 SubagentLoop 分叉实现。
3. 并发、等待、取消、结果交付和恢复由 application/RuntimeSupervisor 编排。
4. 模型工具只产生类型化 Agent control request；只有 application 可以创建 Session/Run、修改 Delegation
   或排队 Host synthetic input。
5. user plugin 和 builtin plugin 都不得直接持有 RuntimeService、SessionStore、Supervisor 或可变 Agent registry。
6. 同一 child Session 同时最多一个 active Run；根 Session 与不同 child Session 可并发运行。
7. foreground 等待是 spawn 后的 durable join，不是另一套执行路径。
8. waiting child、waiting approval 和 waiting user 不占用模型/工具执行 permit。
9. Delegation、child Session、child Run、result delivery 和 interaction 都有可重建的 durable identity。
10. child terminal 状态不能由 UI、内存 task、进程退出码或缺失事件单独推断。
11. background result 必须保留 Host/Agent provenance，不能写成 role=user。
12. 权限求值先应用不可突破的 capability cap，再应用 auto/approval/deny。
13. 恢复不得自动重放 running 或 unknown_outcome 的 child 工具调用。
14. Host 只能通过 application facade/DTO 订阅和控制 child，不直接读取 delegation 文件。
15. 前端关闭、刷新或重新连接不得改变 child 生命周期。

## 架构边界

    Model or Host
          |
          | Spawn / Wait / Send / Interrupt / Close / List
          v
    application AgentOrchestrationService
          |
          +---- DelegationStore port
          +---- SessionService / RuntimeService
          +---- RuntimeSupervisor
          +---- Interaction projection
          |
          v
    child Session + child Run
          |
          v
    ordinary OURO model/tool/interaction events
          |
          +---- child stream
          +---- root AgentTreeSnapshot

模块职责：

- ouro.core：稳定领域模型、状态枚举和类型化 control/result，不依赖 application 或 Host。
- ouro.application：AgentOrchestrationService、控制 DTO、根树投影、interaction 汇聚和恢复协调。
- ouro.sessions：DelegationStore/SessionStore 端口及本地 canonical 持久化。
- ouro.tools：模型可见的内置 Agent 控制工具定义、schema 和普通 ToolExecutor 权限闸门。
- ouro.plugins：未来可贡献 AgentDefinition 或策略，但不拥有调度、持久化或 child 生命周期。
- Host：展示 DTO、订阅事件、提交类型化命令，不实现状态迁移。

## AgentDefinition

AgentDefinition 至少包含：

| 字段 | 契约 |
|---|---|
| agent_type | 稳定、唯一、可用于权限匹配的 identity |
| name、description | Host 展示名与模型选择说明 |
| instructions | child developer instructions，不包含父级秘密或未经选择的完整历史 |
| model_policy | 固定模型、允许模型集合或 inherit |
| context_policy | task_only、summary、selected_messages 或显式 bounded_history |
| tool_capabilities | 工具、命令、路径、网络、MCP 和资源访问上限 |
| workspace_policy | inherit、read_only、workspace_write 或显式隔离工作区 |
| can_spawn | 是否可创建下一层 child |
| default_budget | turn、token、elapsed time、tool call 上限 |
| contract_hash | 本次定义内容的稳定摘要 |

首版 product-owned definitions 由 application composition root 显式装配，不扫描 workspace。建议内置：

- explorer：只读、无嵌套，用于搜索和证据收集；
- planner：只读、无 shell/write，用于方案设计；
- worker：限定 workspace write，危险工具继续审批；
- reviewer：只读，输出结构化 findings。

AgentDefinition 更新只影响新 Delegation。Delegation 必须保存实际使用 definition 的 identity、contract hash 和
有效策略快照；恢复不能按同名新 definition 猜测旧执行契约。

## DelegationRecord

DelegationRecord 是 canonical record，建议字段如下：

| 分组 | 字段 |
|---|---|
| identity | delegation_id、schema_version、root_session_id |
| parent | parent_session_id、parent_run_id、parent_tool_call_id、request_id |
| child | child_session_id、active_child_run_id、agent_type、agent_contract_hash、nickname |
| task | description、prompt、context_policy、selected message/resource refs |
| execution | execution_mode、lifecycle_binding、depth、status |
| policy | capability_snapshot、approval_mode、interaction_mode、budget |
| result | terminal child run、summary、message/resource refs、usage、delivery |
| lifecycle | created_at、updated_at、started_at、ended_at、closed_at、error |

execution_mode：

- foreground：父 Run 建立 durable wait，child terminal 后恢复父 Run；
- background：spawn 返回后父 Run 可继续，child 结果按 delivery policy 交付。

lifecycle_binding：

- bound：父 Run/Session 终止时按规则级联 child；默认值；
- detached：父 Run 结束后 child 可继续；必须显式允许并受更严格权限与预算控制。

DelegationStatus：

    requested
      -> queued
      -> running
      -> waiting_user | waiting_approval
      -> completed | failed | aborted | recoverable
      -> closed

child Run 是执行状态权威；Delegation status 是关系生命周期与 Host 查询所需的 durable projection。恢复时必须以
child Run、pending interaction 和 result record 对账，不以进程内 job 状态覆盖 canonical 状态。

### Result 与交付

DelegationResult 至少包含：

- child_session_id、child_run_id；
- terminal status；
- final summary；
- result_message_id 和 resource refs；
- changed file/diff refs（存在时）；
- usage；
- result_ready_at；
- delivery_id；
- delivery_policy；
- delivery_status：pending、delivered、skipped 或 failed；
- delivered_at 和目标 parent Run/message/queue identity。

result terminal 与 result delivery 是两个状态。child completed 不等于结果已经进入父上下文。

## Session 与 Run 扩展

### Session

SessionRecord 增加：

- session_kind：primary 或 subagent，默认 primary；
- delegation_id：subagent Session 必填，primary Session 必须为空。

现有字段继续使用：

- root_session_id：整棵 Session tree 根；
- parent_session_id：直接父 Session；
- forked_from_message_id、forked_from_run_id：只表示 branch lineage。

subagent Session 必须满足：

- root_session_id 指向根 primary Session；
- parent_session_id 与 DelegationRecord.parent_session_id 一致；
- delegation_id 指向同一 root 下存在的 DelegationRecord；
- workspace、model 和策略来自 Delegation 的冻结快照，不从当前父 Session 动态猜测。

### Run

RunStatus 增加 waiting_child，并纳入 active Run 集合。RunPhase 增加 subagent_join。

session_status_for_run 将 waiting_child 映射为 running，保持 SessionStatus 的粗粒度，不把子智能体 UI 状态扩张进
SessionStatus。

Run 等待 child 时保存 AgentWaitCheckpoint，至少包含：

- wait_id；
- parent tool call/batch/turn identity；
- delegation ids；
- completion_condition：all 或 any；
- timeout/deadline；
- state：pending 或 resolved；
- resolved delegation ids；
- resolution reason 和 resolved_at。

等待 checkpoint 属于原父 Run。child 完成后恢复同一父 Run，不创建假的用户 Run。

## 持久化布局与 Store 端口

默认本地布局在现有 Session 数据下增加：

    sessions/
      <root_session_id>/
        session.json
        delegations/
          <delegation_id>.json
      <child_session_id>/
        session.json
        messages.jsonl
        events.jsonl
        runs/

Delegation 由 root Session 拥有。child Session 只保存 delegation_id 引用，不复制完整 relation。

SessionStore 或独立 DelegationStore 端口至少支持：

- create/load/save delegation；
- list direct children、descendants 和 root tree；
- compare-and-save revision；
- load pending result deliveries；
- load incomplete spawn records；
- archive/trash 前检查 active descendants；
- 按 child_session_id 反查 Delegation。

Delegation JSON 使用同目录临时文件、flush/fsync 和原子 replace。索引可以重建，不是 canonical transaction。

新增布局必须显式提升 data_layout_version 或提供一次性、可验证的单向迁移；不得在热路径用兼容分支猜测旧数据。

## Spawn 提交协议与幂等

spawn 不是跨多个 Session 文件的单一原子写，因此采用 durable、可重放的提交协议：

1. 校验 parent Session/Run、AgentDefinition、depth、并发、预算、context refs 和权限。
2. 使用 parent_tool_call_id 或 Host request_id 生成/查找稳定 idempotency key。
3. 创建 status=requested 的 DelegationRecord。
4. 创建带 session_kind=subagent、root/parent/delegation identity 的 child Session。
5. 将 child_session_id 写回 Delegation。
6. 创建 child queued Run，并写 active_child_run_id。
7. 通过 RuntimeSupervisor submit 已存在 child Session 的 Run。
8. 将 Delegation 投影为 queued/running 并发布关系事件。

任一步骤重试时必须复用已有 identity。不得因 Host 超时、模型重试或恢复重复创建 child。

启动 reconciliation 处理：

- relation 存在、child Session 缺失；
- child Session 存在、relation 缺失或不一致；
- relation 有 child、没有 Run；
- child terminal、result 未建立；
- result ready、delivery 未确认；
- child active、没有 live Supervisor job。

无法无歧义修复时标记 recoverable/failed 并产生 diagnostic，不静默删除。

## 模型控制工具

模型侧使用 product-owned 内置控制工具：

- SpawnAgent：创建 child，立即返回 AgentHandle；
- WaitAgent：等待一个或多个 child，必要时挂起原父 Run；
- ListAgents：列出当前 root 或 direct children 的有界状态；
- ReadAgent：读取 child 的有界摘要或最终结果，不默认加载完整 history；
- SendAgentMessage：向 child active Run steer，或为 idle child 创建 follow-up；
- InterruptAgent：中断当前 child Run；
- CloseAgent：关闭 terminal/recoverable delegation 的控制生命周期。

工具名称属于 builtin reserved names，user plugin 和 MCP 不能覆盖。

这些工具继续经过普通 ToolExecutor：

- schema validation；
- permission evaluation；
- ToolPolicy；
- ToolCallBatch persistence；
- approval；
- event 与 result commit。

控制工具不直接持有 RuntimeService。工具定义产生类型化 AgentControlRequest，由 application
AgentOrchestrationService 执行。WaitAgent 返回类型化 AgentWaitRequest，使 ToolCall/Run 进入 durable
waiting_child；不得用一个长生命周期 Tool handler coroutine 占住 worker。

Host 可通过 application facade 调用相同控制语义，不需要伪造模型 ToolCall。Host request 必须有 request_id、
actor/provenance 和相同 capability 检查。

## Foreground 执行

foreground 是以下组合：

1. SpawnAgent 返回 handle。
2. 父模型调用 WaitAgent，或 convenience Agent tool 在 application 层展开为 spawn + wait。
3. WaitAgent 的 ToolCall 进入 waiting_child，父 Run 进入 waiting_child/subagent_join。
4. RuntimeService 返回 durable waiting outcome，Supervisor worker/permit 被释放。
5. child terminal 后，application 在父 Session lock 内解析 checkpoint。
6. child result 作为原 WaitAgent ToolResult 提交到原 tool batch。
7. 父 Run 重新进入 running，并由 Supervisor 重新调度后续模型轮次。

即使 max_concurrency=1，该流程也不得死锁。waiting child 不计入 active execution permits，但仍计入 active Run 和
root budget。

父等待多个 child 时：

- all：全部 terminal 后恢复，结果保持请求顺序；
- any：首个满足条件的 child 后恢复，其余 child 按 lifecycle policy 继续或取消；
- timeout：只返回已完成结果和未完成 handles，不把未完成 child 标为 completed。

## Background 执行与结果联动

background spawn 成功后立即返回 handle。父 Run 不进入 waiting_child。

child terminal 时：

1. 原子保存 DelegationResult 和 delivery_status=pending。
2. 发布 agent_result_ready。
3. 立即更新 AgentTreeSnapshot。
4. 根据 delivery policy 决定是否进入父上下文。
5. canonical message/queue 已提交后，将 delivery 标为 delivered。

DeliveryPolicy：

- notify_only：只更新 Host，父模型显式 ReadAgent/WaitAgent 获取；
- next_boundary：父 Run active 时以 Host steer 在安全 turn 边界交付；
- follow_up：父 Run terminal/idle 后创建 Host-origin follow-up；
- resume_waiter：只用于 foreground wait。

next_boundary/follow_up 应复用现有 durable queued input：

- producer=host；
- host_source_id=ouro.subagents/<delegation_id>；
- role=system_synthetic；
- queue id 与 delivery_id 稳定关联；
- 内容包含 child/delegation/run provenance；
- 不携带未经父 Session 验证的用户 resource refs。

恢复时若 queue id 已进入 canonical messages，只确认 delivery，不重复追加或启动 Run。

## 上下文隔离与结果契约

child 默认只获得：

- task prompt 与 description；
- AgentDefinition instructions；
- workspace 与模型策略；
- 显式选择的 message summaries；
- 显式授权且属于目标 Session 可达范围的 resource refs；
- 必需的项目规则、Skills 和工具 contract。

默认 context_policy 为 task_only。完整父 history 不作为默认值。bounded_history 必须显式配置消息上限和 token
预算；secret、credential、approval answer 和未选择的 system/plugin context 不因 parent relation 自动复制。

child final result 建议结构化包含：

- summary；
- evidence/message/resource refs；
- changed files/diff refs；
- verification；
- unresolved questions；
- terminal status；
- usage。

父 Agent 把 child 输出视为有来源的协作者结果，不视为系统真相；执行写操作或高影响结论前仍需验证。

## 权限与能力继承

### 两阶段求值

有效能力先求不可突破的上限：

    platform hard cap
      intersection parent delegated cap
      intersection AgentDefinition cap
      intersection workspace/resource constraints
      intersection current root/session revocations
      = candidate capability

之后才应用现有 auto、approval、deny：

- deny 永远拒绝；
- approval 产生 ApprovalRecord；
- auto 只跳过 ask，不扩大 candidate capability；
- tool-controlled permission 继续不可被 Session 模式绕过。

父 Agent 只能下放自己拥有的能力。AgentDefinition、child Session 配置、一次审批、插件 ToolPolicy 或模型请求都不能
扩大父 capability。

### Spawn 权限

创建 child 本身是独立权限，至少控制：

- 允许的 agent_type pattern；
- foreground/background；
- bound/detached；
- max_depth；
- root/direct child 数；
- write-capable AgentDefinition；
- 可下放的 shell、network、MCP 和 external path 能力。

Spawn approval 与 child 后续工具 approval 是不同请求。批准 spawn 不等于批准 child 的危险工具。

### 授权 scope

Host 对 child 工具审批支持：

- once；
- child_session；
- subtree；
- root_session。

默认 scope 为 child_session。只有用户显式选择时才能传播到 subtree/root。权限在 child 运行期间被收紧时，尚未执行
的调用必须重新求值；已经 running 的外部调用按取消与 unknown outcome 规则处理。

## HITL

ApprovalRecord 和 ToolInputRequest 继续保存在请求所属 child Session/Run/ToolCall 中，是 canonical interaction。

application 提供 root InteractionInbox projection，将所有 descendant pending interaction 汇聚给 Host。每项至少显示：

- agent_type、nickname、task description；
- foreground/background；
- child session/run/tool call；
- tool、参数摘要、workspace 和影响；
- approval scope；
- requested_at。

Host 回答必须携带原 child session_id、run_id、tool_call_id/request_id。application 在 child Session lock 内验证后
恢复该 child Run；答案不能写入父 Run 或其他 waiting child。

foreground child 等 HITL 时，父 Run 继续保持 waiting_child。background child 等 HITL 时，父 Run 不被阻塞。

无可交互 Host 时：

- interaction_mode=unattended 使用现有 UnattendedResolution 或 AgentDefinition 的 fail/skip/default 策略；
- 新 approval 无法展示时确定性 deny/fail；
- 不允许后台 child 无限期静默等待；
- 超时决议必须持久化 resolved_by=host_policy 和稳定 resolver_id。

## 取消、关闭、恢复与级联

### Interrupt

- 先持久化 child Run abort_requested/aborted，再通知进程内 AbortSignal。
- queued child 可在未执行前直接 aborted。
- running tool 遵循现有进程树取消和 unknown_outcome 语义。
- interrupt 只终止当前 child Run，child Session 与 Delegation history 保留。

### Close

Close 表示 Delegation 不再接收新的 follow-up，并释放进程内控制资源。Close 不删除 child Session、消息、Run 或结果。
active child 必须先 interrupt 或达到 terminal/recoverable；不能以 close 伪造 completed。

### Resume

- resume context：同一 child Session 创建新 Run，记录 resumed_from_run_id/delegation provenance；
- recover execution：当前不承诺恢复旧 coroutine；
- recoverable/unknown_outcome 需要用户或明确 Host policy 决定 retry；
- retry 创建新 tool/run identity，不能覆盖旧记录。

### 级联

- bound：父 Run aborted/failed 时默认 interrupt 未 terminal child，并有限等待；
- detached：不随父 Run terminal，但仍属于 root tree、预算和 Host 控制；
- 根 Session archive/trash 时不得留下 active descendant；必须拒绝或显式级联；
- direct parent close 不删除 descendant history；
- root delete 的范围必须在 Host 确认中明确显示。

## 并发、深度与预算

默认目标配置：

- max_depth=1；
- max_active_children_per_root=4；
- detached 默认禁用；
- 并行 writer 默认禁用或限制为 1；
- child budget 必须小于等于 root 剩余预算。

预算至少覆盖：

- 每 child turn/input/output token；
- elapsed time；
- tool call 数；
- root aggregate token/time；
- active/queued child 数；
- nested depth。

root budget 耗尽时：

- 不接受新 spawn；
- active child 在下一个 durable 边界 paused 或按 policy 中断；
- 已完成结果仍可交付；
- 不把预算停止标为成功。

Supervisor 的执行 permit 只覆盖实际 model/tool work。waiting_child、waiting_user、waiting_approval 不占 permit。
队列必须防止一个 root 长期占满全部 worker；公平性至少以 root/session 为边界。

## 事件与前端投影

### 关系事件

新增关系生命周期事件：

- agent_spawn_requested；
- agent_spawned；
- agent_status_changed；
- agent_result_ready；
- agent_result_delivered；
- agent_interrupted；
- agent_closed。

关系事件不复制 child text/tool delta。child 继续在自己的 Session event stream 中产生现有事件。

每个 Host-facing Agent event/snapshot 可投影：

- root_session_id；
- parent_session_id；
- delegation_id；
- child_session_id；
- agent_type、nickname、depth；
- execution_mode、lifecycle_binding。

上述 scope 优先从 Session/Delegation join 得到，不要求在每个高频 canonical delta 中重复保存。

### Root subscription

application 提供 root subtree subscription：

- 订阅开始时返回 AgentTreeSnapshot；
- 新 child 创建后自动加入；
- child close/archive 后移除 live subscription，但历史节点仍可查询；
- 每 child 保留自己的 event sequence；
- root projection 使用独立 revision，不把多个 Session sequence 假装成单一 durable 全序；
- 慢订阅者使用有界队列和 lag diagnostic，不反向阻塞 child。

### AgentTreeSnapshot

至少包含：

- root_session_id 和 projection revision；
- nodes、delegation edges；
- active/queued/waiting/terminal 汇总；
- pending interaction 摘要；
- root/child usage 与 budget；
- 每 node 当前 RuntimeActivitySnapshot。

Host 默认显示任务、状态、当前工具、elapsed 和控制入口；用户可打开 child Session 渲染完整 text/reasoning/tool
流。根 transcript 不自动混入 child 中间日志。

前端只拥有展开、选择、滚动和临时过滤等 UI 状态。状态迁移、级联、权限和结果交付不能在前端实现。

## 插件边界

子智能体调度与内置控制工具不是普通 builtin plugin：

- AgentOrchestrationService 属于 application；
- reserved Agent control tools 由 composition root 注册；
- durable child 不归 PluginGeneration owner；
- 禁用、升级或崩溃某个插件不能删除或接管 child Session。

未来可新增 AgentDefinitionContribution，但必须满足：

- definition 经不可变 Extension Catalog lease 冻结到 Delegation；
- user plugin 只能提出更窄能力模板，不能取得 Host 调度对象；
- contribution disable 后，旧 Delegation 按冻结 contract 恢复或明确 blocked，不按同名新 definition 替换；
- plugin tool 要派遣 child 也必须返回类型化 intent，并经过 Spawn 权限；
- AgentDefinitionContribution 不是首版实现前置条件。

## 错误、恢复与诊断

启动恢复顺序：

1. 运行现有 Session/Run recovery，将无 live lock 的 active child Run 转 recoverable。
2. 将 child running tool calls 转 unknown_outcome。
3. 扫描 incomplete Delegation 提交协议。
4. 从 child Run/interaction/result 对账 Delegation status。
5. 重建 pending result delivery。
6. 重建 AgentTreeSnapshot 和 InteractionInbox。
7. 仅在显式 Host/model 请求后 resume/retry，不自动重放 child 工作。

必须产生类型化 diagnostic 的情况：

- delegation_child_missing；
- child_delegation_mismatch；
- delegation_run_missing；
- child_contract_unavailable；
- child_policy_invalid；
- result_delivery_incomplete；
- descendant_active_on_archive；
- cascade_timeout；
- root_budget_exhausted；
- subagent_subscription_lag。

事件、错误和 DTO 继续执行现有脱敏；不包含 credential、完整敏感工具参数、父 Session 未选择历史或本地私有路径。

## Host/Application API

application facade 至少提供：

- spawn_agent；
- wait_agents；
- list_agents；
- read_agent；
- send_agent_message；
- interrupt_agent；
- close_agent；
- agent_tree_snapshot；
- subscribe_agent_tree；
- list_agent_interactions；
- resolve_agent_approval；
- resolve_agent_input。

所有 mutation 接口使用稳定 request_id 或 expected revision，返回类型化 DTO/diagnostic。Host 不需要 import core record、
SessionStore 或 RuntimeSupervisor 内部 job。

## 验收标准

本规格进入“当前”前，至少需要以下自动化证据：

- domain 模型与 schema 验证测试；
- LocalSessionStore delegation 原子写、幂等、索引重建和布局迁移测试；
- spawn 每个崩溃窗口的 reconciliation 测试；
- max_concurrency=1 的 foreground join 无死锁测试；
- background result exact-once delivery 测试；
- child resume 创建新 Run、旧 Run 不被覆盖测试；
- parent abort 的 bound cascade 与 detached 对照测试；
- child approval/AskUser root routing 与错配拒绝测试；
- headless/unattended 不悬挂测试；
- parent hard deny 不可被 child/auto/plugin 放宽的负向测试；
- root/subtree/child approval scope 测试；
- child context 不默认复制完整父 history 与敏感信息测试；
- root subscription 重连、lag 和 snapshot 重建测试；
- Host detach 不终止 child 测试；
- unknown_outcome 不自动重放测试；
- reserved tool name 与插件架构边界测试；
- CLI 或无窗口 Host 的完整 spawn/wait/interrupt 恢复集成测试。

## 延后能力

以下能力必须在首版闭环稳定后单独设计和验收：

- nested depth 大于 1；
- per-child git worktree 与结果合并；
- AgentSwarm/batch map；
- durable Goal/Task tree 自动调度；
- remote worker/process isolation；
- AgentDefinition plugin contribution；
- 跨根 Session 共享 child；
- 进程重启后自动重启明确声明为 replay-safe 的 child 工作。

## 相关规格与计划

- [核心运行时](core-runtime.md)
- [会话与持久化](sessions-persistence.md)
- [工具与人机协同](tools-hitl.md)
- [资源与后台运行监督](resources-supervisor.md)
- [插件系统](plugin-system.md)
- [个人智能体平台能力](future-platform.md)
- [子智能体实施计划](../plans/subagents.md)
- [子智能体设计范式调研与目标方案](../../docs/OURO_子智能体设计范式调研与目标方案_2026-07-22.md)（非规范性背景）

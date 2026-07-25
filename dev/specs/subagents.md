# 子智能体规格

状态：当前

## 目标与范围

本规格定义 OURO 子智能体的领域模型、运行语义、持久化、控制面、事件投影、HITL、权限和恢复契约。
子智能体是 application/runtime 的一等平台能力：模型可通过内置控制工具派遣，Host 也可通过类型化
application service/DTO 直接操作；CLI、Desktop、API 或其他 Host 不拥有子智能体业务状态机。

当前实现已经完成首版框架闭环：Run 冻结 AgentDefinition 目录、durable Delegation、独立 child Session/Run、
Host/CLI 与模型控制面、sync/async、durable join、exact-once 结果交付、HITL 路由、取消级联、恢复协调、
显式 spawn/capability 权限求交、执行边界动态撤销、共享预算上限，以及可重建且可动态订阅的 root
AgentTree/interaction projection。本规格只陈述已经实现并有自动化证据的首版语义；延后能力集中列在文末，不能
从当前闭环外推。

## 设计目标

- 子智能体复用现有 Session、Run、Message、Event、ToolBatch、Approval、AskUser 和恢复语义。
- 同一套非阻塞 spawn 原语覆盖同步等待、异步运行、并行派遣和后续批量编排。
- 父子委派具有 durable identity、明确来源、权限上限、预算和结果交付状态。
- child 的完整消息与工具流可独立订阅；根 Host 可展示有界的整棵 Agent 活动树。
- Host detach、进程退出、审批等待和失败不会伪造完成、重复副作用或丢失 canonical 状态。
- 子智能体权限不超过父级可下放能力；审批只能处理 ask，不能突破 hard deny。
- OURO 提供不可编辑、不可删除的通用 AgentDefinition，并允许用户管理自定义 definition；两者进入同一目录、
  校验、冻结、派遣和恢复管道。
- AgentDefinition 目录与 SpawnAgent（Host 展示名“派遣子智能体”）的 agent_id 参数从同一 Run 快照生成，避免配置、
  工具 schema 与实际解析结果漂移。
- 新能力保持 core、application、tools、plugins 和 Host 的既有依赖方向。

## 非目标

首版不包括：

- 分布式队列、远程 worker 集群或跨设备接管；
- 宿主进程崩溃后透明复活原 Python coroutine、子进程或未知外部副作用；
- 通用 DAG、工作流 DSL、长期 Goal/Task 调度器或任意 swarm 语言；
- 默认复制完整父 Session history；
- 为共享工作区写入增加路径锁、单写者配额或自动合并协议；
- 让 user plugin 直接取得 RuntimeService、RuntimeSupervisor、SessionStore 或 AgentService；
- 允许模型通过普通工具创建、修改或删除 AgentDefinition；definition 管理由 Host application API 承担；
- 把子智能体结果或 Host 控制消息伪装为真实用户输入；
- 为子智能体建立第二套模型循环、工具执行器、审批系统、事件协议或 SessionStore。

## 核心术语

### Agent definition

AgentDefinition 是可选择的能力模板，不是运行实例。它描述稳定 agent_id、说明、指令、模型策略、
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
7. sync 等待是 spawn 后的 durable join，不是另一套执行路径。
8. waiting child、waiting approval 和 waiting user 不占用模型/工具执行 permit。
9. Delegation、child Session、child Run、result delivery 和 interaction 都有可重建的 durable identity。
10. child terminal 状态不能由 UI、内存 task、进程退出码或缺失事件单独推断。
11. async result 必须保留 Host/Agent provenance，不能写成 role=user。
12. 权限求值先应用不可突破的 capability cap，再应用 auto/approval/deny。
13. 恢复不得自动重放 running 或 unknown_outcome 的 child 工具调用。
14. Host 只能通过 application service/DTO 订阅和控制 child，不直接读取 delegation 文件。
15. 前端关闭、刷新或重新连接不得改变 child 生命周期。
16. agent_id 是持久化和工具参数使用的稳定 identity；可编辑 name 不能作为引用、权限键或恢复依据。
17. builtin 与 user AgentDefinition 只在管理面的 editable 属性上不同；AgentService 不按来源选择执行分支。
18. AgentDefinition 配置、SpawnAgent schema 和执行解析必须来自同一个 Run-frozen AgentCatalogSnapshot。
19. tool_selection=all 只扩大候选可见集合，不授予权限，也不能包含当前 depth 禁止的 Agent 控制工具。

## 架构边界

    Model or Host
          |
          | Spawn(sync|async) / Send / Interrupt / Close / List
          v
    application AgentService
          |
          +---- AgentDefinitionService -> config + AgentCatalogSnapshot
          +---- DelegationRepository port
          +---- SessionService / RuntimeService
          +---- narrow RunScheduler port -> RuntimeSupervisor
          +---- AgentTree / interaction projection
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
- ouro.config：`agents.definitions` 中的 user AgentDefinition 配置、原子 CRUD 和严格校验。
- ouro.application：AgentDefinitionService 管理定义/目录/兼容性，AgentService 编排 child；同时提供控制 DTO、根树投影、
  interaction 汇聚和恢复协调。
- ouro.sessions：组合进 SessionStore 的 DelegationRepository 端口，以及文件和 SQLite 两个 canonical 实现。
- ouro.tools：模型可见的内置 Agent 控制工具定义、schema 和普通 ToolExecutor 权限闸门。
- ouro.plugins：未来可贡献 AgentDefinition 或策略，但不拥有调度、持久化或 child 生命周期。
- Host：展示 DTO、订阅事件、提交类型化命令，不实现状态迁移。

## AgentDefinition

AgentDefinition 是严格、不可变且可序列化的值对象，同一个类型同时用于 product-owned registry 和
Delegation 中的冻结副本；首版不再定义字段近似相同的 AgentDefinitionSnapshot。它至少包含：

| 字段 | 契约 |
|---|---|
| agent_id | 稳定、全局唯一的 identity；创建后不可修改，不从 name 推导恢复 |
| name、description | Host 展示名与 SpawnAgent 模型选择说明；可编辑但不是 identity |
| system_prompt | child 专用指令，不包含父级秘密或未经选择的完整历史 |
| tool_selection | `all`，或 `selected` + 一组 ToolRegistry public names；selected 允许空集 |
| model_policy | 固定模型、允许模型集合或 inherit |
| workspace_policy | inherit、read_only、workspace_write 或显式隔离工作区 |
| can_spawn | 是否可创建下一层 child |
| default_budget | 复用 RunBudgetPolicy 的 model round、token 和 elapsed time 上限 |
| contract_hash | 本次定义内容的稳定摘要 |

system_prompt 是用户面名称，但进入上下文时必须位于 OURO platform/base system instructions 之后的专用 Agent 指令
边界，不能替换或删除平台安全规则、工具协议和 provenance 规则。全文可保存在用户配置和私有 Run
AgentCatalogSnapshot 中以支持恢复，但模型上下文只向被选 child 投影；它不进入 SpawnAgent 描述、普通目录事件或根
AgentTreeSnapshot。

### Builtin 与 user definition

首版至少内置一个 `agent_id=general` 的通用子智能体：

- name 和 description 明确表示通用任务能力；
- system_prompt 由 OURO 代码提供并进入同一 contract hash；
- tool_selection=all；
- model/workspace/budget 使用受约束的 inherit；
- can_spawn=false，与首版 max_depth=1 一致；
- 永远存在，不能被 user definition 覆盖、编辑或删除。

用户可以创建、读取、更新和删除自定义 definition。管理 DTO 投影 `source=builtin|user` 和派生字段 editable；builtin
的 editable=false，user 的 editable=true。delete 与 update 对 editable=false 返回类型化错误。若用户希望修改 builtin，
Host 提供“复制为自定义”操作并生成新的 agent_id。

source/editable 不进入 AgentDefinition contract hash、capability 计算或 child prompt；它们只保护配置 mutation 和帮助 Host
渲染。内容相同的 builtin/user definition 必须得到相同的运行语义。

除管理命令是否允许修改外，两类 definition 没有运行时差异：它们使用同一个 AgentDefinition 类型、validator、
contract hash、AgentCatalogSnapshot、SpawnAgent schema 投影、capability 求交、Session/Run 创建、HITL、事件、恢复和
结果交付路径。AgentService 不允许出现 builtin/user 两套 spawn handler 或 policy 分支。

### 自定义配置与 identity

用户 definition 保存到共享 `config/config.json` 的权威分区：

    agents:
      revision: <monotonic integer>
      definitions:
        <agent_id>:
          name: ...
          description: ...
          system_prompt: ...
          tool_selection:
            mode: all | selected
            tool_names: [...]

builtin definition 不复制进用户配置；目录在读取时把 code-owned builtin 与 user section 合并。细粒度
AgentDefinitionService 提供 list/get/create/update/delete/copy_builtin 和 compatibility/tool-option 查询，底层
AgentDefinitionConfigManager 复用 JsonConfigFile 的跨进程锁、read-validate-write 和原子 replace。mutation 接受
expected revision，避免 Desktop/CLI 并发编辑丢失更新。

同一 `agents` 分区还保存首版 composition 配置：`runtime.max_depth=1` 和
`runtime.spawn_policy.allowed_agent_id_patterns=("*",)`。definition 与 runtime policy 共用一个 expected
revision，避免 Host 同时编辑时发生交叉覆盖。max depth 只能为 1，lifecycle 只能为 bound；配置
detached/nested 在校验期明确失败。runtime policy 由 `LocalApplication` 在 composition start 读取，变更报告
`effective_from=next_runtime`；definition 内容仍由每个新 parent Run 冻结并报告
`effective_from=next_run`。根软预算继续来自现有 `run_budget` 并冻结到 parent Run，每个 definition 的
subagent 默认值继续复用 `RunBudgetPolicy`，不增加第二套 AgentBudget 配置或执行引擎。

首版用户编辑面必须覆盖 name、description、system_prompt 和 tool_selection。model/workspace/budget/can_spawn
先由产品默认值归一化，只有已有权限模型能安全表达的字段才逐项开放；不接受任意 policy JSON。新增可编辑字段必须进入
同一 AgentDefinition validator、contract hash、Run snapshot 和 Delegation freeze，不得成为旁路配置。

AgentDefinitionService 与 AgentService 是两个真实变化轴：前者只管理系统级 definition 配置、编辑器选项和不可变目录
快照，后者只管理 Delegation/Session/Run 生命周期；它们共享 AgentDefinition validator/catalog resolver，不互相转发
全部 API，也不合并成大一统 facade。

create 返回稳定 agent_id；name 后续可自由修改而不改变已有引用。builtin id 和 reserved namespace 不能被 user
占用。字段数量、name/description/system_prompt 长度、definition 总数和 selected tool 数使用集中、可测试的产品上限，
避免配置文件、模型工具 schema 和 child 上下文无界增长。

删除或修改 user definition 只影响新的 AgentCatalogSnapshot：

- 已存在 Delegation 继续使用自身冻结 definition；
- 已存在 child Session 的 resume/follow-up 继续使用该 Delegation contract；
- active parent Run 继续使用其 Run-frozen catalog；
- 新 Run 不再看到已删除 definition，并看到更新后的 contract。

因此不需要为用户配置另建不可变 revision store 或 tombstone。Run snapshot 和 Delegation 已分别覆盖 active tool
contract 与已创建 child 的恢复需求。

### 工具选择与可用性

tool_selection 保存当前产品公开的 ToolRegistry name，不保存 display name、schema 副本或本地 owner 路径：

- all：选择本次 Run ExtensionCatalog 中对 child 可见的全部候选工具；
- selected：只选择 tool_names 与本次 Run 可见工具的精确交集；空列表表示无工具 Agent；
- 两种模式都继续与 platform hard cap、父级可下放约束、workspace、depth、当前 revocation 和现有
  auto/approval/deny、ToolPolicy 求交；
- all 不是 all permissions，不跳过 approval，也不自动允许 shell、network、MCP、external path 或 Agent control；
- can_spawn=false 或 depth 已达上限时，Spawn/Wait/Send 等 Agent control tools 必须从 child 工具视图移除。

创建或更新 selected definition 时，当前目录未知的 tool name 返回校验错误。之后因 plugin/MCP 禁用、移除或启动失败
而缺失的 tool 不得导致配置被删除或静默缩成更小 Agent；目录把该 definition 标为 unavailable，给出
missing_tool_names，并从新的 SpawnAgent agent_id 选项中排除。

AgentCatalogSnapshot 把配置 name 解析为 EffectiveToolRef；每个 ref 冻结 public name、canonical identity/owner、
execution contract hash、最低 permission mode，以及 network/MCP/external-path 等显式执行能力。硬 deny 工具不进入
候选 refs；approval 作为最低权限随 Delegation/child Run 冻结，后续配置只能继续收紧，不能把它放宽为 auto。恢复旧
Delegation 时使用冻结 refs 和既有 unknown-contract 规则，不能把当前出现的同名、不同 owner/contract 工具猜测为
原工具。

### AgentCatalogSnapshot 与派遣工具联动

每个新 parent Run 在取得 ExtensionCatalogLease 后构造并持久化一个有界 AgentCatalogSnapshot，包含：

- agents section revision 与 catalog contract hash；
- 本 Run 冻结的 AgentSpawnPolicy，包括 agent/model pattern、execution/lifecycle mode 与显式能力上限；
- 所有 selectable definition 的完整冻结值和 contract hash；
- 每项解析后的 EffectiveToolRefs 与 compatibility diagnostics；
- builtin/user 管理来源只用于 Host 展示，不参与执行分派。

SpawnAgent（Host 展示名“派遣子智能体”）必须从这个同一 snapshot 生成 ToolDefinition：

- agent_id 参数的 JSON Schema enum/oneOf 只包含 selectable definitions；
- 每个选项向模型提供有界的 agent_id、name 和 description；
- system_prompt、完整 tool list 和敏感 policy 不拼入工具描述；
- 执行 AgentControlRequest 时按 parent Run snapshot 解析 agent_id，不重新读取 current config；
- execution_mode schema 与 Host handler 都按同一 snapshot 校验；
- Run/Tool invocation 保存 agent catalog contract hash，Delegation 再嵌入实际选中的完整 definition 和 EffectiveToolRefs。

配置 CRUD 返回 effective_from=next_run。编辑或删除发生在 active Run 期间时，不热改本 Run 已发送给 Provider 的工具
schema；下一 Run 自动生成新参数，无需手写同步枚举、重启 Host 或修改 SpawnAgent 实现。

AgentDefinition 更新只影响基于新 AgentCatalogSnapshot 创建的 Delegation；active Run 即使随后 spawn，也继续使用其
旧 snapshot。Delegation 必须嵌入实际使用的完整 definition、contract hash 和本次派遣的显式约束；恢复不能按同名新
definition 猜测旧执行契约。

### 复杂度与边界评估

这项能力引入的最小新职责只有：一个 `agents` 配置 manager、一个有独立管理用例的 AgentDefinitionService、一个
Run-frozen AgentCatalogSnapshot，以及从 snapshot 投影 SpawnAgent schema 的纯函数。它们分别解决跨进程配置事务、
Host CRUD/工具兼容性、durable contract 和模型参数联动，具有真实且不同的变化轴。

首版明确不引入 AgentDefinition 专用数据库、不可变 revision store、热更新事件总线、第二套 ToolRegistry、手写派遣
枚举、builtin/custom runtime adapter 或新的权限系统。用户配置保存在现有 config.json；动态工具来自现有
ExtensionCatalog；运行冻结落在现有 Run；实际执行仍走 AgentService/RuntimeSupervisor/RuntimeService。由此新增能力
不会成为跨层 registry 或插件反向调用中心。

## DelegationRecord

DelegationRecord 是 canonical record，建议字段如下：

| 分组 | 字段 |
|---|---|
| identity | delegation_id、schema_version、root_session_id |
| parent | parent_session_id、parent_run_id、parent_tool_call_id、request_id |
| child | child_session_id、current_child_run_id、agent_id、agent_contract_hash、nickname |
| task | description、initial_message_id、prompt_digest、显式 resource refs |
| execution | execution_mode、lifecycle_binding、depth、spawn_state、control_state |
| policy | frozen AgentDefinition、EffectiveToolRefs、显式 workspace/model 约束、approval_mode、interaction_mode、RunBudgetPolicy |
| result | terminal child run、result message/resource refs、usage、delivery |
| lifecycle | created_at、updated_at、started_at、ended_at、closed_at、error |

execution_mode：

- sync：原 SpawnAgent ToolCall 建立 durable wait，child terminal 后恢复父 Run；
- async：SpawnAgent 立即返回 handle，父 Run 可继续，child 结果自动在安全边界交付。

lifecycle_binding：

- bound：父 Run aborted/failed 或 root 生命周期关闭时按规则级联 child；父正常完成后 async child 可继续；默认值；
- detached：保留的领域取值；当前首版在 config、Host request 和模型请求三个边界都明确拒绝。

Delegation 不保存一套与 child Run 同步变化的执行状态机。关系自身只保存：

- spawn_state：requested -> child_created -> submitted，任一步骤也可进入 failed；
- control_state：open 或 closed；
- 当前 child Session/Run identity；
- result delivery 的独立状态。

child Run 是 queued/running/waiting/completed/failed/aborted/recoverable 的唯一执行状态权威。Host 查询中的当前状态
由 Delegation 与 child Run、pending interaction、result record 联合投影，不在每个 Run 转换时回写关系状态。

### Result 与交付

DelegationResult 至少包含：

- child_session_id、child_run_id；
- terminal status；
- result_message_id 和 resource refs；
- changed file/diff refs（存在时）；
- usage；
- result_ready_at；
- delivery_id；
- delivery_policy；
- delivery_status：pending、delivered、skipped 或 failed；
- delivered_at 和目标 parent Run/message/queue identity。

最终文本从 result_message_id 指向的 canonical child message 读取；DelegationResult 不复制完整最终消息。只有产品
明确产生了独立的蒸馏摘要时，才以单独 message identity 保存，而不是在 relation 中再存一份文本。

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

SpawnAgent(sync) 在 application 层产生类型化 AgentWaitRequest，并把请求与解析结果保存在原 SpawnAgent
ToolBatchCall 上，至少包含：

- wait_id；
- delegation ids；
- completion_condition：all 或 any；
- timeout/deadline；
- state：pending 或 resolved；
- resolved delegation ids；
- resolution reason 和 resolved_at。

ToolBatchCall 已经携带 tool call、batch 和 turn identity，不得在 Run 上再建立一份重复这些 identity 的
AgentWaitCheckpoint。child 完成后解析原 ToolCall，恢复同一父 Run，不创建假的用户 Run。

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

DelegationRepository 作为后端中立端口组合进现有 SessionStore，至少支持：

- create/load/save delegation；
- list direct children、descendants 和 root tree；
- compare-and-save revision；
- 按 root 列举 Delegation，并由 application 在单个 root 内筛选 pending delivery 或 incomplete spawn；
- archive/trash 前检查 active descendants；
- 按 `(root_session_id, child_session_id)` 反查 Delegation；禁止在单次 Session 执行路径中扫描其他 root。

LocalSessionStore 的 Delegation JSON 使用同目录临时文件、flush/fsync 和原子 replace。索引可以重建，不是
canonical transaction。SQLiteSessionStore 必须在数据库事务中实现同一端口、identity、revision 和查询语义；
不能把文件实现包装成 SQLite 的旁路存储。

文件布局必须显式提升 data_layout_version 或提供一次性、可验证的单向迁移；SQLite 使用独立 schema version 和
事务迁移。两种后端都不得在热路径用兼容分支猜测旧数据，并必须通过同一组 repository contract tests。

## 运行组合与调度边界

LocalApplication 是本地 composition root，统一拥有 RuntimeService、RuntimeSupervisor、AgentDefinitionService 和
AgentService。AgentDefinitionService 从当前 ExtensionCatalog/ToolRegistry 构造 definition compatibility 与 Run catalog，
但不调度 child。为避免
RuntimeService -> AgentService -> RuntimeSupervisor -> RuntimeService 的实现依赖环：

- AgentService 只依赖 application-owned 的窄 RunScheduler Protocol，不导入具体 Supervisor 实现；
- RuntimeSupervisor 是首版唯一 RunScheduler 实现，不再新增 SubagentScheduler、worker pool 或公平调度层；
- Agent 控制工具只产生类型化 intent，由 ToolCallCoordinator 交给 AgentService；tools/core 不导入 application；
- LocalApplication 使用显式装配或有界的两阶段 attach 完成对象连接，不提供通用 reverse host.call；
- 任何向模型暴露 Agent 控制工具的标准 Host 路径都必须通过 RuntimeSupervisor；裸 RuntimeService 不注册这些工具，
  或在缺少 AgentService/RunScheduler 时 fail closed；
- CLI、Desktop 和未来 API 复用同一 supervised composition，不各自创建 Agent 调度路径。

RuntimeSupervisor.submit/job 必须区分 `DelegatedTaskInput` 与普通 `HostSessionInput`。child 初始任务携带稳定
`delegation_id` provenance，并以 `role=user` 进入 child canonical history；不得调用裸 `runtime.run(prompt)` 丢失
委派身份。普通 Host/插件输入仍为 `role=system_synthetic`，不能借委派入口伪装用户消息。

## Spawn 提交协议与幂等

spawn 不是跨多个 Session 文件的单一原子写，因此采用 durable、可重放的提交协议：

1. 从 parent Run 的 AgentCatalogSnapshot 按 agent_id 解析 definition，并校验 parent Session/Run、depth、预算、
   compatibility、显式 resource refs 和权限。
2. 使用 parent_tool_call_id 或 Host request_id 生成/查找稳定 idempotency key。
3. 创建 spawn_state=requested 的 DelegationRecord。
4. 创建带 session_kind=subagent、root/parent/delegation identity 的 child Session。
5. 将 child_session_id 写回 Delegation，并推进 spawn_state=child_created。
6. 创建 child queued Run，并写 current_child_run_id。
7. 通过 RuntimeSupervisor 提交已存在 child Session 的 Run，并传递 `DelegatedTaskInput` attribution。
8. 推进 spawn_state=submitted 并发布 agent_spawned；queued/running 从 child Run 投影。

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

模型侧首版使用最小的 product-owned 内置控制工具：

- SpawnAgent：按 agent_id 选择 Run-frozen AgentDefinition，携带 task、显式 resource refs 和 execution mode 创建 child；sync 模式
  挂起原调用并最终返回 child result，async 模式立即返回 AgentHandle 并自动交付结果；Host 可显示为“派遣子智能体”；
- SendAgentMessage：向 child active Run steer，或为 idle child 创建 follow-up；
- InterruptAgent：中断当前 child Run；

工具名称属于 builtin reserved names，user plugin 和 MCP 不能覆盖。

SpawnAgent 不接受 system_prompt、tool_selection、model/workspace capability 的临时自由文本覆盖；否则配置目录、schema
联动和权限审计会被绕过。若未来允许 per-dispatch override，只能提供明确、可求交且进入 Delegation contract hash 的
收窄字段。

list_agents、read_agent 和 close_agent 保留为 AgentService/Host API，供 Host/UI 读取子智能体状态与有界结果；
模型不持有状态查询工具，async 结果由框架在安全边界主动交付。只有出现独立模型用例后才增加新的模型工具。

这些工具继续经过普通 ToolExecutor：

- schema validation；
- permission evaluation；
- ToolPolicy；
- ToolCallBatch persistence；
- approval；
- event 与 result commit。

控制工具不直接持有 RuntimeService。工具定义产生类型化 AgentControlRequest，由 application
AgentService 执行。SpawnAgent(sync) 由 AgentService 返回类型化 AgentWaitRequest，使原 ToolCall/Run 进入 durable
waiting_child；不得用一个长生命周期 Tool handler coroutine 占住 worker。

Host 可通过 AgentService 调用相同控制语义，不需要伪造模型 ToolCall。Host request 必须有 request_id、
actor/provenance 和相同 capability 检查。

## Sync 执行

sync 是以下组合：

1. 父模型调用 SpawnAgent(execution_mode=sync)。
2. application 使用统一 spawn 原语创建 child，但不向模型提交中间 handle ToolResult。
3. 原 SpawnAgent ToolCall 进入 waiting_child，父 Run 进入 waiting_child/subagent_join。
4. RuntimeService 返回 durable waiting outcome，Supervisor worker/permit 被释放。
5. child terminal 后，application 先固化 DelegationResult，再在父 Session lock 内解析 checkpoint。
6. `handle + result` 作为原 SpawnAgent、原 tool_call_id 的 ToolResult 提交到原 tool batch。
7. 父 Run 重新进入 running，并由 Supervisor 重新调度后续模型轮次。

即使 max_concurrency=1，该流程也不得死锁。waiting child 不计入 active execution permits，但仍计入 active Run 和
root budget。

底层 AgentWaitRequest 继续保留可持久化的集合与完成条件，以支持恢复和未来 application 编排；当前每个模型侧
SpawnAgent(sync) 只等待自己创建的一个 delegation。Host 的 `wait_agents()` 可等待多个 handle，但不暴露为模型工具。

## Async 执行与结果联动

async spawn 成功后立即返回 handle。父 Run 不进入 waiting_child。

child terminal 时：

1. 原子保存 DelegationResult 和 delivery_status=pending。
2. 发布 agent_result_ready。
3. 立即更新 AgentTreeSnapshot。
4. 以 next_boundary 策略自动进入父上下文。
5. canonical message/queue 已提交后，将 delivery 标为 delivered。

DeliveryPolicy 是 application 内部持久化事实，不是模型参数：

- notify_only：Host 直接派遣使用，只更新 Host 投影；
- next_boundary：async 使用；父 Run active 时以 Host steer 在安全 turn 边界交付，父已 idle 时创建 Host-origin Run；
- resume_waiter：只用于 sync 原 ToolCall 恢复；每个结果的策略在结果固化时按当时状态确定：首个结果沿用派遣时
  冻结的策略，sync 派遣后续 follow_up Run 的结果在父 Run 未 aborted/failed 时按 next_boundary 交付。

next_boundary 应复用现有 durable queued input：

- producer=host；
- host_source_id=ouro.subagents/<delegation_id>；
- role=system_synthetic；
- queue id 与 delivery_id 稳定关联；
- 内容包含 child/delegation/run provenance；
- 不携带未经父 Session 验证的用户 resource refs。

恢复时若 queue id 已进入 canonical messages，只确认 delivery，不重复追加或启动 Run。

同一进程内对一个 Delegation 的结果固化、投递、中断、follow-up 和 close 必须串行化；跨调用竞争仍以 repository
revision compare-and-swap 为最终事实。CAS 失败只能在 canonical record 已保存同一 child Run result 或同一 delivery
identity 的 delivered 状态时按幂等成功收敛，其他版本冲突继续显式失败。只有结果 CAS 赢家发布 result-ready 事件。

## 上下文隔离与结果契约

child 初始任务完整文本只保存为 child 的第一条 canonical user message，并携带稳定 `delegation_id` provenance。Delegation 保存
initial_message_id 与 prompt_digest，不复制 prompt。child 的文本上下文只包含：

- OURO platform/base system instructions；
- 冻结的 AgentDefinition system_prompt；
- SpawnAgent task。

此外，child 的运行环境获得：

- workspace 与模型策略；
- 显式授权且属于父 Session 可达范围的 resource refs；
- 必需的项目规则、Skills 和工具 contract。

父 Session history、CompactionRecord、tool result、assistant tool call、reasoning/provider continuation 和
system/plugin synthetic message 均不投影到 child。模型工具与 Host API 都不接受 canonical message ids；主智能体必须
在 task 中显式写出 child 完成任务所需的文本上下文。这样只有一条派遣语义，不建立第二套上下文选择、摘要或消息引用协议。

资源不随父子关系隐式继承。Spawn 请求必须显式列出父 Session 已可达的 resource refs；ResourceService 在持有源
Session mutation boundary 时重新验证不可变内容并把带 `copied_from_session_id` 的引用附加到 child。child 初始
delegated-task user message 携带这些引用，因而资源内容和 Agent 专用 system prompt 一并进入正常 ContextView token 预算。
child 结果只收集 assistant/tool-result 输出引用，不回显初始输入引用；结果经 Host list、sync ToolResult 或 async delivery
进入父 Session 前，再从 child 到 parent 执行同样的可达性验证和显式引用复制，随后才写入父 canonical message。

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
      intersection AgentDefinition effective tool selection and constraints
      intersection workspace/resource constraints
      intersection current root/session revocations
      = candidate capability

之后才应用现有 auto、approval、deny：

- deny 永远拒绝；
- approval 产生 ApprovalRecord；
- auto 只跳过 ask，不扩大 candidate capability；
- tool-controlled permission 继续不可被 Session 模式绕过。

首版不引入通用 capability algebra、规则语言或新的权限引擎。允许的工具、命令、network/MCP 和最低权限模式
已经由冻结的 `EffectiveToolRefs` 完整表达；Delegation constraints 只保存 workspace 与 external path 这类不能由
工具引用独立表达、且恢复时确实需要的运行约束。实际工具调用继续进入现有 permission/ToolPolicy 边界，不再复制
`allowed_tool_names`、`allow_write`、`allow_network` 或 `allow_mcp` 等可由冻结目录推导的字段，也不保存可与当前
权限漂移的通用 capability blob。工具能力不参与 subagent 派遣并发决策。

父 Agent 只能下放自己拥有的能力。AgentDefinition、child Session 配置、一次审批、插件 ToolPolicy 或模型请求都不能
扩大父 capability。

当前实现先从 parent Run 的冻结 ToolRegistry 解析 exact EffectiveToolRefs，再由 AgentService 将 AgentSpawnPolicy 与
definition 结果求交并把最终目录持久化到 parent Run。插件 manifest 的进程/网络权限会进入 Host-owned tool metadata，
插件提供的同名 metadata 不能覆盖它；shell、MCP 和声明了相关 package 权限的工具据此受 network/external-path cap。
child Run 同时持久化 DelegatedConstraints；external path 未下放时，普通文件工具即使 Host 全局配置更宽也强制使用
workspace-only 执行上下文。

### Spawn 权限

创建 child 本身是独立权限，至少控制：

- 允许的 agent_id pattern；
- sync/async；
- lifecycle binding（当前只允许 bound）；
- max_depth；
- root/direct child 数；
- 是否允许向 child 下放写工具；
- 可下放的 shell、network、MCP 和 external path 能力。

Spawn approval 与 child 后续工具 approval 是不同请求。批准 spawn 不等于批准 child 的危险工具。

### 审批继承边界

首版复用现有逐 ToolCall ApprovalRecord 和 child Session 的 approval_mode，不新增 child_session/subtree/root_session
记忆型授权 scope。批准 Spawn 只允许创建该 child，不能批准它后续的危险工具；一次 child 工具批准也不能传播给
sibling、descendant 或 root。权限在 child 运行期间被收紧时，尚未执行的调用必须重新求值；已经 running 的外部调用
按取消与 unknown outcome 规则处理。记忆型 scope 只有成为全产品权限系统的独立需求后才能单独设计。

ToolCallCoordinator 在初次预检之外，还在 ToolBatchExecutor 为每个 invocation 真正调用 handler 前重新评估当前
ToolPolicy、Session mode、Host override 和冻结 permission floor。新的 deny 在 handler 前产生错误结果；新的 approval
回到原 ToolBatchCall 的 durable `awaiting_approval` 状态，批准后仍再次经过执行边界。批准记录始终绑定精确的
session/run/tool_call，SpawnAgent 的批准不会被 child 工具复用。

## HITL

ApprovalRecord 和 ToolInputRequest 继续保存在请求所属 child Session/Run/ToolCall 中，是 canonical interaction。

application 提供 root interaction inbox projection，将所有 descendant pending interaction 汇聚给 Host。它是从 child
ApprovalRecord/ToolInputRequest 构造的可重建查询视图，不是新的 canonical store、broker 或状态机。每项至少显示：

- agent_id、name、nickname、task description；
- sync/async；
- child session/run/tool call；
- tool、参数摘要、workspace 和影响；
- requested_at。

Host 回答必须携带原 child session_id、run_id、tool_call_id/request_id。application 在 child Session lock 内验证后
恢复该 child Run；答案不能写入父 Run 或其他 waiting child。

sync subagent 等 HITL 时，父 Run 继续保持 waiting_child；async subagent 等 HITL 时，父 Run 不被阻塞。长期运行的
Interactive Host 始终消费同一 root inbox：读取主终端输入时保持事件循环可运行，并同时等待 AgentTree 通知；
终端以单一行读取协调器拥有 stdin。subagent interaction 先到达时，Host 把当前提示重定向为审批或提问提示，
由同一个 reader 接收答案，不建立会竞争 stdin 的第二个读取；AskUser 空行不构成回答。处理完 pending interaction
后再建立新的主提示。sync 路径继续等待原父 Run 恢复，async 路径不改变父 Run 生命周期。Host 只缓存本次已提交的
request identity，以覆盖 application 接收决议与 canonical inbox 投影收敛之间的短暂窗口；identity 已消失时把旧
通知视为 stale，不缓存或推断 canonical pending 状态。interaction DTO 只给出有界参数预览，完整 ToolInvocation
仍留在 subagent Run。当前终端只渲染主智能体行动轨迹与 subagent HITL 提示，不渲染 subagent 回复或工具调用；
完整 subagent stream 保留给 Desktop 等树形 Host。

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
- detached：当前不执行此分支；请求在创建 relation 前失败；
- 根 Session archive 时不得留下 active descendant；trash 必须在取得全部 live Run 锁后显式级联完整 subagent tree；
- direct parent close 不删除 descendant history；
- root delete 的范围必须在 Host 确认中明确显示，且 lifecycle 路径不得依赖 Run/Delegation 反序列化。

## 并发、深度与预算

默认目标配置：

- max_depth=1；
- detached 默认禁用；
- subagent budget 必须小于等于 root 剩余预算。

首版每个 subagent 直接复用现有 RunBudgetPolicy，至少覆盖：

- 每 subagent model round/input/output token；
- elapsed time；

OURO 不增加 subagent 专用 active/queued 配额。每个通过派遣策略、深度、预算和幂等校验的 SpawnAgent 都创建
durable relation 并提交现有 RuntimeSupervisor；实际同时执行数量与排队顺序由 Supervisor 的通用
`max_concurrency` 和 FIFO 决定。深度在创建任何 relation 前固定拒绝大于 1。工具权限、effect 和 write capability
不参与派遣或调度；并行写冲突作为普通 ToolResult 回到各 subagent，由模型在原循环内调整。同一 Delegation 的
结果固化、投递、中断、follow-up 和 close 继续由 application lifecycle lock 与 repository CAS 串行化，不通过
工具 batch 的 `exclusive` 属性建立第二把调度锁。

每个 subagent Run 继续直接读取冻结的 `RunBudgetPolicy`。此外，InnerAgentLoop 在普通 model round durable
boundary 调用 application 提供的共享预算检查；AgentService 从 parent Run 与其 Delegation 对应的每个 subagent
Session/Run 聚合 usage 和 model round，按 root policy 返回同一 `RunBudgetReason`。因此并发 subagent 在下一个
边界进入现有 `paused` 路径，已完成结果不被改写。
聚合以 `(session_id, run_id)` 去重，resume/follow-up 新 Run 各计一次；AgentTree 使用相同 Run 粒度展示 root/node
usage。Supervisor 仍使用原全局 FIFO 与 execution permits，未增加 root-aware scheduler。

root budget 耗尽时：

- 不接受新 spawn；
- active child 在下一个 durable 边界 paused 或按 policy 中断；
- 已完成结果仍可交付；
- 不把预算停止标为成功。

Supervisor 的执行 permit 只覆盖实际 model/tool work。waiting_child、waiting_user、waiting_approval 不占 permit。
首版只复用现有跨 Session FIFO 和通用并发上限，不增加 root-aware 限流或公平调度器。只有出现可复现饥饿或
第二种 Supervisor 后端时，才单独演进调度策略端口。

## 事件与前端投影

### 关系事件

首版只新增无法从现有 child Run 事件无歧义表达的关系/交付事件：

- agent_spawned；
- agent_result_ready；
- agent_result_delivered；
- agent_closed。

spawn requested/failed 由 Delegation spawn_state 和原 ToolCall/Host command 结果审计；running/waiting/terminal/interrupt
继续使用 child Run 的现有事件。关系事件不复制 child status、text 或 tool delta，避免形成第二条运行事实流。

每个 Host-facing Agent event/snapshot 可投影：

- root_session_id；
- parent_session_id；
- delegation_id；
- child_session_id；
- agent_id、name、nickname、depth；
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

- AgentService 属于 application；
- builtin general 是 code-owned AgentDefinition，user custom 是 `agents.definitions` 产品配置；二者都不是
  PluginGeneration contribution；
- reserved Agent control tools 由 composition root 注册；
- durable child 不归 PluginGeneration owner；
- 禁用、升级或崩溃某个插件不能删除或接管 child Session。

未来可新增 AgentDefinitionContribution，但必须满足：

- definition 先经不可变 ExtensionCatalogLease 解析，再进入 Run-frozen AgentCatalogSnapshot 和 Delegation；
- user plugin 只能提出更窄能力模板，不能取得 Host 调度对象；
- contribution 若未来进入产品，必须先归一化为同一 AgentDefinition/AgentCatalogSnapshot，不能产生第三套 spawn 管道；
- contribution disable 后，旧 Delegation 按冻结 contract 恢复或明确 blocked，不按同名新 definition 替换；
- plugin tool 要派遣 child 也必须返回类型化 intent，并经过 Spawn 权限；
- AgentDefinitionContribution 不是首版实现前置条件。

ToolRegistry 将 Agent control names 视为 product-reserved：只有 `source=builtin` 且带 Host 生成标记的动态 overlay
可以注册，plugin/MCP 即使伪造 metadata 也会被拒绝。tools/plugins/sdk 不导入或命名 AgentService；CLI Host 通过
LocalApplication composition boundary 使用它。PluginContext 只暴露既有 data/credential/task/logger/tool
execution 能力，没有调度 handle。

管理面与执行面也保持分离。`source/editable` 只属于 AgentDefinition list/detail DTO，不进入 Core
AgentDefinition、contract hash 或 AgentService 分支；list 不返回 system_prompt，显式 get/edit 才返回全文。
child task 完整文本只存在 child canonical message 和目标 child ContextView；Delegation、handle、root tree、关系
event 与工具 schema 只包含 digest、稳定 identity 或安全 description。application control error 经过现有 redaction
边界。child shell/path 继续使用 ToolExecutionContext；delegated external-path cap 会强制 `workspace_only=true`。
并行写冲突不在 spawn 边界预测或阻止；工具执行失败作为标准 ToolResult 返回触发 child 自然调整，不增加工具名、
路径锁或 writer quota。

## 错误、恢复与诊断

启动恢复顺序：

1. 运行现有 Session/Run recovery，将无 live lock 的 active child Run 转 recoverable。
2. 将 child running tool calls 转 unknown_outcome。
3. 扫描 incomplete Delegation 提交协议。
4. 从 child Run/interaction/result 对账 current child identity、spawn state 和 delivery，不复制执行状态。
5. 重建 pending result delivery。
6. 重建 AgentTreeSnapshot 和 interaction inbox projection。
7. 仅在显式 Host/model 请求后 resume/retry，不自动重放 child 工作。

对账同时验证 Session 的 root/parent/delegation backlink、冻结 approval/interaction/workspace policy，以及 child Run 的
parent/delegation/root/agent/definition-contract provenance。只有一个 Run 能由这些 durable identity 唯一证明归属时，
才允许补齐 `current_child_run_id` 和 `spawn_state=submitted`；其余情况 fail-closed、保留证据并报告到 application
startup report。根归档判断 active descendant 时以 child Run 为事实源，不能以可能缺失或跨记录不一致的 result 代替。
当前可编辑 Agent config 与旧 snapshot 不同是 `effective_from=next_run` 的正常结果，不参与旧 Delegation 对账；恢复只校验
parent Run 冻结的 AgentCatalogSnapshot、Delegation definition/effective tools 和 child Run contract 三者的一致性。

必须产生类型化 diagnostic 的情况：

- agent_definition_invalid；
- agent_definition_id_conflict；
- builtin_agent_definition_immutable；
- agent_definition_tool_unavailable；
- agent_catalog_contract_unavailable；
- delegation_child_missing；
- child_delegation_mismatch；
- delegation_run_missing；
- child_contract_unavailable；
- child_policy_invalid；
- child_run_recovered；
- result_delivery_incomplete；
- parent_agent_wait_mismatch；
- descendant_active_on_archive；
- cascade_timeout；
- root_budget_exhausted；
- subagent_subscription_lag。

事件、错误和 DTO 继续执行现有脱敏；不包含 credential、完整敏感工具参数、父 Session 未选择历史或本地私有路径。

## Host/Application API

AgentDefinitionService 与管理 DTO 至少提供：

- list_agent_definitions / get_agent_definition；
- create_agent_definition / update_agent_definition / delete_agent_definition；
- copy_builtin_agent_definition；
- list_agent_definition_tool_options；
- agent_definition_catalog_snapshot / compatibility diagnostics。

AgentService 与 application DTO 至少提供：

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
SessionStore 或 RuntimeSupervisor 内部 job，也不依赖一个大一统 application facade。

CLI definition 命令通过不需要模型 Provider 的本地 composition 使用 AgentDefinitionService；运行命令构建完整
LocalApplication 并使用 AgentService。短生命周期 CLI 可从 completed parent Run 的冻结目录发起 unattended Host
delegation，并在退出前等待 terminal；模型 `SpawnAgent` 仍只接受当前 active parent。长期运行 Host 可在 active parent
上 spawn、保持 subscription，并按 child Session event cursor 增量打开完整流。Agent handle 携带 relation revision 与
control state；tree refresh/reconnect 从 canonical snapshot 重建，不要求 UI 缓存业务状态。
CLI 的 completed-parent spawn 固定 notify-only；会启动 parent next-boundary/follow-up Run 的 delivery policy 只由
长期运行 Host 或模型控制面使用。

## 验收标准

当前首版由以下自动化证据约束：

- domain 模型与 schema 验证测试；
- agents config 分区的原子 CRUD、expected revision、严格字段和产品上限测试；
- builtin general 永远存在、不可覆盖/编辑/删除，copy 生成新 user agent_id 测试；
- builtin/user definition 经过相同 validator、contract hash、spawn、权限、HITL 与恢复路径测试；
- name 修改不改变 agent_id，旧 Run/Delegation 不从 name 重新解析测试；
- all/selected/selected-empty 工具选择和 parent hard cap 求交测试；
- selected tool 后续缺失时 definition 保留但对新 Run unavailable，不静默降级测试；
- AgentCatalogSnapshot 重启恢复和 active Run 配置漂移测试；
- SpawnAgent agent_id schema 与同一 snapshot 自动联动、配置从 next_run 生效测试；
- system_prompt 只进入目标 child，不替换 base instructions、不泄漏到派遣 schema/事件/根树测试；
- LocalSessionStore delegation 原子写、幂等、索引重建和布局迁移测试；
- SQLiteSessionStore delegation 事务、revision、索引和 schema migration 测试；
- 两个后端共享的 DelegationRepository contract tests；
- spawn 每个崩溃窗口的 reconciliation 测试；
- max_concurrency=1 的 sync join 无死锁测试；
- async result exact-once delivery 测试；
- child resume 创建新 Run、旧 Run 不被覆盖测试；
- parent abort 的 bound cascade 与 detached 明确拒绝测试；
- child approval/AskUser root routing 与错配拒绝测试；
- headless/unattended 不悬挂测试；
- parent hard deny 不可被 child/auto/plugin 放宽的负向测试；
- child 逐调用审批不传播到 sibling/root 的负向测试；
- child 文本上下文严格只含 base/AgentDefinition instructions 与 task，不读取父 history/compaction 测试；
- root subscription 重连、lag 和 snapshot 重建测试；
- Host detach 不终止 child 测试；
- unknown_outcome 不自动重放测试；
- reserved tool name 与插件架构边界测试；
- 裸 RuntimeService 不暴露不可执行的 Agent 工具、标准 Host 统一经过 Supervisor 的架构测试；
- CLI 或无窗口 Host 的完整 spawn/wait/interrupt 恢复集成测试。

## 延后能力

以下能力必须在首版闭环稳定后单独设计和验收：

- nested depth 大于 1；
- per-child git worktree 与结果合并；
- AgentSwarm/batch map；
- durable Goal/Task tree 自动调度；
- remote worker/process isolation；
- AgentDefinition plugin contribution；
- 模型侧 AgentDefinition CRUD 工具；
- 可扩大 definition 的 per-dispatch system prompt/tool/model/workspace override；
- child_session/subtree/root_session 记忆型审批授权；
- 通用 capability algebra 或独立 AgentBudget 引擎；
- root-aware 公平调度策略；
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

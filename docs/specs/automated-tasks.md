# OURO 自动任务规格

> 文档类型：现行规格
> 范围：本地自动任务配置、计划调度、后台会话执行、状态查询与 TUI 交互

本规格定义自动任务的有效契约、状态边界、失败与恢复语义以及验收依据。使用入口见
[自动任务说明](../guides/automated-tasks.md)；公共运行、审批与会话观察行为分别遵循
[核心运行时](runtime.md)、
[工具与人机协同](tools-and-interactions.md)、
[资源与运行监督](resources-and-supervision.md)和
[会话观察](session-observation.md)规格。

## 1. 目标与范围

自动任务在计划时刻执行保存的目标。Agent 目标向指定工作空间中的 Session 提交指令，通过共用
Runtime/Supervisor 执行；插件目标调用显式声明可调度的服务方法。两类目标共用计划、单次领取、并发上限、
持久执行记录和调度所有权。用户无需保持任务菜单或目标会话打开。

当前能力以 OURO 进程存活为调度条件。关闭进程后不调度、不唤醒计算机，不注册操作系统计划任务或独立
daemon。任务之间的依赖图、工作流步骤、独立预算策略、自动失败重试和分布式调度不属于本规格；
[高级智能体规划](../plans/advanced-agents.md)中的依赖工作流是独立的未来能力。

## 2. 概念与身份

| 概念 | 身份与职责 |
|---|---|
| 任务配置 `TaskConfiguration` | 标题、执行计划 `schedule` 与执行目标 `target` |
| Agent 目标 `AgentTaskTarget` | `kind=agent`；指令、工作空间、模型、审批及会话创建方式 |
| 插件目标 `PluginTaskTarget` | `kind=plugin`；插件、服务、方法身份及 JSON 参数 |
| 任务 `TaskRecord` | 由 `task_id` 标识的长期配置实体，包含固定的 `owner_id`、修订号、启用状态、下次计划和活动执行关联 |
| 计划发生点 | 某任务在计划中应执行的时刻；使用 UTC 时刻与 `task_id` 共同去重 |
| 单次执行 `TaskExecution` | 由 `execution_id` 标识的一次已领取执行，记录 `task_id`、`owner_id`、发生点、已绑定目标 `target` 与结果状态 |
| Agent 执行目标 `AgentTaskRun` | `kind=agent`；一次领取所预留的精确 `session_id`、`run_id` |
| 插件执行目标 `PluginTaskCall` | `kind=plugin`；领取时的参数、发布、配置修订、执行契约及传递依赖身份 |
| 会话 `Session` | 持有消息、上下文、事件与交互；不以任务记录代替会话存储 |
| 运行 `Run` | 共用运行链上的一次执行，是运行结果、审批与取消状态的规范事实 |

任务、单次执行和会话必须保持独立身份。复用会话时，多次执行具有不同的 `execution_id` 和 `run_id`，可以
共享 `session_id`。Host 在任务会话内发送的后续指令遵循普通会话协议，不凭会话身份自动计作新的计划执行。
执行目标在领取事务中固定；结果提交不能修改目标身份。配置编辑作用于后续发生点，不重写已领取执行。
`TaskRecord.session_id` 是 Agent 任务用于复用和导航的会话关联；执行对账使用 `TaskExecution.target` 中的身份。

## 3. 配置契约

### 3.1 Agent 目标配置

| 配置 | 必填性与默认值 | 有效契约 |
|---|---|---|
| 标题 `title` | 可选 | 最多 160 字符；用于会话标题及任务展示。未填写时，将指令中的空白折叠后取前 60 字符作为展示标题 |
| 目标类型 `target.kind` | 必填，`agent` | 持久化记录必须显式声明受支持的目标类型 |
| 指令 `target.prompt` | 必填 | 去除首尾空白后为 1–100000 字符，保留内部换行；以用户角色提交，表达自然语言目标和要求 |
| 工作空间 `target.workspace` | 必填 | 必须是存在的绝对目录；保存时规范化路径，保存与派发时均校验 |
| 审批 `target.approval_mode` | 可选，默认 `afk` | 支持 `inherit`、`auto`、`approval`、`deny`、`afk`，沿用会话审批语义 |
| 模型 `target.model` | 必填 | 保存指定模型路由或符号值 `default`；后者必须有用户配置的系统默认端点，并在保存和每次派发时解析。TUI 新建任务在有系统默认端点时预选 `default`，否则要求用户指定端点；不从模型列表排序或当前会话的模型选择推断 |
| 思考 `target.reasoning` | 默认模型思考配置 | 按所选模型能力解析 `ReasoningRequest`；不支持的组合不得保存或执行 |
| 执行计划 `schedule` | 必填 | 包含频率、时区、起始边界、执行时刻和适用的重复参数 |
| 会话创建 `target.session_mode` | 可选，默认 `new` | `new` 每次执行创建新会话；`reuse` 复用关联会话，找不到该会话时重新创建 |

修改默认端点不改写已保存任务的模型选择；使用 `default` 的任务在下一次派发时采用新的默认端点，指定模型的任务保持原路由。编辑任务保留原有模型和思考请求；用户切换模型时，TUI 将思考
请求恢复为新模型默认值，再按新能力提供可选项。模型能力或工作空间在保存后失效时，派发必须报告执行失败，
不得静默换用其他模型或目录。

`afk` 使用共用会话设置 `approval_mode=auto` 与 `interaction_mode=unattended`。自动任务不另行定义审批
绕过规则。执行需要人工审批时，原 Run 挂起，用户进入会话后通过共用审批入口处理。

### 3.2 执行计划字段

| 字段 | 契约 |
|---|---|
| `frequency` | `hourly`、`daily`、`weekdays`、`weekly`、`monthly`、`custom` |
| `timezone` | 有效 IANA 时区；模型层默认 `UTC`，TUI 新建表单显式使用系统本地 IANA 时区，编辑保留已保存值 |
| `starts_at` | 必填，带时区的开始时刻；新建或改变计划时必须存在可执行发生点 |
| `ends_at` | 可选，带时区且不早于开始时刻；边界包含该时刻 |
| `at` | 本地 `HH:MM`，不含时区、秒或微秒；每小时只使用分钟，自定义分钟/小时使用开始时刻而非此字段 |
| `interval` | 自定义重复间隔，严格整数 `1–10000`；非自定义固定为 `1` |
| `unit` | 自定义重复单位：分钟、小时、天、周、月；非自定义保持模型默认值 `days` |
| `weekdays` | 不重复的星期值 `0–6`，周一为 `0`；适用于每周或自定义日历周期，不适用于自定义分钟/小时 |
| `month_day` | 可选严格整数 `1–31`，仅用于每月或自定义按月重复；未设置时使用开始日期中的日 |

TUI 中未带偏移的日期时间按选定时区解释。结束字段只填写日期时，包含该日期整天；不存在的本地时间必须
给出校验错误。时区通过可搜索选项页选择，不能要求用户自由填写时区标识。

### 3.3 插件目标配置

`TaskConfiguration` 的标题和执行计划适用于所有目标。`target.kind=plugin` 使用 `plugin_id`、`service`、
`method` 与 `arguments`，不包含模型、工作区或会话设置。目标方法必须在 Manifest 服务导出中声明
`schedulable=true`；注册方法不可调度，提供者必须具有 `background.run` 权限。参数按方法的 JSON Schema
在保存和领取前校验。未填写标题时显示插件、服务和方法身份。

Host 通过 `TaskService` 创建和管理配置。方法实现使用既有 `ServiceContribution`，在 `ServiceCall.scheduled`
读取 Host 分配的 `task_id`、`execution_id` 和 UTC `scheduled_at`；普通服务调用的该字段为 `None`。
这些身份可供业务去重或记录同步游标，框架不承诺外部副作用 exactly-once。插件返回的 JSON 保存为执行的
`result`；错误和中断保存明确状态，不创建虚假的 Session／Run。

### 3.4 插件所有权与公开端口

`owner_id` 记录创建任务的插件，Host 创建的任务为 `None`。所有权独立于执行目标：插件可以拥有 Agent 任务，
也可以调度自己或直接声明的依赖服务。所有权在创建后不可变，每次领取的执行记录必须使用相同 owner。

公开 `PluginContext.scheduler` 和 worker 的 `WorkerScheduler(peer)` 提供 `create`、`get`、`list`、`update`、
`set_enabled`、`delete`、`executions`、`interrupt`。Host 注入创建者身份；插件不能指定 owner，查询和控制只能
访问自己的任务。分页先按 owner 筛选，再按更新时间与任务身份排序。Host 的原生管理入口可以管理全部任务。

写操作要求 `background.run`，拥有 Agent 目标还要求 `sessions.write`。只读回调可以查询，不能改变计划或中断
执行；未发布候选和已撤销代次不能使用调度端口。修改与控制携带正整数 `expected_revision`，过期修订报告冲突。
中断只提交持久请求，由调度 owner 结算实际执行，不假定调用者就是执行所在 Host。

每次 `create` 创建独立任务；取消或传输失败不保证写入未发生，调用者应先查询已保存计划再决定下一步，SDK 不自动
重试创建。任务 owner 不可用或缺少派发权限时，在领取前暂停计划并保存原因。owner 停用触发本 Host 调度唤醒，
在途 Agent 与服务执行由共用对账中断；其他 Host 通过共享状态刷新观察变更。服务提供者与任务 owner 可以不同，
中断 owner 的任务不要求停用仍可独立使用的提供者。重新启用插件不会自动启用已暂停的计划。

## 4. 计划与派发语义

### 4.1 日历规则

| 计划 | 发生点规则 |
|---|---|
| 每小时 | 所选时区每个日历小时的指定分钟 |
| 每天 | 每个本地日期的指定时刻 |
| 每工作日 | 周一至周五的指定时刻，不接入国家或地区节假日表 |
| 每周 | 选定星期的指定时刻；未选星期时沿用开始日期的星期 |
| 每月 | 指定日的指定时刻；月份不存在该日期时跳过，不挪到月末 |
| 自定义分钟/小时 | 以 `starts_at` 为锚点，按实际经过的时间每 N 个单位重复 |
| 自定义天/周/月 | 按本地日历周期重复，支持适用的星期过滤和月内日期；周周期以开始日期所在周的周一为锚点 |

开始边界允许作为首次发生点。领取时计算的下次发生点必须严格晚于本次领取时刻；不能逐个遍历停机期间
错过的分钟或建立无界补跑队列。日历发生点遇到夏令时缺失时间则跳过；重复的本地时间只采用第一次。
自定义分钟/小时按实际间隔计算，不将本地时钟回拨解释为重复领取。

### 4.2 领取与调度

1. 只有启用、没有活动执行、存在 `next_run_at` 且已到时刻的任务，才允许领取。
2. 领取必须在一次 Repository 原子提交内保存单次执行身份、活动执行关联、最近执行关联和下次发生点。
   配置修订冲突使领取失败，调度器重新读取，不覆盖用户修改。
3. 单次执行按 `(task_id, scheduled_at)` 唯一。Agent 目标使用精确 Run 身份；插件目标使用领取时固定的实现和参数。
4. 进程恢复后，仍有效的任务将多个未领取发生点合并为一次执行，随后定位到未来；计划已结束时不补跑。
5. 同一任务不得重叠执行。排队、运行、待审批、待用户输入和运行暂停均保留活动执行关联，阻止下一轮领取。
6. 复用会话已有活动 Run 或被执行锁占用时，本任务暂缓领取；调度窗口应继续覆盖其他到期任务。
7. 结束时刻后不启动新的执行；已经接受的执行可以继续完成。长执行结束时若计划已过期，清空下次发生点。

计划时刻是最早可领取时刻，不承诺硬实时启动。实际启动还受进程是否存活、调度所有权、会话占用和共用
Supervisor 容量约束。原子领取与防重复派发不等于外部工具副作用的 exactly-once 保证。

## 5. 状态、结果与统计

### 5.1 两种状态边界

任务状态表达当前计划或活动执行；执行记录表达该次目标的结果。不能将“本次完成”解释为“重复任务结束”。

| 条件或事件 | 任务状态 | 执行记录 |
|---|---|---|
| 启用且存在下次发生点，没有活动执行 | `waiting` | 保留已有历史 |
| 已领取、共用 Run 排队 | `waiting` | 本次为 `waiting`，保持活动关联 |
| 运行、取消处理中或等待子执行 | `running` | 投影规范 Run 的活动状态 |
| 等待审批、等待用户输入或运行暂停 | `paused` | 本次未结束，保持原 Run 与活动关联 |
| 本次完成、失败或中断，仍启用且有后续计划 | `waiting` | 分别保存 `completed`、`failed`、`interrupted` 及结束时间 |
| 本次结束，计划已停用 | `paused` | 独立保存本次终结结果 |
| 本次结束且没有后续计划 | 本次终结状态 | 保存本次终结结果 |
| 无活动执行时停用计划 | `paused` | 不改变历史结果 |
| 重新启用或延长已结束计划，存在新发生点 | `waiting` | 不重放原指令，不改变历史结果 |
| 停机期间计划到期且从未执行 | `completed` | 不创建虚假的成功执行记录 |

审批标志 `awaiting_approval` 来自规范 Run/Supervisor 的 `waiting_approval`。它是暂停状态的细分信息，
不能通过“任务已暂停”推断。计划停用不会取消已接受的执行；中断本次执行也不会自动停用后续计划。

状态对账仅允许匹配 `active_execution_id` 的执行更新该任务。终结事件到达时，仍须等待监督任务释放执行，
再清除活动关联；不能仅因收到完成事件就允许下一轮与尚未退出的生产者重叠。

### 5.2 首页统计

首页 `Task` 固定显示 `完成次数/完成与失败次数之和 completed`，例如：

```text
7/10 completed • 2 waiting • 1 running • 1 approval
```

- `7/10` 表示 7 次完成、3 次失败；斜杠两侧无空格，标签使用小写。
- 完成和失败次数按保留的单次执行记录累计，不按任务定义数计算。同一任务多次执行分别计数。
- 中断、排队、暂停及仅结束计划不计入分母；没有已结束的成功或失败执行时显示 `0/0 completed`。
- 动态项顺序固定为 `waiting`、`running`、`approval`，计数为零时隐藏，完成统计始终位于前方。
- `waiting` 和 `running` 按任务当前状态计数；`approval` 只统计确实在等待审批的活动执行。
- 等待使用低亮色 `text_lowlight`，运行使用主题色 `accent`，审批使用提醒色 `warning`；
  连接符采用共用 `inline_separator` 和欢迎页其他项目的 `text_lowlight` 颜色。

重复任务从运行转为完成后，如果仍有后续计划，首页应从 `0/0 completed • 1 running` 收敛为
`1/1 completed • 1 waiting`。这由应用层持久化状态与执行统计共同产生，TUI 不另建状态机。

删除任务会同时删除其执行记录和对应累计次数，保留已创建会话。任务状态的完成数与单次执行的完成次数
是两个不同查询字段，Host 不得混用。

## 6. 配置管理、失败与恢复

### 6.1 修改与控制

创建、修改、启停和删除通过 `TaskService` 完成。修改与控制请求携带预期修订号；过期修订必须报告冲突，
要求读取最新记录，不能无条件覆盖。配置更新保留正在执行的关联；结果对账保留同时发生的配置修改。

改变执行计划时重新计算可执行发生点；重新启用已结束计划必须失败，直到用户先保存有效的新计划。
已领取执行使用其接受的参数和身份，编辑配置不会重写正在运行的目标。

有活动执行时禁止删除。中断请求写入对应执行并唤醒调度器；已接受的请求不能被稍后的状态快照覆盖。
Agent 中断使用 Supervisor 或精确 Run 的通用取消入口，插件中断取消该次受管理调用。另一 Host 的请求由调度
所有者在刷新时消费。审批和用户输入通过原 Run 的共用入口解决。任务会话打开失败应呈现诊断，并保持 TUI 可用。

### 6.2 故障矩阵

| 场景 | 必须遵循的处理 |
|---|---|
| 模型、思考或工作空间校验失败 | 保存阶段拒绝无效配置；派发阶段记本次失败并保存原因，其他任务继续调度 |
| 领取后、提交前进程退出 | 恢复时将该发生点记为中断，不自动重放其指令 |
| 插件缺失、禁用、调度声明或权限不可用 | 领取前暂停计划并保存原因；不创建失败 occurrence，不自动启用插件 |
| 插件执行中被禁用或调度器停止 | 取消并等待调用结算，保存中断结果；停止 home 服务前先结算插件任务 |
| 插件执行中升级 | 已领取执行持有原实现及依赖；后续发生点绑定当前实现 |
| 插件调用结果尚未持久化时 Host 进程退出 | 结果未知，恢复记为中断，不自动重放 |
| 创建会话或提交 Run 失败 | 记录失败；已预留但未执行的 Run 必须收束，不能永久占住会话 |
| 会话或关联 Run 被移除 | 活动执行对账为中断；复用模式在后续领取时可重建会话 |
| 等待审批时进程退出 | 保留原 Run 与审批身份，恢复后可进入该会话继续，不新建替代执行 |
| 运行中进程退出 | 遵循共用 Runtime 的关闭、所有权检查和恢复协议，不新增任务专用重放循环 |
| 任务存储无效或不可访问 | 暴露 `PersistenceError` 或调度诊断，调度可重试；不能阻止普通交互会话启动 |
| 同一 home 有多个进程 | 只允许一个调度锁持有者派发，其他进程仍可查询或提交配置修改 |
| 异步菜单操作结束时原菜单已关闭 | 不再查询或刷新旧控件；会话加载与观察继续由应用拥有的工作执行 |

失败或中断保留在执行历史中。有效重复计划的下一次执行是新的发生点，不是对失败发生点的自动重试。

## 7. 分层与共用运行链

| 层或模块 | 职责与边界 |
|---|---|
| Core / 共用 Runtime | Session、Run、事件、工具、HITL、取消和运行不变量；不认识自动任务菜单、频率或调度业务 |
| `ouro.core.tasks` / `ouro.automation.schedule` | 类型化任务数据与纯日历计算；不驱动 Host 或模型调用 |
| `ouro.core.task_ports.TaskServicePort` / `NativePluginScheduler` | 应用用例端口与受代次约束的 SDK 适配；插件层不依赖应用或持久化实现 |
| `TaskRepository` / `LocalTaskRepository` / `SQLiteTaskRepository` | 文件与 SQLite 共用持久化契约，提供索引、查询、版本与原子提交；不选择执行策略 |
| `TaskService` | 存储就绪、配置校验、修订、领取、计划边界及任务/执行状态投影；公开调用不依赖调度循环先启动 |
| `TaskExecutionCoordinator` | 共用派发容量与执行对账；Agent 使用 Run，插件使用绑定的服务调用 |
| `PluginTaskProvider` / `PluginTaskBinding` | 装配边界提供窄绑定，持有实际插件及依赖，仅执行被接受的方法 |
| `TaskScheduler` | 进程调度所有权、截止时间、唤醒、重试与停止领取 |
| `LocalApplication` | 组合服务、启动恢复与资源关闭顺序 |
| TUI / 其他 Host | 表单、菜单、键位、状态展示和会话观察；通过应用接口工作，不直接读取任务数据库 |

```text
任务配置
  → TaskService.claim（任务 + 单次执行 + 下次计划的原子提交）
  → RuntimeService.create_session / 复用 Session
  → RuntimeService.prepare_run → reserve_run
  → RuntimeSupervisor.submit_prepared(background=True)
  → RuntimeService.run_prepared → RuntimeService.run
  → 共用 InnerLoop / Tool / HITL / Event / Persistence
  → 按 Run 结果对账任务与单次执行
```

用户会话、Agent 自动任务和子智能体应在共用运行机制上保持一致。子智能体同样通过预留 Run 和既有状态转换
执行，不为任务增加另一套模型循环、工具执行器、审批处理器或消息存储。

任务新建会话使用 `namespace=task`，普通用户会话使用 `default`；子会话和分支继承会话域。域是持久化的
通用分类，不替代 Run 身份、父子关系或权限边界。计划运行使用 `origin=host`、`trigger=scheduled`，
`initiator_id` 关联 `ouro.tasks/<task_id>`，便于追踪来源。
任务输入由调度入口显式声明 `role=user, scaffold=true, semantic_tag=scheduled-task`，正文保留任务配置中的
原始指令；模型可见的语义标签标识其定时任务来源，不改变消息角色或授权范围。

Host 创建新用户会话时通过显式创建回调绑定身份，不从并发事件推测会话归属。打开任务会话遵循公共
snapshot/live 衔接：读取历史与事件水位，订阅后续事件，并用规范 Run 校准活动状态，包括尚无运行事件的
排队 Run。退出菜单或切换可见会话不终止任务。

公共订阅持续跟踪所选会话后端的增量事件，不依赖任务执行进程。同一 home 的其他 OURO Host 可在任务执行工具、
流式输出或等待审批时进入会话，继续接收后续事件；完成后同一订阅仍能观察下次 Run。事件顺序、去重、读取频率、
资源释放与错误恢复遵循[会话观察规格](session-observation.md#持续事件跟踪)，TUI 不另行轮询任务消息。

## 8. 持久化、一致性与性能

### 8.1 数据边界

任务存储与 Session 消息存储是两个独立的持久化边界，选择均由 composition root 完成，不能在执行协调器或
TUI 内根据任务类型另建 SessionStore。任务会话始终使用 `TaskService.runtime.store`，与用户及子智能体一致。

| 选择 | 配置与优先级 | 数据位置 |
|---|---|---|
| 任务后端 | 显式 `task_backend` / `--task-backend` > `OURO_TASK_BACKEND` > `files` | 文件为 `<home>/tasks/`；SQLite 为 `<home>/tasks.sqlite3` |
| 会话后端 | 既有 `session_backend` / `--session-backend` > `OURO_SESSION_BACKEND` > `files` | 沿用 OURO 会话后端的数据布局 |

`files` 与 `sqlite` 任务后端共享 `TaskRepository` 协议，支持与两种会话后端的全部四种组合。
TUI 与 `run` 入口接受任务后端选择。无效后端在组合边界报告配置错误，不静默回退。
切换任务后端不自动迁移、合并或发现另一个后端的数据；已有 SQLite 任务通过显式选择 `sqlite` 使用。
同一 home 的多个 Host 应选择相同后端组合，切换前结束活动执行并关闭原 Host，避免在另一会话数据集中恢复 Run。

文件后端的布局为：

```text
<home>/tasks/
  layout.json
  index.json
  transaction.json                 # 仅在已接受的事务发布期间存在
  records/<task_id>/
    task.json                      # 任务、历史头、完成/失败次数及审批投影
    executions/<execution_id>.json  # 单次执行及前一执行身份
    occurrences/<UTC时刻的哈希>     # 发生点到执行身份的去重记录
```

- `task.json` 和执行文件是规范数据，不存储会话正文；`index.json` 只保存任务元数据，丢失或损坏时从任务
  记录重建，不扫描执行历史。规范记录损坏或版本不支持时必须报告错误，不能跳过或当作空库。
- 所有读写通过同一跨进程短锁观察文件事务。先持久化完整事务意图，再原子替换执行、任务和索引文件，
  最后移除意图。发生中断后，读取或启动恢复先幂等完成发布，使任务状态、执行结果和汇总一并可见。
- 文件事务意图持久化后操作已被接受；后续 I/O 失败时不能假定操作未生效。恢复后读取任务修订和执行身份，
  按共用恢复协议处理，不盲目重发用户指令。
- 执行历史通过前序身份按需读取，返回最近接受的执行；每次更新不重写全部历史。发生点记录确保重复领取
  被拒绝。删除任务在同一可恢复事务内移除任务、执行历史及索引。
- 文件布局与规范文件显式声明 `schema_version=3`。元数据索引包含 owner，分页只读取筛选后的记录。
  文件后端不依赖 SQLite 作为隐藏索引或规范存储。

SQLite 后端使用 WAL、短事务、修订比较以及 `tasks` / `task_executions` 表；到期、状态、执行历史及
结果索引支持查询。领取、结果对账及删除通过数据库事务保证原子性，发生点唯一约束用于防重复派发。
owner 与更新时间的联合索引支持插件分页。当前数据库版本为 `3`，不支持的版本或无效记录必须报告可诊断错误，
保留已有数据。

SQLite 初始化使用 `<home>/tasks.init.lock` 协调多 Host 同时建库，锁等待上限为 5 秒；版本检查、WAL 设置和
建表由同一个初始化所有者执行。此锁只用于初始化，常规任务事务与调度所有权保持独立。

两种后端共用 `<home>/locks/tasks.lock` 调度所有权锁。文件事务使用独立的 `task-data.lock` 短锁，不把整个
模型执行期间变成存储临界区。任务与会话存储之间不假设跨库或跨文件系统事务，通过领取身份、Run 预留和
恢复对账收束中间故障。

新建 Session 的默认 `namespace` 为 `default`；任务执行的初始 `awaiting_approval` 为否。
活动执行恢复或对账从规范 Run 校准审批状态。这些业务默认值不构成历史格式兼容或迁移协议。

### 8.2 运行成本与生命周期

- 调度使用最早发生点与配置/生命周期信号唤醒。同进程修改立即发信号；空闲时默认最多每 30 秒检查外部
  修改和调度锁接管，不采用高频全量扫描。到期但会话忙碌或容量不足时保留等待下限，避免忙循环。
- 调度协调器默认派发并发上限为 `1`，Agent 与插件调用共享该容量；Agent 复用 Supervisor worker。
  后台提交不加入前台请求的模型启动顺序门控，不额外创建任务模型执行池。
- 到期选择采用 100 条有界窗口并滚动游标，忙碌复用会话不能永久饿死窗口后的任务。TUI 清单每页 100 条，
  执行历史入口查询最近 50 条。
- Agent 对账只读取活动任务关联的 Run，插件对账读取所属调用的完成结果，不扫描全部会话消息。生命周期信号不携带后台 token 到当前前台视图；
  用户选择目标会话后，使用公共有界订阅接收流。
- 文件任务后端的热查询读取小型任务索引并按文件版本缓存；只加载被选中的任务和执行，不扫描历史目录。
  索引更新成本与任务定义数有关，历史分页成本与返回页大小有关。SQLite 使用对应查询索引。
- 任务与会话 I/O 通过线程边界执行。等待审批释放当前监督工作占用，保留可恢复 Run；终结提交释放句柄。
- 首页默认每 2 秒查询任务汇总，计数或错误变化时才更新展示。持久化结果、统计和 Host 显示允许经过该
  对账与刷新周期收敛，不能将显示延迟解释为任务尚未完成。
- 关闭时先停止领取、结算插件调用，再停止所属 home 服务；Runtime/Supervisor 结算 Agent Run 后保存结果并释放调度锁。

上述设计约束后台开销，不承诺后台模型或工具消耗与前台完全隔离，也不提供固定的毫秒级触发 SLA。

## 9. TUI 交互契约

### 9.1 导航与管理

`Ctrl+P` 分页菜单中的 `Task` 分组直接列出 `Task list`、`Create task`、`Delete task`、`Update status`，
各操作无需经过额外的管理菜单。`Ctrl+T` 直接进入任务清单，使用共用菜单导航，重复操作不叠加窗口，不替换尚未保存的
配置编辑器。`Update status` 刷新应用投影，不触发立即执行或手工补跑。

任务详情提供当前状态、下次计划、打开会话、执行历史、中断、编辑、停用/启用、删除和刷新。任务会话尚未
创建时禁止打开；无活动执行时禁止中断。详情和历史均可打开会话，已关闭菜单的异步回调不得访问旧
`#choice-filter` 或其他控件。

会话列表通过 `Tab` 循环切换 `SESSION • USER`、`SESSION • TASK` 与 `SESSION • SUBAGENT`，允许分组为空。
前两组展示对应主会话，子智能体组汇总所有子智能体会话并沿用只读边界。任务会话支持消息查看、中途恢复流、
人工审批、中断和后续指令；运行期间发送输入采用共用输入队列，不建立任务专用聊天入口。

### 9.2 编辑与选择

插件任务编辑标题、JSON 参数和执行计划，显示固定的服务身份；不显示 Agent 的模型、工作区、审批和会话设置。
执行历史选择 Agent 记录时进入会话，选择插件记录时展示服务、状态及 JSON 结果或错误。

- 标题行使用 `Title`，不追加 `(optional)`；指令行使用 `Instruction`，不添加 `Task` 前缀。
- 审批行使用 `Approval`，选项名称和说明与 `/approval` 共用来源。思考行使用 `Model thinking`，
  与会话思考菜单共用能力筛选和选项语义，不支持配置时禁用。
- 模型、审批、思考、时区、频率、会话方式及重复单位等单选页，应在当前项前显示主题选定标记，并定位初始焦点。
- 时区选项可搜索，系统本地时区置顶；选中其他时区再编辑时保持其值。
- 保存项统一为 `Save`，放在 `ACTIONS` 分组；草稿无效时禁用并给出错误，保存动作才提交整个任务配置。

指令使用可复用的多行内联组件 `MultilineEditableChoiceScreen`，通过 `Choice.edit_lines=3` 声明三行编辑：

| 操作或状态 | 行为 |
|---|---|
| 保存态 | 单行展示，预览指令首行 |
| `Enter` | 进入编辑；编辑中按下则保存该字段并退出编辑 |
| `Ctrl+Enter` | 编辑中插入换行，不提交字段或任务 |
| 多行粘贴 | 保留内容内部换行，统一换行表示 |
| 编辑态 | 固定三个可视行，支持折行和随光标滚动；上下键、Home/End 在文本中导航 |
| `Esc` | 取消本次字段编辑，恢复原已保存的字段值 |

多行组件与普通单行字段分工明确，业务表单只定义字段、预览和保存回调，不保留另一套独立 TextArea 弹窗。

## 10. 验收证据

| 验收范围 | 可重复证据 |
|---|---|
| 六类计划、包含边界、闰年与缺失月日、夏令时、长时间停机计算 | [计划测试](../../tests/application/test_task_schedule.py) |
| 两种任务后端的修订、去重、并发写入、分页、统计，文件部分发布恢复和索引重建 | [任务存储契约测试](../../tests/application/test_task_storage.py) |
| 四种任务/会话后端组合和配置优先级，真实任务会话使用共用 SessionStore | [组合边界测试](../../tests/application/test_local_application.py)、[CLI 后端选择测试](../../tests/cli/test_task_backend_selection.py) |
| 共用 Runtime、文件/SQLite 会话后端、原子领取、会话复用和删除重建 | [任务应用测试](../../tests/application/test_tasks.py)：`test_due_task_runs_with_persisted_configuration_through_shared_runtime`、`test_missed_occurrences_coalesce_and_claim_is_atomic`、`test_session_creation_mode_and_deleted_reusable_session` |
| 单次结果与重复计划分离、停用/到期边界、延长计划恢复等待 | 同上：`test_execution_result_is_separate_from_remaining_schedule`、`test_extending_an_ended_schedule_returns_task_to_waiting` |
| 调度锁、启动派发、审批恢复、派发失败隔离、后台并发和有界窗口公平性 | 同上：scheduler、approval、restart、launch failure、background concurrency、busy reusable sessions 场景 |
| 主页计数公式、动态项顺序、语义色、默认端点、选定标记、保存与快捷导航 | [任务 TUI 测试](../../tests/tui/test_tasks.py) |
| 会话列表、任务详情及执行历史三种入口均能恢复流，后台保持静默且可追加指令 | 同上：`test_background_run_stays_silent_and_attaching_restores_live_stream` 的三个入口参数 |
| 已排队任务可在模型开始前中断 | 同上：`test_tui_cancels_a_queued_task_before_it_can_start` |
| 多行组件的换行、粘贴、保存、重开、取消和固定高度视口 | [内联编辑组件测试](../../tests/tui/test_inline_editor.py) |
| 共用菜单刷新与页面切换、层级依赖边界 | [TUI 共用交互测试](../../tests/tui/app/)、[架构边界测试](../../tests/architecture/test_boundaries.py) |

验证本领域行为的命令：

```powershell
uv run --frozen pytest -q tests/application/test_task_schedule.py tests/application/test_tasks.py tests/tui/test_tasks.py tests/tui/test_inline_editor.py tests/architecture/test_boundaries.py
uv run --frozen pytest -q tests/application/test_task_storage.py tests/application/test_local_application.py tests/cli/test_task_backend_selection.py
uv run --frozen ruff check ouro tests
uv run --frozen mypy ouro
```

文档更新必须保持任务状态、执行次数、调度边界和 TUI 标签一致；修改公共会话行为时，应同时检查关联规格
与对应测试。规格不记录已完成实施清单，也不将未测量的性能目标写作已达到的保证。

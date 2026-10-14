# 核心运行时规格

> 文档类型：现行规格

## 目标与范围

核心运行时负责把一次用户输入转换为可恢复的 Run，并在模型响应、工具调用、人工交互和终止状态之间
维护一致的状态机。核心状态机不直接访问 CLI、文件系统或具体 Provider 实现。

## 领域对象

Session、Run、Message、Event 等可独立持久化的核心记录继承同一版本化记录契约。新记录默认写入当前版本，
但持久化读取必须经过 versioned decoder 并要求 payload 显式包含当前 `schema_version`；普通 Pydantic 字段默认值
不能作为旧数据迁移。OURO 模型忽略未声明字段，但仍严格校验已声明字段、必填字段和领域约束；未知字段不会进入
规范对象或再次持久化。嵌套值对象随其根记录版本演进，只有独立写入的 artifact 才建立独立解码边界。

### Session

Session 是长期会话容器。关键字段包括：

- `session_id`：稳定身份；
- `current_model`：完整 `provider_id/endpoint_id` 路由；
- `status`：`idle`、`running`、`waiting_user`、`waiting_approval` 或 `failed`；
- `active_run_id`：唯一 active Run；
- `last_message_id`、`message_count`：canonical message 游标；
- `active_compaction_id`：当前上下文摘要投影；
- `harness_version`：当前不可变业务状态版本；`history_epoch`：当前执行历史代次；
- lineage、标题、归档、置顶、工作区与标签状态。

同一 Session 同时最多存在一个 active Run。

### Run

Run 表示一次可恢复执行。状态集合为：

`queued`、`running`、`waiting_user`、`waiting_approval`、`waiting_child`、`completed`、`aborted`、`failed`、
`paused`、`recoverable`。

运行阶段为：`model_streaming`、`tool_preflight`、`human_interaction`、`tool_execution`、
`subagent_join`、`result_commit`。Run 保存模型摘要、usage、活动工具批次、待审批身份、终止请求和错误信息。
ProviderStreamEvent 的 error 可附带供应商无关 error_code；原生模型请求将其传给 Driver 执行边界，
写入 run_failed.payload.error_code 并进入 CLI JSON 的 events。Core 不解释认证状态或 HTTP 对象，
不在 RunRecord 复制认证状态；请求侧分类见[请求认证规格](request-authentication.md)。

活动 Run 可在 model turn 边界切换路由，因此不能仅凭 `RunRecord.model` 重建逐轮模型历史；每次实际 Provider
请求使用的 provider、endpoint 与 model 以 `model_request_started` 事件和 usage 记录为权威。

### Run Driver 与原生端口

Run 记录使用 schema 5，Message 记录使用 schema 4。Run 保存不可变的 `driver` 绑定。默认算法的身份为
`ouro.default`；插件 Driver 保存 `plugin_id/run_drivers/id`、贡献契约，以及所属插件和传递依赖的实现引用。
每个实现引用包含 plugin identity/class、release、配置修订 ID 和执行契约哈希，不保存解析后的凭据。
Host 在执行前校验精确契约，缺失或不匹配时报告错误，不切换算法。Driver 调用只租用所属插件及其依赖，
模型轮次的工具与上下文目录仍在原生边界刷新。

当前目录不包含所需契约时，Host 从已安装的不可变包和配置修订按需构建所需 generation，校验完整契约后开放调用。
该装载不修改插件选版、不发布全局贡献、不启动后台服务。并发调用可复用仍持有租约的相同实现；进入等待或结束后
释放租约并关闭不再使用的 generation。已禁用的插件或依赖不参与恢复；禁用沿实际 generation 依赖关系撤销在途调用。
Builtin 必须匹配当前应用提供的发布契约。装载使用当前插件私有数据，不复制或回滚数据格式。

`RuntimeService.run(driver_id=...)`、Host 输入和 SDK `SessionInput(driver_id=...)` 在接受 follow-up 输入时
选择算法，完整实现契约随排队输入保存；消费时据此创建 Run。升级不改变已接受输入的 Driver 或依赖，排队引用也
参与包保留检查。`steer` 不能选择 Driver。原子输入 batch 必须选择同一 Driver，`all_at_once` 不合并不同契约。
Host 输入的相同 queue ID 重试复用已接受回执，不重新选版；已有 Run 的 Driver 不能由后续输入改变。
自定义 Driver 使用原生模型请求、工具批次、输入交付和 Agent 控制端口，计数、用量、上下文、工具权限和审批由 Host 执行。
模型请求执行原生模型预算；Agent 控制保留共享预算与 Delegation 约束。
Driver 的完成意图由 Host 写入终态；默认算法的 turn policy 不自动作用于自定义算法。
默认算法和插件算法均以 `RunDriverHandler` 调用同一 `NativeRunDriverContext`。默认算法在模型轮次和工具批次
后检查插入输入，并在无工具响应后评估 turn policy；完成策略由 `NativeRunTurns` 接入。

`steer` 由 Host 的原生输入边界交付给当前 Run。每次模型决策前，Host 消费待交付输入并将消息纳入该次上下文。
SDK `context.inputs.consume()` 允许算法显式声明一个安全决策边界，返回已提交的 `RunInput` 视图；
算法可以在模型、工具或普通插件服务之间读取输入，不要求实现默认循环。显式消费后紧接的模型请求不再消费额外单元；
后续模型或工具操作结算后重新开放隐式边界。再次显式调用表示算法主动开始另一个决策边界。
交付遵循 `one_by_one`／`all_at_once` 和原子 batch 的队列规则，保留 Prompt Policy、来源、媒体及资源引用，
不消费 `follow_up`。返回视图与 Host 记录分离，修改视图不改写历史。输入读取只加载 Session 队列元数据，不重复读取完整历史。

未结算的模型工具响应、正在执行的原生操作以及暂停／已关闭的调用不能接受输入消费；输入只能在工具结果完成提交后交付。
审批等等待期间，steering 保留在持久队列，恢复 Driver 后仍交付给同一 Run。取消或策略拒绝不提前移除未交付输入。
自定义算法负责决定何时完成；没有进一步安全决策边界的剩余输入依照排队输入终结规则处理，Host 不擅自重跑算法。

`DriverFinished(text, response_id=...)` 可以选定当前已保存的无工具模型响应作为最终答复。Host 校验句柄和文本，
保留原消息的推理、路由、时间和 Provider continuation，不重复写入 assistant 消息。省略句柄时，文本作为 Driver
自己的最终输出保存。后续模型请求或程序化工具调用使先前的最终响应句柄失效。

工具批次、调用与 canonical 结果记录 `producer`。模型批次绑定真实 assistant 消息；程序化批次记录 Driver 身份，
不创建 assistant tool call、不增加模型计数。两种来源共用原生 preflight、权限、审批、执行和恢复链路。
Provider 边界将程序化结果投影为带 `driver-tool-result` 来源标记的用户角色输入，保留协议支持的媒体、降级不支持的
媒体，不生成模型工具请求或 function output。上下文与压缩不要求程序化结果具有配对的模型请求。

Agent 端口将类型化的 spawn、send、interrupt 请求交给原生工具边界，复用 Agent catalog、Delegation、子 Session
和同步等待。批次遵循 Host 工具并发配置，子任务遵循原生调度并发限制。重启后加载会话会恢复其有限 Agent 聚合，
已完成的子任务结果继续结算原父 Run，不重新创建子任务。

可等待的工具调用携带最多 64 KiB UTF-8 JSON checkpoint。checkpoint 与具体原生批次绑定，并在批次开始前
通过同一个 Session commit 持久化，工具副作用不能先于它发生。状态为：

- `pending`：原生工具批次尚未结算，包含审批、用户输入和子任务等待；
- `ready`：原生结果已提交，完成的批次保留在 Run 上供 Driver 恢复；
- `consumed`：Host 已原子标记消费并移除活动批次，允许把 checkpoint 与工具结果交给 Driver 一次。

结果沿用原生工具事实，不建立独立 Driver 操作账本。立即完成的工具批次也在返回调用者前消费 checkpoint。
结果已持久化但批次完成提交中断时，恢复协调器根据已保存的结果和 canonical 回执完成 checkpoint，保留媒体、资源
引用与执行时间，不重新执行工具。执行中断且没有可靠结果时记录未知结果，不推断外部副作用已经撤销。
进入等待后 Driver 返回 `DriverSuspended`，结束调用并释放执行所有权；恢复先结算原生批次，再校验原 Driver、
消费 checkpoint 并调用其恢复入口。消费前中断可以继续交付已结算结果；消费后中断不能从插件函数开头自动重跑。
没有可恢复 checkpoint 的自定义 Run 不支持任意位置重放。默认算法也使用原生批次 checkpoint；其逻辑只通过
原生执行端口产生事实，因此可以在 checkpoint 已消费后的中断中，从已结算历史继续模型轮次，不重放已执行工具。

原生端口只在所属 Driver 调用期间有效，同一 Run 的调用按顺序等待。Host 在结算 Driver 前取消并等待未完成的
原生调用，避免调用越过 Run 终态。插件撤销和 Run 中断均传播到模型流、工具及 worker 的父子调用。
用户中断时，原生端口先取消所属工作并完成结果结算，Driver 外层再取消剩余算法代码；工具取消回执与批次终态
先于 Run 的中断终态发布。

默认和插件 Driver 均建立所属 Run 的原生调用作用域。新的子 Run 使用自己的 Session、Run 及工作区事实，不继承
已结束父回调的身份。普通插件服务可通过 `PluginContext.models/tools/agents` 复用调用链上的原生端口；服务不另建
Run 或预算。嵌套调用继承只读约束，需要声明 `sessions.write`，且不能在另一原生操作内部重入当前 Run。
调用结束后旧上下文失效，不能借另一个 Run 的活跃回调重新使用它。worker 的原生路由沿 Host 保存的父请求上下文
解析目标；peer 提供的 Run ID 不构成授权。服务业务 RPC 跟随调用生命周期，模型和工具继续使用原生预算与取消。

### Run/Session 状态转移

Run 是执行状态的唯一权威，Session 只保存面向 Host 与查询的粗粒度投影。`plan_run_transition` 是不访问
Store、锁、事件或外部系统的纯规则：它校验 Session/Run identity、限制终态迁移、要求
active Run 没有 `ended_at` 且 terminal Run 必须有 `ended_at`，并在同一个时间点生成 Run 与 Session 写入计划。

Session 投影规则为：

- `queued`、`running`、`waiting_child` -> `running`，保留唯一 `active_run_id`；
- `waiting_user` -> `waiting_user`，`waiting_approval` -> `waiting_approval`；
- agent Run 的 `failed` -> `failed` 并清除 active owner；
- `completed`、`aborted`、`paused`、`recoverable`，以及 compaction Run 的 `failed` -> `idle` 并清除
  active owner。

所有 active 状态之间允许在同一 Run 内 wait/resume；active Run 可以进入 terminal 状态。显式继续可以在同一
history epoch 内将 `aborted`、`failed`、`paused`、`recoverable` Run 重新置为 running，清除结束标记并取得执行所有权；
普通写入和迟到事件不能触发该迁移。completed Run 保持终态；历史撤销后的继续创建新 Run。新 Run 只能从
`queued` 或 `running` 注册。
application 用例仍负责取得 mutation lock、准备业务字段、先保存权威 Run 再保存 Session 投影，以及在 durable
边界之后发事件。usage、continuation、tool batch 审计和消息游标等不改变 Run status 的写入不属于状态迁移。

### Message 与 model turn

- `MessageRole` 是封闭枚举：`system | user | assistant | tool_result`，只表达模型看到的上下文身份。
- `scaffold: bool` 只表达消息是否由 Harness 有意构造并注入；它与 role 正交，四种 role 都可以是或不是
  scaffold。
- `semantic_tag` 是可选模型可见语义标记；它与 role、scaffold 和 producer provenance 都正交。只有提供方显式
  声明标签时，统一 renderer 才形成语义包络。
- 消息提供方必须显式定义 role、scaffold 和 semantic tag；Core、队列、Store 和 Provider adapter 只校验并传递，
  不根据来源、role、标签或正文猜测、补全或改写其他维度。
- 用户、assistant、Harness 和工具结果消息都可以属于 canonical history。
- assistant 工具请求写入类型化 `tool_calls`。
- 工具结果写入类型化 `tool_result`。
- 可展示的模型思考写入类型化 `reasoning_content`。
- 原生协议续接数据写入 `provider_continuation`，并标记来源 provider、protocol 和 model；通用消息帧是跨路由续跑依据。
- 一次模型响应中的 assistant 工具请求及其结果共享 `turn_id`。
- `turn_id` 只能出现在拥有 `run_id` 的消息上。
- 工具身份与思考续接状态不得写入 `metadata`。
- durable input queue 把单条输入或非空有序 `SessionInputBatch` 视为消费单位；逐条消费配置不能拆分 batch，组内
  canonical messages 必须属于同一 Run 或同一安全 turn 边界。

## 运行流程

1. Host 调用 `RuntimeService.run`。
2. 新会话被创建，或现有会话通过运行态快照执行恢复检查；仅当存在中断 Run 时，按 `run_id` 分页读取该 Run 的
   event 以固化未提交的部分输出，不加载其他 Run 的 replay events。
3. 取得 session run lock；锁竞争失败时返回 active Run 或明确的 `SessionBusyError`。
4. `RunLifecycle.start` 持久化 active Session/Run。
5. 输入消息按提供方给出的 role、scaffold、semantic tag 和 provenance 原样持久化并产生事件。
6. `InnerAgentLoop` 通过 `RunModelRequests` 请求模型。宿主复用已加载的 canonical messages 准备上下文，
   检查预算、提交请求计数并调用 request-local `RuntimeAdapter`；正常模型轮次不得重新加载完整 Session 事件历史。
7. 模型文本、reasoning 和工具调用增量到达时依次形成事件；`done` 后完整工具调用交给
   `ToolCallCoordinator`，最终消息则完成 Run。
8. waiting 状态通过 approve/reject/AskUser 或 child 结果恢复同一 Run。
9. Run 退出执行时释放执行许可和环境绑定；终态清理 Observer 隔离状态，已投递的观察任务独立完成收尾。
10. 在 model turn 边界消费 steer 输入；completed Run 先释放 session run lock，再串行启动 follow-up Run。

## Run 进程内资源

`RunResources` 按 run_id 保存一次执行所需的环境租约、控制视图、委派上限、Agent 目录和工具协调器。
持久事实归属 Session、Run、ToolCall 与 Delegation；执行锁和取消信号归属 `RunExecutionOwnership`。
资源登记、释放和异常收尾由 Runtime 负责，CLI、TUI 与 Supervisor 使用相同边界。

- 每轮模型决策取得当前不可变 Catalog。工具描述、上下文提供方、技能、派遣目录及该响应产生的工具调用共享
  这个绑定；下一轮重新取得当前能力。模型请求已经发出后，不把调用转交同名新实现。未装配插件的 Runtime
  同样按此边界绑定 Host ToolRegistry。
- `waiting_approval`、`waiting_user`、`waiting_child`、暂停和终态释放 Run 的环境租约及协调器。恢复取得当前
  环境，只检查待完成调用的精确契约、当前权限及控制要求。不存在重建整个历史 Catalog 的步骤。
- 队列只保存输入、来源和接受凭据，不持有可执行环境。用户输入入队与消费均执行相应时点的 Prompt Policy；
  拒绝消费保留队列事实，不把旧 allow 当作永久许可。原子输入批次不拆分。
- 插件命令是独立的当前环境操作，即使目标 Session 有等待中的 Run，也使用当前 Catalog；Harness 在命令执行前
  捕获，提交时检查同一版本与历史边界。
- Observer 为已投递工作独立持有租约，回调完成、取消或超时后释放。旧执行实例负责已开始的回调；禁用关闭 gate，
  禁止该实例接受新的回调，已有副作用按正常结束或未知结果处理。
- 所有入口显式登记资源，异常和取消关闭已取得租约。重复释放幂等；Runtime 关闭尝试释放全部资源并汇总错误。

环境变化不影响规范历史的可读性。Run metadata 中 Catalog 与 generation 信息是最近绑定的审计摘要，不能作为
Session 继续的整体前提。持久 ToolInvocation、控制 activation 和 Delegation 分别承担操作、约束和委派契约。

## Inner loop 边界

`InnerAgentLoop` 是默认 `RunDriverHandler`，只拥有“模型 → 工具 → 模型”算法及默认 turn 规则：

- `RunDriverContext.request_model` 返回本次真实模型响应及句柄。
- `RunDriverContext.execute_model_tools` 使用句柄执行原生批次；进入等待时算法返回 `DriverSuspended`。
- `DefaultRunTurns` 提供插入输入消费与完成策略；`NativeRunTurns` 将其连接到原生会话状态。
- `DriverFinished` 表达完成意图，`NativeRunDriverContext` 统一管理完成、失败、暂停、取消和 checkpoint 消费。

`RunModelRequests` 属于 Application，负责能力绑定、路由解析、上下文准备、本 Run 与共享预算检查、请求计数、
用量入账和事件发布。它执行一次请求，不执行工具或决定 Run 终态。用量以原请求身份写入原生账本，
即使响应不可用或记账期间收到取消，已报告的用量仍属于该 Run。

`execute_model_request` 属于 Core，负责响应流的所有权、增量聚合、完成原因判断、工具参数解析及响应时间记录。
它返回包含部分输出的 `ModelExecutionResult`；不持久化 Session/Run，不选择下一步算法。

模型请求开始时成对采样 UTC 时间与单调时钟；Run 内请求的起点包含请求计数提交及开始事件发布的耗时。
思考结束与请求结束时间均由同一 UTC 起点加单调时钟经过时间生成，不受请求期间系统校时影响。
阶段时间只在首次从思考转入正文或工具调用、以及请求结束时采样；收到 `done`/`error` 时记录请求结束，
未收到终止事件的异常中断、流耗尽与 AbortSignal 在关闭流后计时。未出现正文或工具调用的思考以请求结束为结束点。
`ModelResponseRecord` 保持开始、思考结束、请求结束的顺序校验；单调时钟读数不进入持久化格式。

工具等待由 `ToolCallCoordinator` 通过 `RunLifecycle.save_batch_state` 原子提交。返回值使用提交后的记录，
包含已消费的 harness change 和绑定的交互身份；Driver 上下文直接消费已提交的等待结果。
上述请求类型是宿主内部契约。Core 不依赖 SessionStore、CLI、插件 Manifest 或 Provider factory。

## Provider 流契约

- `RuntimeAdapter` 以每个 provider stream 一个常驻 reader 和一个固定容量 FIFO 解耦网络生产与 Core 消费。
  reader 是 provider 迭代器的唯一调用者；FIFO 是唯一无损背压边界。持久化、Host 或 UI 的协作式等待不得直接
  暂停 socket 读取，FIFO 满后 reader 才允许等待消费者。
- FIFO 保持事件顺序。每个连续语义段中首个 `text_delta` 或 `reasoning_delta` 不等待未来分片；后续快速分片以
  上次同类发射时间为锚点，在最多 8 ms 且不续期的窗口内合并。到达时窗口已截止的慢分片立即交付。合并受
  16 KiB 字符上限约束，不得跨 continuation、tool、retry、done、error、类型切换或携带 usage/completion 的
  事件边界。Provider 原始 chunk 边界不属于 Core 的稳定语义。
- Provider 异常必须在已排队事件之后按序交付，不能转换为正常 `done`。消费者结束、取消或 abort 时必须取消 reader
  并关闭 provider async iterator，不能遗留网络读取任务。
- 模型请求执行器以第一个 `done` 或 `error` 为终止边界，返回前关闭 adapter 及其 provider 流。
  Python task cancellation 在关闭流后向调用方传播；AbortSignal 返回部分输出，宿主结算原生取消状态。
- Provider 必须把流式 chunk 与非流式完整调用都在 adapter 边界规范化为 `tool_call_delta`；Core 不接受第二种
  完整 `tool_call` event。流式路径不得在 Provider 内等待完整响应后再
  一次性提交；只支持非流式响应的 Provider 为每个完整调用产生一个包含全部字段的 delta event。
- `tool_call_delta.index` 是当前 assistant 响应内从零开始的工具调用序号，并稳定关联同一调用；Provider
  适配器必须在统一边界内把供应商原生 content-block 坐标归一化，不能向 Core 泄漏。`id_delta`、
  `name_delta` 和 `arguments_delta` 字段均保持追加分片语义。
- `RunModelRequests` 按到达顺序发布 `model_tool_call_delta`。Core 执行器按工具 index 追加分片，
  完成时一次拼接并解析 arguments JSON，校验身份和批次唯一性；不逐分片复制累积参数。
- 完成事件的用量记录先于输出可用性判断。输出长度受限、过滤或不完整时，不执行工具片段，不保留可重放 continuation。
- Provider 可在工具调用响应中产生一个 `continuation` 事件；执行器只接受一个类型化 artifact，并与
  assistant 工具消息原子提交。普通最终回答不保存协议续接状态。
- `done` 只携带完成边界和 usage，不重复携带工具调用，避免增量流和最终快照成为两个互相竞争的真相来源。

## 预算与暂停

软预算只在 durable 模型回合边界检查：

- 下一模型轮次；
- 累计输入 token；
- 累计输出 token；
- 已用时间。

超过软预算进入 `paused`。上下文无法安全构建时以 `context_blocked` 暂停。预算未配置时不限制运行轮数。

## 排队输入

active Run 期间的新输入写入 Session metadata 中的 durable 队列。每项完整保存 `source` provenance、`role`、
`scaffold`、可选 `semantic_tag` 和交付模式；这些字段分别由输入提供方定义，队列不互相推断：

- `follow_up`：当前 Run 完整结束后交付，每次消费创建一个后续 Run；
- `steer`：当前 model turn 完整结束后交付，作为同一 Run 下一轮模型请求的新 canonical 消息。

每项还显式保存生产来源：

- `user`：真实用户输入，可以携带用户资源引用；
- `host`：OURO Host/Application 产生的输入，必须携带稳定 `host_source_id`；
- `plugin`：插件产生的输入，必须携带完整 plugin/contribution/generation/contract provenance；
- `delegation`：只允许专用 `DelegatedTaskInput` 启动已准备好的 child Run，必须携带 `delegation_id`，
  该提供方显式声明 `role=user, scaffold=true`。

role 不改变 producer provenance，也不授予直接用户权限。Host 和插件输入不能携带未经对应 use case 验证的用户资源
引用；delegation 输入可携带 Spawn 时已验证并复制到 child Session 的资源引用。未知来源、Host 来源缺少 source id、
插件来源缺少完整执行归属、delegation 来源缺少 relation identity，或缺少消息三维契约字段的持久化项均按无效项
移除，不按来源或历史默认值补全。

工具请求及结果共享原子 turn，steer 必须在工具结果提交后插入。`queued_inputs.consumption_mode` 决定每个
消费边界只取一条（`one_by_one`）或取出当时全部符合交付时机的消息（`all_at_once`）。过滤不同交付模式时，
符合条件的消息保持原始入队顺序；较早的 follow-up 不阻塞较晚 steer 在 turn 边界生效。

队列项具备唯一 id。完整消费单位通过 `SessionCommit` 一次提交消息、逐节点 Harness 锚点、队列消费与
Session 投影；没有独立 acknowledge 阶段。输入接受回执在撤销消息后仍保留，重试不能重新接受同一输入。

审批和 AskUser 恢复属于原 Run 的同一锁事务。恢复后即使 Run 已 completed，也不能在该锁的作用域内递归
启动 follow-up；后续 Run 必须等恢复入口释放非重入 session run lock 后再取得新的锁。

## 子智能体等待

模型侧 `SpawnAgent(execution_mode=sync)` 不在工具 handler 内轮询 child。原 SpawnAgent 所在
`ToolCallBatchRecord` 保存 durable `AgentWaitRequest`，父 Run 进入 `waiting_child/subagent_join`，`RuntimeService` 把 waiting outcome 交还
`RuntimeSupervisor` 并释放 execution permit。child 终态后，application 按原 parent session/run/tool-call
identity 解析该调用并恢复同一父 Run；重复结果通知只会命中已经解析的 checkpoint，不创建第二个父 Run。
每个同步 SpawnAgent 的 wait 只引用它创建的一个 Delegation；Host 批量等待不进入该持久协议。

child 本身仍执行普通 `InnerAgentLoop`、ToolBatch、Approval、AskUser、预算与恢复状态机。父子关系和结果交付由
application 的 durable Delegation 编排，core 不递归调用 RuntimeService，也不维护另一套 child 执行状态。

## 取消语义

- `abort_run` 先持久化取消意图 `abort_requested`，再通过 `settle_run_abort` 通知原执行的 `AbortSignal` 并等待收尾。
- 命令原子提交中的取消意图直接交给 `settle_run_abort`，不再次请求取消。已结算或已恢复的执行不会被迟到的收尾请求取消。
- 原执行完成所属工作和结果结算后才进入 `aborted`。无活动执行者时由 Runtime 在同一状态校验边界结算；有 reservation 的排队工作复用其所有权。
- Provider stream、工具和子进程通过协作式轮询观察 signal。
- 未知外部副作用不能因恢复而自动重放。
- terminal Run 的后续迟到写入不能把 Session 恢复为 running。

## 进程内监督

`RuntimeSupervisor` 在 RuntimeService 外管理跨 Session FIFO 队列、全局并发上限、状态快照和 Host 订阅。
它不复制本节的 durable Run 状态机。Host detach 只关闭订阅；explicit abort 通过 RuntimeService 提交取消意图并结算；
shutdown 在有限时间内 wait 或显式 abort。未在超时内完成的 Run 在下次运行或提交所属 Session 时由
SessionStore 定点恢复；Host 的历史展示读取不触发恢复，进程启动本身也不扫描历史 Session。
`waiting_child` 与其他 durable waiting 状态不占 execution permit；child 或恢复后的 parent 作为独立工作重新进入
既有 FIFO，不增加第二个 worker pool。

durable Run 等待与本地执行回执分别表达结果可用和调度占用释放。一个已持久化的 queued 后续 Run 若遇到同一
Session 的前一个本地工作尚未收尾，只有在前一个 Run 已进入终态时才等待其回执释放，再核对当前 Run 与调度
占用后入队；运行中的前驱和同一 Run 的重复提交均拒绝。
接纳持久 queued Run 时，Supervisor 经 `RunExecutionOwnership` 取得该 Run 的 Session 执行锁，并在锁内
复核 queued 身份。开始执行时一次性移交同一把锁，交接没有无主窗口；接纳取消、排队终止、关闭或执行前失败均
释放未移交的锁。恢复、普通输入、插件输入与 Host 输入读取相同的原生所有权，其他 Host 也能识别排队工作。
终止本地排队 Run 时由其 reservation 授权结算，无须等待尚未开始的 worker；进程失去所有权后仍按原规则恢复。
终态收尾检查待处理输入时只读取当前快照；已接受但尚未执行的后续 Run 仍属于活动工作，不能由前驱的收尾路径
判定为崩溃遗留。执行恢复仅在明确的恢复入口进行。

`RunExecutionOwnership` 是 Runtime 内唯一的进程内执行所有权组件：它取得/释放 Session run lock、登记和释放
AbortSignal，并从 durable Run 轮询 abort 事实。它不写 Run status、不发事件，也不处理 queued input 或 terminal
产品语义。Run 命令由 `RunLifecycle` 负责；approval/AskUser/child wait 仍由 RuntimeService 作为跨协作者事务入口；
terminal continuation、Agent settlement、Observer/catalog lease 清理由明确的 terminal boundary 依次完成。

完整 LocalApplication 在 Runtime、Plugin catalog、Supervisor、AgentDefinitionService 和 AgentService 全部构造后，
通过一次性 `bind_extensions` 绑定完整 Host 扩展组合。绑定不可重复，Agent controls 不能脱离 matching definitions
单独注入。裸 Runtime 不绑定扩展，明确表示无插件/Agent Host。

`RuntimeActivitySnapshot` 是 Host-facing 投影：Run、ToolBatch 和 ToolCall 提供 durable 权威状态，模型增量
及 compaction 事件补足响应中的瞬时阶段。投影必须能从 SessionSnapshot 重建；`Session.status` 继续保持粗粒度，
不得扩张为 UI 状态枚举。活动订阅是可选能力，已有 durable event 订阅语义不因 UI 投影而改变。

工具批次在 `tool_batch_planned` 前持久化 planned calls 和 `tool_preflight`，结果写入 canonical messages 前产生
`tool_batch_results_committing`。`tool_batch_started` 携带预检后的逐调用状态，交互恢复与调用完成事件也携带
精确 ToolCall status；Host 不得仅凭 `is_error` 推断 aborted/failed。因而 Host 不需要从相邻事件间隙猜测
预检、执行结果和提交阶段。

批次暂停由纯 `tool_batch_suspension` 统一判定。优先级固定为 approval -> child join -> user input；该结果同时
给出 Run status、Run phase、batch phase 和拥有暂停原因的 ToolCall。preflight、并发 outcome checkpoint、执行
结束以及 interaction resume 均复用此规则，不得各自扫描 calls 后拼装不同状态。

Runtime 在一个环境绑定内惰性构造并复用 `ToolCallCoordinator`。新决策或等待恢复重新绑定环境并使协调器失效；
工具执行和 resume 前检查当前 Session controls、ToolPolicy、权限 override 与委派下限。非 resumable 问题的回答直接
形成原调用结果；resumable 回答在回调执行前持久化为 running 调用及 AnswerResolution。缺失或不匹配的工具形成
同一调用的错误结果；回答保持原问题归属，不成为对另一操作的批准。中断的 running/resume 调用按未知结果恢复，
不自动重放。

## 失败语义

- 配置、持久化和 session lock 错误必须保持类型化异常。
- Provider 失败转换为模型流 error event，再由原生 Driver 执行边界结束 Run。
- Observer 和 Host event sink 失败隔离为 diagnostic/event，不破坏 durable Run。父子终态级联、等待解析与结果固化属于
  `RuntimeService` 的显式 terminal semantic boundary，不得挂在观察型 event sink 上。
- 工具失败作为 `ToolResult` 回灌模型；协调器自身失败才结束 Run。

## Harness 提交与历史撤销

Run 在开始时绑定 history_epoch。模型、工具、Controller 和续跑操作捕获基准 Harness，并在原生提交边界
检查代次及实际使用的状态前置条件。消息和对应状态变化按逻辑顺序一起提交；回调不持有 Store 写入器。

canonical 撤销由 `SessionService.undo` 取得执行所有权，活跃执行未结清时要求用户先中断。
`RuntimeService.undo` 调用该入口撤回指定直接用户输入及其后缀，并结清输入租约和绑定子会话；待消费输入取消复用
同一队列身份。成功后恢复消息前 Harness，清除当前/最近 Run 与旧许可。保留的其他用户队列项不因撤销
自动运行，旧 child 交付、审批、工具恢复或 Continuation 不得进入新代次。

`/continue` 不追加输入，同代次中断通过合法检查点继续并保留原 Run 预算；历史被撤销后创建新 Run，
从现有 canonical 前缀准备请求。恢复入口只处理所有权、历史代次、终态结算和工具帧恢复。
模型循环在预算允许的下一请求边界解析当前 Session 模型；原生协议附件的兼容性由适配边界处理，
不限制任务切换模型后继续。具体契约见[消息撤销与会话继续](../guides/session-history.md#会话继续)。
模型轮次准备属于 InnerLoop 的失败边界，插件契约不可用必须终结 Run 并保留诊断，不能遗留运行态。

完整语义见[会话撤销与 Harness 状态规格](session-undo.md)。

## 验收证据

- `tests/core/test_inner_loop.py`
- `tests/core/test_run_transitions.py`
- `tests/core/test_tool_batch_transitions.py`
- `tests/application/test_run_ownership.py`
- `tests/application/test_runtime.py`
- `tests/application/test_tool_call_batches.py`
- `tests/application/test_queued_inputs.py`
- `tests/application/test_queued_input_modes.py`
- `tests/application/test_events.py`
- `tests/application/test_agents.py`
- `tests/providers/test_openai_chat_completions.py`
- `tests/architecture/test_boundaries.py`

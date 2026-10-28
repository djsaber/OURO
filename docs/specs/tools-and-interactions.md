# 工具与人机协同规格

> 文档类型：现行规格

## 目标与范围

工具系统把模型请求转换为受 schema、权限、取消和持久化约束的执行。模型错误调用应形成可回灌结果，
基础设施错误应明确终止或诊断，不能产生不可解释的半状态。

## 工具契约

`ToolDefinition` 描述：name、description、参数 schema、结果 schema、权限模式、来源和 metadata。
`ToolCallRequest` 保存 Provider 调用身份、参数和原始顺序。`ToolInvocation` 增加 Session/Run 上下文。
`ToolResult` 保存模型内容、结构化业务结果、错误标志和资源引用。`content` 接受字符串或有序的
`TextContent` / `ImageContent` / `AudioContent` / `VideoContent` 列表；字符串是单个文本块的简写。`content_blocks` 给出统一内容序列，
`text_content` 只提取文本供日志和 Observer 使用。模型消息与执行结果复用 `core.content` 类型，
不通过 `structured`、metadata 或 Markdown 文本隐式传输媒体。

结果与呈现的边界见[工具结果架构](../architecture/tool-results.md)。结果提交保留内容块；Provider 请求通过
`ToolResultProjector` 投影为原生工具内容与零到多个补充上下文片段。补充片段统一位于整批结果之后，带调用
来源标记，不写成 canonical 用户输入；该能力不依赖特定工具名称或图片类型。

注册时验证 schema；执行前使用同一 schema 验证参数。未知工具或非法参数返回 error `ToolResult`，
不进入审批或实际 handler。

每个预检和执行边界先刷新当前 Session controls。当前环境没有注册 ToolPolicy 时，不构造策略调用或执行
策略专用的参数校验；宿主预检仍负责参数、工具契约、权限覆盖与审批。有策略时，在调用策略前验证参数，
策略只能收紧权限；等待恢复与实际执行边界重新检查当前控制状态和权限，不跨边界缓存判定。

内部 Tool 的执行回调固定为 `handler(invocation, context)`，恢复回调固定为
`resume_handler(invocation, resolution, request, context)`，各参数按位置传入。取消信号通过 `context.signal`
访问。构造 Tool 时验证签名能接受对应参数；执行和恢复直接调用，不探测参数名，也不在失败后换一种签名重试。
回调可以同步或异步，由 `invoke_owned_callback` 隔离同步工作并拥有取消收尾。取消先关闭插件的调用视图，
再等待所属线程或异步任务结束；返回的观察与已撤销的业务提交资格分开处理，不遗弃线程或重放副作用。

公开 SDK 的 Tool / resume 回调可只接收调用对象，也可接收执行上下文。贡献适配在装配时确定调用方式并保留
同步、异步属性；非法签名在装配时失败。SDK、MCP 和 generation gate 的输出均遵循上述内部 Tool 签名。

## 内置工具参数

Read、ViewImage、Write、Edit、Glob、Grep、TodoList 和命令行工具各自以参数模型定义字段、默认值、约束和描述。
公开 JSON Schema 从模型生成，处理器在入口解析为类型化参数，再执行文件、进程或 Session 操作。
各工具复用 `ouro.tools.builtin.common` 的参数语义；公共模块不依赖具体工具实现。

- JSON 整数接受 `1`、`1.0` 等无小数部分的数值，并解析为 Python `int`；不接受布尔值、数字字符串或有损转换。
- 布尔字段只接受布尔值；这些工具及 TodoList 条目拒绝未知字段。参数错误必须在副作用前返回。
- 省略字段使用模型默认值；只有声明可空的字段接受 `null`。搜索路径和命令工作目录可空，表示使用默认目录；
  Grep 的可空过滤条件表示不设该过滤，`-C` 可空表示分别采用 `-A`、`-B`。
- TodoList 必须传入完整的 `todos` 列表，`[]` 表示空列表，`null` 非法；命令超时必须是有限正数，省略时为 30 秒。

AskUser 复用其交互参数模型；Agent 控制工具从 Run 目录投影参数契约。动态插件和 MCP 工具继续在适配边界
发布和验证各自 schema。参数类型约束不替代权限、路径归属、文件状态和持久化提交等执行时条件；审批修改参数后
仍须重新验证，不跨等待阶段复用校验结果。

## 内置工具

- Read：只读取 UTF-8 文本文件，不承担目录枚举；使用 1 基 `offset` 和 `limit` 分页，按
  `<行号>\t<内容>` 返回，单次至多 1000 行或 100,000 UTF-8 字节；
- ViewImage：读取本地图片，返回类型化图片内容与路径、尺寸说明；`detail` 可选择默认缩放或原始分辨率；
- Write：以覆盖或追加模式写入 UTF-8 文本，并自动创建缺失的父目录；
- Edit：执行精确字面文本替换，默认替换首处，可显式选择替换全部；
- Glob：按文件名、扩展名或目录模式发现文件，遵循忽略规则并限制返回数量；
- Grep：按正则表达式搜索文件内容，支持文件过滤、上下文、计数和分页输出；
- TodoList：校验并展示本次传入的完整任务列表，返回文本和结构化条目；
- 命令行工具：启动时检测宿主可执行 shell，仅暴露一个与实际执行方言同名的工具；Windows 依次选择
  PowerShell 7、Windows PowerShell、Command Prompt，POSIX 系统依次选择 Bash、POSIX sh；
- AskUser：暂停 Run 并请求用户输入。
- SpawnAgent、SendAgentMessage、InterruptAgent：从 parent 当前决策绑定目录投影的
  product-owned Agent 控制工具；只在标准 Agent-capable composition 中出现。

Glob/Grep 使用 `ToolExecutionContext` 的 workspace、忽略规则与有界输出。TodoList 是无状态工具，不读取或写入
Harness；每次调用独立处理输入，可与其他兼容工具同批顺序或并行执行。任务列表通过普通工具调用与结果保留在
会话历史中，模型每次提交完整列表。三者由基础 `ToolRegistry` 在组合根注册，不依赖插件 generation 或启停生命周期。

写入、编辑和命令行工具默认可由权限配置要求审批。Read/ViewImage/Write/Edit 的路径和命令行工具的工作目录统一经过
`ToolExecutionContext` 解析；默认限制在 Session workspace 或 Host 管理的 `OURO_HOME/artifacts` 制品根目录内，
不会放开其他用户数据，并拒绝 `..` 与符号链接逃逸。Read 在文件 IO 边界流式执行自身的行数和 UTF-8 字节上限，
返回 `next_offset`、实际行数与截断信息，避免为截取一页而全量载入文件；工具结果在 executor 边界对全部文本块
共享 `max_output_chars` 上限并标记截断，媒体 URI 共享独立的 `max_output_media_chars` 上限（默认 20 Mi 字符）。
媒体超限整块替换为说明，保留的媒体不切片、不解码。请求级内容预算独立于执行结果限额。

命令行工具的模型可见 name、方言描述和执行包装必须由同一个检测结果生成，不能用 `Bash` 名称委托宿主
默认 shell。PowerShell 以无 Profile、非交互、UTF-8 文本模式运行；Bash 以无 Profile、非交互并启用
`pipefail` 的模式运行。各方言描述需明确其引用、管道、路径、可选程序探测、错误状态与非持久会话等常见
陷阱。若没有支持的 shell，则不注册命令行工具，不能暴露一个实际无法执行或会落到其他方言的定义。

## 查看图片

`ViewImage` 的描述为 “View a local image file from the filesystem when visual inspection is needed.
Use this for images already available on disk.”。JSON Schema 由严格参数模型生成，只接受：

- `path`：必填、非空的本地文件路径，绝对路径或相对于 Session workspace 的路径；
- `detail`：可选的 `high | original`，默认 `high`，不接受 `null`。

`high` 保持宽高比，将超过 2048 像素边长的图片缩小到 2048 × 2048 范围内，不放大小图；`original`
不缩放。两者都应用 EXIF 方向。PNG、JPEG、WebP 在无需转换时复用经过解码验证的原始字节；GIF、BMP、TIFF
以及需要缩放、方向校正的图片转换为静态 PNG。动画和多页图片只返回首帧，并在结果文字中明确说明。
格式由内容识别，不依赖扩展名；SVG、PDF、损坏文件和非普通文件返回工具错误。

工具声明 `read_only`，默认自动执行，但受统一权限覆盖与路径边界约束；会话资源目录只提供读取授权。
文件读取与 [Pillow 解码](https://pillow.readthedocs.io/en/stable/reference/Image.html) 在宿主拥有的同步 worker
中执行，并在阶段边界检查取消。Pillow 在文件读取与取消检查通过后按需导入，同一进程复用 Python 模块缓存；
启动和工具注册不加载图片库。源文件至多 32 MiB，选定帧至多 40,000,000 像素；媒体结果遵循执行上下文的
编码字符限额，PNG 编码在写入缓冲区时实施上限。读取不修改源文件，不生成缩略图文件，不缓存文件内容；
读取期间发生的文件大小或修改时间变化会使调用失败，模型可以重试。

结果使用标准 `ToolResult.content` 保存文本与 `ImageContent`，`structured` 只保存路径、尺寸、格式、detail、
缩放/首帧标志和字节数。工具不选择供应商协议，不注入消息；原生工具图片、补充用户上下文与不支持图片输入时的
文本反馈由[通用结果呈现](../architecture/tool-results.md)处理。TUI 展示路径与输出尺寸，不渲染 Base64。

## 权限

权限模式包括：

- `auto`：schema 通过后可执行；
- `approval`：生成 `ApprovalRecord`，Run 进入 `waiting_approval`；
- `deny`：硬拒绝调用，不允许会话级 `auto` 或一次审批绕过。

配置按工具名覆盖 definition 默认值。审批可批准、拒绝或替换参数；替换后的参数必须重新验证。
拒绝生成 error ToolResult 并继续模型循环，不把用户拒绝当作应用崩溃。

## 工具批次

一次完整 Provider 响应中的所有 tool calls 组成一个原生批次。`ToolCallBatchRecord` 是按需显式物化的工作集，包含：

- batch/turn/assistant message identity；
- execution mode 和 max concurrency；
- 每个调用的 request、invocation、approval、result 和 result message；
- planned、interaction、execution、commit、terminal 或 recoverable phase。

Run 仅保存批次引用小头；原请求、实际执行参数、确定结果与等待状态分别按
[工具事实存储契约](session-persistence.md#工具事实存储)持久化，不序列化完整工作集。

`batch_behavior` 声明批次组合约束，与执行顺序独立：

- `compatible`：允许与其他调用组成批次，执行方式由 `execution_mode` 决定。
- `exclusive`：所在批次必须只有这一个调用。无论顺序还是并行执行，混合批次都在任何 handler 执行前整批拒绝，
  每个调用收到 `ExclusiveToolBatchViolation`，说明没有工具执行，并要求将 exclusive 调用分别提交。

该规则同时适用于模型与 Driver 批次，只约束当前批次，不提供跨 Run、Session 或外部进程的资源互斥。
Host 在构建模型上下文时，将 exclusive 约束统一追加到工具描述的请求副本；注册定义与执行契约保持原样。
说明随工具定义发送，运行时仍按结构化契约独立校验。请求呈现、预算与快照见[上下文规格](context-and-compaction.md)。

模型批次原 Call 与 assistant 引用消息在同一个决定中接受，实际进入工具前固定并提交 started。
正常 handler 或 resume handler 返回后，确定观察与待处理提案先通过原生提交保存，再进入 Session control 回调；
回调不占用存储事务或 mutation 锁。每项结果独立保存，不等待其他调用的业务处理，历史按原始 `source_index`
顺序发布并回灌模型。完成批次先统一验证全部资源引用，
再原子发布整帧消息、Call 发布状态、Harness 效果及 Driver ready；任何结果或提案验证失败不得留下部分效果。
提交成功后才投递 tool-finished Observer。恢复使用固定 Call/Outcome 引用完成整个未发布帧，已发布帧按 Call 状态
中的消息身份读取；完整身份包含 Run 与 batch，Provider call ID 可以跨批次复用。

## 顺序与并行

- `sequential`：合法批次中的调用逐个执行，不放宽 exclusive 约束，也不在调用之间自动提交工具结果或重新决策。
- `parallel`：受 `max_concurrency` 限制并发执行。
- 工具的 `execution_mode="sequential"` 可将所在合法批次设为顺序执行；`inherit` 使用宿主配置。
- AskUser 和审批会在实际执行前完成 preflight；未解决交互时不启动批次副作用。
- SpawnAgent(sync) 可进入 durable `waiting_child`，不会在 handler 内占用 worker 轮询；async 调用仍可与其他兼容
  工具共同执行。
- 一个调用失败不取消其他已经允许执行的调用；批次结果保留每项状态。

## AskUser

AskUser 调用进入 `waiting_user`，保存活动批次和问题。用户答案转换为该调用的 ToolResult，随后继续处理
同一批次。Run id 和 tool call id 必须匹配，不能把答案写入其他 waiting Run。

## Agent 控制与 child HITL

Agent 控制工具的普通 handler 只返回类型化 `AgentControlRequest`，executor 返回无 ToolResult 的
`control_requested` outcome；`ToolCallCoordinator` 再把请求交给 application `AgentService`。
意图本身不构成观察，不能持久化为空 Outcome。实际控制结果返回后才进入结果保存边界；等待 child 仍使用独立等待状态。
tools/core 不导入 AgentService、RuntimeService 或 Supervisor。SpawnAgent 的
`agent_id` schema、execution mode 与实际解析都来自同一 decision-bound `AgentCatalogSnapshot`；目录没有可派遣
definition 时省略 SpawnAgent，而不是生成非法空枚举。控制名称属于产品保留集，plugin/MCP 不能覆盖。

child 工具权限先与 parent 可下放的工具、workspace、network/MCP/external-path hard cap 求交，再应用现有
auto/approval/deny 与 ToolPolicy。`tool_selection=all` 不绕过 hard deny。child Approval 与 AskUser 仍保存在
child Run；根 Host 通过包含 delegation/session/run/tool-call/request identity 的 interaction inbox 精确路由，
错投到 sibling 或 parent 明确拒绝。unattended child 对残余审批和输入请求沿用统一确定性策略，不无限等待。

## 取消与进程

handler 进入前已取消时，executor 返回 `aborted`；进入后仅收到取消或协作式中断，不能证明外部作用未发生，
返回 `unknown_outcome`。Python 任务取消在所属工作收尾后向调用方传播；Run 的 `aborted` 终态不等于工具未执行。

应用管线对同步线程、抑制取消的异步 handler、SDK worker 及恢复回调的确定迟到结果，校验原 Call、started
attempt 和实际执行绑定后独立保存 Outcome。只保存内容、structured、资源和时间等观察，不接受 Harness、
Session control 或 Agent control 意图，不把原 Run 恢复为活动状态，也不向撤销后的历史发布结果。
已发布的未知反馈继续引用其当时字面值。worker 退出而没有返回结果时，仍保持未知，不补造确定失败结果。
独立使用 ToolExecutor 的嵌入宿主需提供结果接收端口，executor 自身不拥有会话持久化。

`ToolExecutionContext` 提供 abort 检查、执行操作的取消结算、可取消 sleep 和 shell 运行。普通工具与 application
Agent control 使用同一取消所有权：操作实际返回完成值时只保留观察；返回等待请求不产生空 Outcome。Shell：

- 在独立进程组中启动；
- 从进程启动起并发排空 stdout/stderr，不使用会先累计完整输出的 `communicate()`；
- 两个 pipe 共用 `max_output_chars` 对应的保留预算，超出后继续排空但不再累计，并分别设置
  `stdout_truncated` / `stderr_truncated`；最终 ToolResult 仍由 executor 执行统一总长度上限；
- timeout 或 abort 时终止完整进程树；
- 有界等待 stdout/stderr 管道 EOF；后代继承 pipe 不能让调用无限挂起；
- 结果标记 timed_out 或 aborted。

管道在字节边界实施硬上限，解码使用 UTF-8 replacement 语义；因此非法字节不会使状态丢失。PowerShell 的
小型完整 CLIXML 仍在 Shell adapter 解码，截断的大型 payload 明确保留 truncated 标志而不伪装成完整 XML。

插件工具应使用 execution context 的协作式 API，不能屏蔽取消信号。

workspace 约束只限制命令行工具的工作目录，不是操作系统命令沙箱；命令执行仍依赖权限策略、审批和部署
环境隔离。

## 恢复

实际执行已开始但没有持久观察的调用恢复为 `unknown_outcome`，不得自动重放。有确定 Outcome 时使用其原始
观察完成未发布消息；若尚有未发布的 Harness 或 Session control 提案，丢弃提案并追加
`ToolBusinessEffectsNotApplied` 反馈。恢复不再次进入控制回调，也不把丢弃状态意图解释成外部操作未发生。

## Observer 与事件

工具请求和结果成为 canonical 事实后，分别非阻塞投递 `tool_call_requested` / `tool_call_finished` Observer，并产生
batch/call/approval/AskUser 事件。
Observer 失败隔离为 `plugin_observer_failed`，不改变工具的实际结果。

调用接受、started、批次执行阶段、审批等待与决议、用户回答与再次提问、子结果等待与决议的业务事件，
与各自 Call/Run 状态在同一次原生提交中保存。审批请求不会先于其持久等待状态向 Host 公布。
工具帧完成事件与消息、Call 发布状态和 Driver ready 同提交；中断批次及恢复补齐事件也使用各自的提交边界。
提交后通知不再写入第二条事件，丢失通知时按 Session sequence 追赶。

永久 `tool_call_requested` 只保存 Call 身份及少量展示元数据，不复制 arguments。
永久 `tool_call_completed` 以 Call 身份和固定 `outcome_attempt` 引用确定结果，保留当时的结算反馈，
不复制原始 structured；未知结果没有 Outcome 引用。Application 在观察读取边界内物化 arguments/result_data，
不会用后来变化的当前 attempt 或迟到结果改写历史通知。结果的独立保存仍先于业务结算，通知不承担恢复事实所有权。

## 状态效果与历史代次

工具通过调用范围内的 owner 视图提出状态替换/删除。Host 验证输出和控制转换后，将状态效果与对应结果
消息置于同一 `SessionCommit`；结果消息锚点指向该效果之前的版本。同一次结果提交中，对同一 owner 的多份状态提案明确拒绝。
Session control 的返回或错误作为独立框架反馈保存，历史物化时追加到原始观察；控制失败不得覆盖原始 content、
structured、资源或执行时间。组合后的反馈可以是错误，但原 Outcome 仍保留工具实际返回的成功或失败。
需要独立批次提交状态的工具声明 `batch_behavior="exclusive"`；仅要求 handler 不重叠的工具可声明
`execution_mode="sequential"`。顺序执行不会刷新同一执行阶段的 Harness 快照，不能替代状态提交边界。

超时、取消或调用结束会关闭状态视图。撤销后的旧结果仍可保留执行审计，但不得改变当前 Harness、追加
当前消息或恢复旧 interaction。新执行重新检查许可；文件写入等已发生外部效果不参与会话撤销。

## 验收证据

- `tests/tools/test_tools.py`
- `tests/tools/test_executor_permissions.py`
- `tests/application/test_tool_call_batches.py`
- `tests/application/test_tool_observation_order.py`
- `tests/application/test_parallel_tool_observations.py`
- `tests/application/test_runtime.py`
- `tests/application/test_agents.py`
- `tests/tools/test_builtin_agents.py`
- `tests/tools/test_builtin_shell.py`
- `tests/cli/test_approval_commands.py`
- `tests/cli/test_tool_execution_config_commands.py`

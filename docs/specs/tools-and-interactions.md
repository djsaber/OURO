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
回调可以同步或异步，继续由 `invoke_owned_callback` 隔离同步工作并拥有取消收尾；同步工作结果不确定时保留相应
恢复语义，不因统一签名而遗弃线程或重放副作用。

公开 SDK 的 Tool / resume 回调可只接收调用对象，也可接收执行上下文。贡献适配在装配时确定调用方式并保留
同步、异步属性；非法签名在装配时失败。SDK、MCP 和 generation gate 的输出均遵循上述内部 Tool 签名。

## 内置工具参数

Read、ViewImage、Write、Edit、Glob、Grep、TodoList 和命令行工具各自以参数模型定义字段、默认值、约束和描述。
公开 JSON Schema 从模型生成，处理器在入口解析为类型化参数，再执行文件、进程或 Session 操作。
各工具复用 `ouro.tools.builtin_common` 的参数语义；公共模块不依赖具体工具实现。

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

一次完整 Provider 响应中的所有 tool calls 组成 `ToolCallBatchRecord`。批次保存：

- batch/turn/assistant message identity；
- execution mode 和 max concurrency；
- 每个调用的 request、invocation、approval、result 和 result message；
- planned、interaction、execution、commit、terminal 或 recoverable phase。

批次必须先持久化 assistant tool-call message，再执行外部工具。结果无论顺序或并行执行，都按 Provider
原始 `source_index` 顺序提交并回灌模型。同一完成批次先统一验证全部资源引用，再把缺失的结果消息作为一个
有序 `append_messages` 批次提交；任何结果验证失败不得留下部分消息。提交成功后才投递 tool-finished Observer 并
推进批次状态。恢复时按 tool call id 复用已有结果消息，只批量补交缺失项。

## 顺序与并行

- `sequential`：逐个执行，适合有显式顺序或共享副作用的调用。
- `parallel`：受 `max_concurrency` 限制并发执行。
- AskUser 和审批会在实际执行前完成 preflight；未解决交互时不启动批次副作用。
- SpawnAgent(sync) 可进入 durable `waiting_child`，不会在 handler 内占用 worker 轮询；async 调用仍可与其他兼容
  工具共同执行。
- 一个调用失败不取消其他已经允许执行的调用；批次结果保留每项状态。

## AskUser

AskUser 调用进入 `waiting_user`，保存活动批次和问题。用户答案转换为该调用的 ToolResult，随后继续处理
同一批次。Run id 和 tool call id 必须匹配，不能把答案写入其他 waiting Run。

## Agent 控制与 child HITL

Agent 控制工具的普通 handler 只返回类型化 `AgentControlRequest`，`ToolCallCoordinator` 再把请求交给
application `AgentService`。tools/core 不导入 AgentService、RuntimeService 或 Supervisor。SpawnAgent 的
`agent_id` schema、execution mode 与实际解析都来自同一 decision-bound `AgentCatalogSnapshot`；目录没有可派遣
definition 时省略 SpawnAgent，而不是生成非法空枚举。控制名称属于产品保留集，plugin/MCP 不能覆盖。

child 工具权限先与 parent 可下放的工具、workspace、network/MCP/external-path hard cap 求交，再应用现有
auto/approval/deny 与 ToolPolicy。`tool_selection=all` 不绕过 hard deny。child Approval 与 AskUser 仍保存在
child Run；根 Host 通过包含 delegation/session/run/tool-call/request identity 的 interaction inbox 精确路由，
错投到 sibling 或 parent 明确拒绝。unattended child 对残余审批和输入请求沿用统一确定性策略，不无限等待。

## 取消与进程

`ToolExecutionContext` 提供 abort 检查、可取消 sleep 和 shell 运行。Shell：

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

进程退出时正在执行或提交结果的调用标记为 `unknown_outcome`。恢复不得自动重放这些调用。已经 durable
提交的结果通过 call id 去重；checkpointed result 可安全完成消息提交。

## Observer 与事件

工具请求和结果成为 canonical 事实后，分别非阻塞投递 `tool_call_requested` / `tool_call_finished` Observer，并产生
batch/call/approval/AskUser 事件。
Observer 失败隔离为 `plugin_observer_failed`，不改变工具的实际结果。

## 状态效果与历史代次

工具通过调用范围内的 owner 视图提出状态替换/删除。Host 验证输出和控制转换后，将状态效果与对应结果
消息置于同一 `SessionCommit`；结果消息锚点指向该效果之前的版本。同一 owner 的并行冲突明确拒绝，
需要串行执行的工具复用现有 exclusive 批次规则，不引入另一套调度器。

超时、取消或调用结束会关闭状态视图。撤销后的旧结果仍可保留执行审计，但不得改变当前 Harness、追加
当前消息或恢复旧 interaction。新执行重新检查许可；文件写入等已发生外部效果不参与会话撤销。

## 验收证据

- `tests/tools/test_tools.py`
- `tests/tools/test_executor_permissions.py`
- `tests/application/test_tool_call_batches.py`
- `tests/application/test_runtime.py`
- `tests/application/test_agents.py`
- `tests/tools/test_builtin_agents.py`
- `tests/tools/test_builtin_shell.py`
- `tests/cli/test_approval_commands.py`
- `tests/cli/test_tool_execution_config_commands.py`

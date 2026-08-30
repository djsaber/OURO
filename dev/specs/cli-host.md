# CLI 与 Host 规格

状态：当前

## 目标与范围

CLI 是组合根和终端界面适配器。它解析参数、构建 application 服务、读取用户输入并格式化结果，不能实现
运行、会话、Provider 或工具业务规则。

## 顶层命令

```text
ouro
ouro interactive
ouro run
ouro session
ouro resource
ouro agent
ouro approve | reject | abort
ouro config
ouro provider
ouro mcp
ouro plugin
ouro skills
ouro usage
```

顶层命令名只有当前权威形式，不保留同义 alias。TUI slash command 可保留与正式名互不构成前缀关系的
有意义别名；前缀重复别名和拼写错误兼容不进入命令目录。

## 帮助契约

`--help` 是 CLI 公共接口的一部分，不只是 argparse 自动生成的参数清单：

- 顶层帮助说明无子命令时进入 TUI，并给出独立 Interactive、最短 Provider 与 Run 入门路径；
- 工作流父命令说明资源层级和下一条命令，避免要求用户先阅读外部文档才能发现子命令；
- 有状态差异或破坏性差异的命令必须说明边界，例如 `add/update/replace`、`--runtime-endpoint/--clear`；
- 枚举意图显示合法值；继承、持久化、互斥、严格失败等非显然语义在对应参数或命令说明中表达；
- 复杂工作流提供少量可复制示例，但业务校验仍由 application/core 完成，帮助文本不得成为另一套规则实现。

## 运行模式

### Interactive

`ouro interactive` 启动持续 stdin/stdout Host。支持 prompt、session 切换、新建、模型查看/切换、历史、导出、
Skills 和退出等 slash command。流式文本、reasoning、工具调用、审批和 AskUser 使用独立 I/O 适配器。
Interactive 直接消费 `model_tool_call_delta`：参数分片只更新该工具的单行动态状态，不逐片打印参数；模型
响应完成后仍不展示参数。只有工具进入等待审批状态时，才在工具行及审批提示中显示有界参数摘要，供用户查看和
修改；批准、拒绝或进入执行后立即隐藏参数。不得等到工具预检或执行事件才首次展示调用，也不得把未完成参数
当作可执行 JSON。参数生成、预检、等待交互、执行和完成事件共同驱动按 tool call identity 建立的行生命周期；
流中一旦形成完整 `tool_call_id` 就以它归并后续事件，序号只负责此前的临时定位和显示顺序。每个工具始终只占
一行，状态和动效原位更新，最终留下成功、失败、中止或明确的未知结果，不得由批次状态伪造调用终态。

动态状态由 CLI 终端适配器集中管理：MCP 连接和上下文压缩使用单一可替换状态行，工具调用使用按 key 管理的
稳定多行区域。不同语义使用不同帧集合；新增动画只扩展主题目录，不在事件分支中硬编码帧。真实 TTY 使用异步
动画和 ANSI 行/光标控制原位重绘；重定向输出及测试环境不输出持续帧，每个工具只输出最终稳定行。动画任务在
Run 结束、异常、EOF 和 Host 退出时必须清理。

`/model <provider/endpoint>` 在已有空闲 Session 上持久化切换；没有 Session 时只更新 Host 的下一会话选择。
首条普通消息通过 application 的新会话参数原子校验并持久化该模型，不得为了选择模型提前创建空 Session。

### TUI

无子命令的 `ouro [--session <session_id>]` 启动 Textual full-screen Host。行式 Host 只通过显式
`ouro interactive` 进入，启动后提示 `/help`、`/quit` 以及默认 TUI 入口。

TUI 通过 `TuiController` 调用 `LocalApplication` 的公开服务，通过 Runtime event sink 接收事实；业务运行状态
必须复用 application 的 `reduce_runtime_activity` 投影。Textual 控件只持有视觉状态、焦点和草稿，不解析 Session
文件、SQLite 或工具业务规则。高频 Runtime event 由 Textual 下一显示帧原子取走当前前缀并合并连续同类增量，
每帧至多发布一个 ready message；不得叠加固定周期渲染定时器而降低 compositor 的原生刷新率。结构和完成边界
立即发布，再交给当前 assistant turn；TUI 不为每个 provider chunk 创建渲染任务。

输入草稿的最新修订保存在 TUI 内存状态，磁盘快照采用 300 ms 尾沿防抖；连续编辑只保存静默后的最终快照。
Host 关闭必须等待 pending 草稿完成原子保存，不能把防抖变成退出时的数据丢失窗口。

Actions 的 Subagents 分组通过同一可复用菜单和配置表单依次提供 Dispatch policy、List、Create、Edit 和 Delete。List
是 builtin/user 统一 definition 目录及 compatibility 的可导航页面；builtin 以 disabled 样式保留，user 可进入 Edit/Delete
管理页，独立 Edit/Delete 入口也展示相同的 disabled builtin 分组。Definition 编辑开放 name、description、system prompt、
单选 model 和 all/selected tools；model 默认继承 Session model，agent_id 创建后不可修改。工具 marker 是选择状态的唯一
表达，不重复显示 selected/not selected。

派遣策略是全局 AgentSpawnPolicy，TUI 将当前 definition/model catalog 投影为多选页，并展示 sync/async 和显式能力开关；
用户不手输 agent_id、model route key 或原始 pattern。固定的 depth=1、bound lifecycle 只读展示。Application API 仍完整
保留 pattern 语义。definition 保存从 next Run 生效，派遣策略从 next Runtime 生效。Subagents 的正常导航和成功 mutation
不显示 notification，只有 warning/error 使用现有 notice 通道。

TUI 的实时与重载 transcript 使用同一消息可见性契约：canonical `MessageRecord.scaffold=true` 不进入用户对话面，
`scaffold=false` 才按消息自身 role 投影。`user_message_added`、`host_message_added`、`plugin_message_added` 只表达
producer provenance，不能决定是否显示；实时链收到任一输入事件后按 `message_id` 读取 canonical message，再使用
其 `role/scaffold`。语义标签只影响模型包络，不参与 TUI 可见性。会话选择器只调用 Catalog 投影查询，不能在打开
具体 Session 之前触发 canonical history 扫描或数据质量维修。

每个 assistant turn 分别拥有 reasoning 纯文本行缓冲与 answer 源流。`model_reasoning_delta` 只追加到 dim
reasoning 区域，不能按 Markdown 解释，也不能混入最终回答；首次可见增量创建该 turn 并标记 `THINKING`，activity 离开
`streaming_reasoning` 后收敛为 `THOUGHT`。后续模型轮次再次产生 reasoning 时复用同一区域并恢复 live 状态。
历史恢复读取 canonical `MessageRecord.reasoning_content`，不从展示文本或事件 payload 反推第二份历史状态；没有
可见 reasoning 时不创建空白过程块。

当前交互合同：

- Enter 提交，Shift+Enter 换行，单行边界的 Up/Down 浏览提交历史；
- Composer 以 `/` 开头时自动打开命令窗并实时过滤；Up/Down 选择、Enter 执行、Escape 关闭，命令窗、帮助和
  dispatch 共用 `ouro.tui.commands` 的单一目录。命令完整 `usage` 继续用于帮助和错误提示；Composer 的行内提示
  只显示参数菜单未覆盖、仍需用户键入的语法，选中菜单项后再显示该项自己的后续参数；
- Ctrl+N 新会话，Ctrl+S 会话选择，Ctrl+M 模型选择，Ctrl+P 动作面板，`?` 帮助；
- 版本号只属于欢迎页的系统信息；进入会话页后，顶部 Header 只显示 OURO 标识与当前会话标题，不使用版本号作为
  临时标题；
- 会话内容、Composer 和底部上下文信息共享同一左右 gutter，输入框右边框与上下文信息右端必须和内容区右边界
  对齐；欢迎页继续使用其居中容器的独立边界；
- 切换模型时保留最近有效上下文的 token 估算、固定开销和估算器事实，只用新模型的上下文窗口与输出预留更新容量
  及其派生的剩余量和占比；不能清空状态栏上下文，也不能为此重新扫描会话历史；
- 打开会话是只读展示操作，只在线程中读取指定会话，不隐式执行 Runtime/Agent 恢复；恢复仍由后续运行入口负责。
  执行入口恢复未完成 Run 时按 `run_id` 分页读取恢复所需事件，不加载与该 Run 无关的完整历史事件日志。单个会话因
  版本不兼容或持久化损坏而无法加载时，TUI 复用现有选择视窗显示错误并保持 Host 运行。会话选择 Modal 必须先结束，
  再向主会话视图加载历史，避免主视图的刷新等待被发起加载的 Modal 阻塞。读取和渲染期间，底部状态栏显示统一的
  `Loading session` 动画状态；成功后由新会话投影接管状态，失败后恢复原会话状态；
- 运行中普通提交沿用 Runtime 的 follow-up 排队语义，Host 不复制队列规则；等待消费的输入每项只占一个视觉行，
  统一投影为 `1. Guide • 预览` / `2. Follow-up • 预览`。类型与正文使用 `TUI_DESIGN.inline_separator`
  连接，标签不使用全大写或粗体，正文使用低亮语义色；多行输入先以 `↵` 压平为预览，再按队列内容区的当前宽度
  单行省略，禁止软换行；
- 审批选择面板接管焦点并禁用 Composer，选项只使用 Up/Down 移动、Enter 确认，Left/Right 不承担选项切换；
  计划审批中的 Left/Right 与 PgUp/PgDn 分别复用向前/向后翻阅计划文档的动作。当前选项必须用可见 marker 标识，
  决议提交期间禁止重复输入，失败时保留选择并恢复面板。工具输入请求投影为
  独立问题视窗，支持 Up/Down、数字键、Space、Enter、单选、多选和 Other 自由输入，决议后焦点返回 composer；
- Ctrl+C 在 active Run 中调用协作式 abort，否则退出并由 `LocalApplication.close` 清理资源；
- `/new`、`/sessions`、`/model`、`/clear`、`/abort`、`/help`、`/quit` 与动作面板共享同一 Host action。

提交后的 UI 收敛不能只依赖异步 event sink：实时 sink 提供低延迟，`RunResult.events` 在 Worker 完成时执行同一
幂等投影，保证 sink 延迟或隔离失败时仍能显示规范结果。Host 自有生命周期字段不得使用 Textual Widget 的
`_closing` 等私有名称。

问题视窗只依赖 TUI 自有的 `QuestionForm` 投影，不依赖具体工具 Widget。内置 `AskUser` 适配器保留其
`answers -> question_id -> labels[]` 回答合同；插件提供的常见单问题或问题列表 JSON 复用同一视窗，无法识别的
请求显示有界 JSON 预览并降级为自由输入。问题项标题直接显示 header 与进度，不增加 `?` 等装饰前缀，与工具审批
和计划审批保持相同的无前导符号标题层级。该适配只决定 Host 表现和回答编码，不判断工具业务语义；最终回答仍由
Runtime 恢复原始 waiting Run。自由输入期间以 `/` 开头的文本是回答，不得触发 TUI slash command。

同一工具批次可以同时产生多个 `approval_requested`。TUI 投影必须按 canonical event sequence 保存全部未决交互，
面板始终展示队首，并把该对象携带的 `run_id` 与 `approval_id` 原样提交给 Runtime；不能用单一可覆盖字段，也不能依赖
Runtime 的隐式“当前审批”选择。一个审批解决后移除对应 ID 并立即展示下一项，只有队列清空后才恢复 Composer。

Conversation 是按事件序列增长的时间线，而不是一个可永久回写的 assistant 文本块。首次出现工具活动行时，当前
assistant segment 必须封口；下一轮 `model_reasoning_delta` 或 `model_text_delta` 创建位于工具行之后的新 segment。
高频 delta 可以合并，但只能合并连续且不跨工具状态边界的前缀，不得为减少刷新而重排事件。

answer 活动尾部在 Markdown 结构仍可能变化时按源文本展示：完成视觉行只渲染一次，只有末尾未完成视觉行随新增文本重绘。
短尾部可在新增换行时用共享 Markdown parser 识别并冻结已经完成的顶层块；边界检测受固定字符上限约束，超过上限的
长未闭合块保持纯文本增量展示，直到 turn 结束后再解析一次。冻结块、最终尾块和重载历史统一通过
`assistant_markdown` 渲染。正常帧不得重复拼接完整 answer、构造完整 Rich Markdown，或执行与无界活动块长度成比例
的解析/渲染工作；宽度变化允许显式重建一次 strip 缓冲。

实时渲染的回归用例必须分别让 Provider 在首个 `model_text_delta` 和首个 `model_reasoning_delta` 后保持阻塞，
并在 `runtime.run()` 尚未返回时验证对应活动源流已更新。只用立即返回的 Faux Provider 或只断言最终
`RunResult`，不能作为实时流证据。

Runtime event sink 使用独立有界队列，不能假设入队等于已交付。sink mailbox 必须先完整注册，再创建消费任务；
任务入口直接接收所属 mailbox，不能在启动后反查尚可能未完成的 registry。该顺序是公开运行约束，因为 Textual
在支持时启用 `asyncio.eager_task_factory`，`create_task()` 可在返回前同步执行协程。真正超时、抛错或耗尽有界
队列的 sink 仍按独立失败边界禁用，不能反向阻塞 durable event、其他 Observer 或其他 Session；插件 Observer
继续使用独立 dispatcher。TUI 注册 async sink，回调只把事件放入 UI 自有前缀缓冲、发布至多一个 ready
message，并在跨入 Textual message pump 后让出一次调度，不为每个事件执行线程跳转。

ready message 每次原子取走当前事件前缀：投影仍按 sequence 逐事件执行，文本增量合并为一次
`AssistantTurn.feed`，工具列表、activity 和 pending interaction 只按该前缀的最终规范快照刷新。该合并只减少
展示工作，不丢弃投影事实；`RunResult.events` 仍通过 sequence 幂等补齐未交付尾部。流式验收还必须覆盖超过 sink
容量的 reasoning burst，并直接断言 Run 完成前 `AssistantTurn.source` 已包含可见文本且没有
`event_sink_failed`。该用例必须走 Textual 的 production `run_async` 路径，不能只用不会安装 eager task factory
的 `run_test` 测试循环。

TUI 的实现与验收证据位于 `ouro/tui` 和 `tests/tui`。尚未接通的设计目标不得用占位状态伪装完成，按
TUI 设计文档的实施顺序继续增加资源附件、MCP live reconnect、compaction/subagent 详情、显式 steer 和 cursor
重同步。MCP auto-start 已显示 connecting 动画及 connected/failed/tool-count 汇总，但不代替运行期连接面板。

`/context` 与 `/context full` 读取同一个 application 最近 Provider 请求快照：前者显示记录的 metrics，后者
通过 `SessionService.context` 输出经 digest 校验的完整 `ProviderContext`。`session context [--content]` 使用
相同语义。CLI 不得重新投影 canonical history、重跑 Context Provider，或自行修补 token、system prompt 和
message 内容；没有请求快照时明确显示尚未准备过 Provider context。

Interactive 启动配置了 auto-start MCP 时，先显示 `mcp_connection` 动态状态；连接完成后按 server 输出脱敏的
connected/failed 状态、transport 和工具数量。MCP manager/resource 只暴露类型化连接结果，不依赖终端展示。
Run 启动、审批/AskUser 恢复和恢复后的 Session 刷新处于同一个 Host 错误边界；类型化 application 错误应
显示后继续输入循环，并保留已经返回的 durable Session identity，不得以 traceback 终止整个 Host。

Interactive 的 stdin 读取必须由单一行读取协调器通过非阻塞 async 边界执行，并与
`AgentService.subscribe_agent_tree` 同时等待。同一时刻只能有一个底层 stdin reader；root interaction inbox 出现
subagent approval 或 AskUser 时，Host 显示 agent/delegation 来源及有界参数摘要或完整 input request，把当前提示
重定向为该 interaction 的回答提示，并由同一个 reader 接收答案，不得通过第二个 `input()` 与主提示竞争。
AskUser 的空行不是有效答案，Host 必须继续等待明确输入。决议携带原 child session/run/tool call/request identity
调用 `resolve_agent_approval` 或 `resolve_agent_input`。sync parent
返回 `waiting_child` 时，Interactive 不把它渲染为停止，决议后继续等待原父 Run；async subagent 的 interaction
同样由主窗口处理，但不改变父 Run 生命周期。CLI 只渲染 primary agent 行动轨迹与 subagent HITL 提示，
不展开 subagent 回复或工具调用。Host 只在一次输入协调周期内记住已提交的 request identity，避免 application
接收决议与 canonical inbox 投影收敛之间的短暂重复提示；不得据此保存或推断第二份 pending 状态。Host 不得轮询、
直接恢复 subagent Runtime；订阅在交互完成、EOF、异常和 Host 退出时关闭。

### Print

`run --print` 输出 reasoning 渲染和最终文本，适合人工 shell 使用。未显式选择模式时 `run` 默认 Print。

### JSON

`run --json` 和各管理命令的 `--json` 只向 stdout 输出单个机器可读 JSON；诊断不得混入 stdout。

## 命令资源层级

- `provider add/update/replace/show/list/use/remove/check` 管理供应商；
- `provider add <id> --type <type>` 是唯一创建入口：内置厂商类型自动装载模板，compatible 类型创建空 Provider；
- 同 id 创建失败并提示 `provider update` 或 `provider replace`；内部 Faux 不出现在用户侧 Provider 命令中；
- 模板来源 Provider 的 `provider update` 返回三方合并报告；TUI Provider 子菜单的 `Update from preset` 调用
  同一 application 用例，不在 Host 重写合并规则；
- `provider endpoint ...` 管理具体 Endpoint；`set` 通过互斥的 `--reasoning-preset` 或
  `--reasoning-config` 声明思考契约；
- `session ...` 管理持久化会话；
- `agent definition ...` 管理 builtin/user AgentDefinition 与工具兼容性；
- `agent list|show|spawn|wait|send|interrupt|close|tree|interactions` 查询和控制 durable subagent；
- `config prompt|permissions|tool-execution|queued-inputs|run-budget|agents|compaction` 管理独立配置分区；
  permissions 同时支持 tool 与 plugin 默认权限，agents 覆盖完整的可配置 AgentSpawnPolicy；
- `skills ...` 管理 Agent Skills；
- `approve/reject/abort` 恢复或终止 active Run。

Provider check 的 provider id 使用唯一位置参数；运行时覆盖使用 endpoint 和参数 flags。Provider 创建、更新、
替换和 Endpoint 创建是独立意图；同 id 创建不得静默更新。Skills 单项安装可省略 selector，集合必须明确选择。
`run` 与 `config compaction` 使用统一的 `--thinking-mode`、`--thinking-effort` 输入。Host 只构造
`ReasoningRequest`；能力判断、默认解析和厂商参数编码不进入 CLI。非法的 `disabled + effort` 由统一错误边界
返回配置退出码 `2`，不得输出 traceback。

## Agent 控制面

definition list/show/tools/create/update/delete/copy 通过 provider-independent 的本地 composition 打开
`AgentDefinitionService` 与同一 live Tool catalog；CLI 不读取或修改 config JSON。mutation 使用 expected revision，
builtin general 只可查询或复制，不可编辑、删除或覆盖；目录变更从 next Run 生效。

Application 的 Agent runtime 配置接口覆盖完整 AgentSpawnPolicy。CLI 开放 agent/model pattern、sync/async 执行模式
及 write/network/MCP/external-paths 能力开关；固定的 depth=1 与 bound lifecycle 只展示，不提供无效修改参数。
TUI 通过选择界面投影相同配置，两个入口不另建 policy。

运行命令统一构建 `LocalApplication` 并调用 `AgentService`。短生命周期 CLI 只能基于 completed parent Run 的冻结
Agent catalog 发起 Host delegation，child 固定使用 unattended interaction mode；spawn 与 follow-up 在命令进程内
等待 durable terminal，避免把只存在于即将退出进程的 Supervisor coroutine 宣称为后台 daemon。长期运行 Host 可对
active parent 使用同一 service 并保持 subscription。模型侧派遣仍只允许当前 active parent Run。
CLI spawn 是同步 Host delegation：命令等待 durable terminal 后退出，结果只更新 Host 投影。模型侧 async
SpawnAgent 的 next-boundary 自动投递只属于能持续拥有 parent subscription 与交互策略的长期 Host/模型控制面。

Agent handle JSON 至少包含 delegation/subagent identity、status、relation revision 和 control state；root tree、
interaction inbox 与 subagent stream 都来自 application DTO/cursor。CLI 不自行推断 cascade、permission、
delivery 或恢复状态。

## Home 与覆盖

Home 解析优先级：子命令 `--home`、顶层 `--home`、`OURO_HOME`、`~/.ouro`。System prompt 和运行 Provider
参数可按命令覆盖，但不会隐式持久化。

## Application 边界

- Session 管理命令通过 `ApplicationFacade`。
- Runtime 命令通过 `RuntimeService`；最近 Provider 请求上下文通过 `SessionService`。
- Agent definition 管理通过 `AgentDefinitionService`；Agent 运行控制通过 `LocalApplication.agents` 暴露的
  `AgentService`。CLI 不导入 AgentService 实现，也不读取 Delegation 文件。
- Provider、config 和 Skills 命令通过类型化 `ConfigurationFacade`；公开 DTO 不暴露凭据值。
- CLI 不直接编辑 JSONL、Run 文件或 index。

`session messages/events` 使用 byte cursor 和 limit 增量读取 JSONL；响应返回 `next_cursor`。`run --resource`
可重复指定当前 Session 已导入资源，资源解析与模型投影由 application 完成。

`run --queue-mode follow-up|steer` 在目标 Session 已有 active Run 时选择交付边界；CLI adapter 将连字符形式映射为
application 的类型值。Interactive 中普通 prompt
默认 follow-up，`/follow <prompt>` 与 `/steer <prompt>` 可显式选择。消费批量由配置而不是 Host 决定。

## 输出与安全

- 公开 Provider/Endpoint JSON 脱敏 credential 和敏感扩展字段。
- 错误包含稳定的类型和可操作上下文，不输出 token/key。
- 交互渲染失败与 durable Run 隔离。
- 导出到文件使用明确路径，stdout 导出不夹杂状态文本。

## 退出码

- `0`：成功；
- `2`：配置或参数错误；
- `3`：Provider connection check 失败；
- `4`：等待审批/用户输入；
- `5`：运行中止；
- `6`：工具相关失败；
- `7`：持久化失败；
- 其他 OURO error 由统一映射决定。

同一 RunResult 在 Print 和 JSON 模式下必须得到相同语义退出码。

## 验收证据

- `tests/cli/test_entry.py`
- `tests/cli/test_run_cli.py`
- `tests/cli/test_session_commands.py`
- `tests/cli/test_approval_commands.py`
- `tests/cli/test_error_codes.py`
- `tests/cli/test_permissions_commands.py`
- `tests/cli/test_tool_execution_config_commands.py`
- `tests/cli/test_run_budget_config_commands.py`
- `tests/cli/test_queued_inputs_cli.py`
- `tests/cli/test_agent_commands.py`
- `tests/application/test_agents.py`

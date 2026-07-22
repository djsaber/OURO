# CLI 与 Host 规格

状态：当前

## 目标与范围

CLI 是组合根和终端界面适配器。它解析参数、构建 application 服务、读取用户输入并格式化结果，不能实现
运行、会话、Provider 或工具业务规则。

## 顶层命令

```text
ouro [interactive]
ouro run
ouro session
ouro agent
ouro approve | reject | abort
ouro config
ouro provider
ouro skills
```

命令名只有当前权威形式，不保留同义 alias。

## 帮助契约

`--help` 是 CLI 公共接口的一部分，不只是 argparse 自动生成的参数清单：

- 顶层帮助说明无子命令时进入 Interactive，并给出最短可执行的 Provider 与 Run 入门路径；
- 工作流父命令说明资源层级和下一条命令，避免要求用户先阅读外部文档才能发现子命令；
- 有状态差异或破坏性差异的命令必须说明边界，例如 `add/update/replace`、`--runtime-endpoint/--clear`；
- 枚举意图显示合法值；继承、持久化、互斥、严格失败等非显然语义在对应参数或命令说明中表达；
- 复杂工作流提供少量可复制示例，但业务校验仍由 application/core 完成，帮助文本不得成为另一套规则实现。

## 运行模式

### Interactive

无子命令时启动持续 stdin/stdout Host。支持 prompt、session 切换、新建、模型查看/切换、历史、导出、
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

`/context` 与 `/context full` 读取同一个 application 当前上下文投影：前者显示 metrics，后者输出统一
Provider 边界的完整 `ProviderContext`。`session context [--content]` 使用相同语义。active compaction 变化后
查询必须立即反映摘要投影；CLI 不得读取旧请求快照后自行修补 token 或 message 内容。

Interactive 启动配置了 auto-start MCP 时，先显示 `mcp_connection` 动态状态；连接完成后按 server 输出脱敏的
connected/failed 状态、transport 和工具数量。MCP manager/resource 只暴露类型化连接结果，不依赖终端展示。
Run 启动、审批/AskUser 恢复和恢复后的 Session 刷新处于同一个 Host 错误边界；类型化 application 错误应
显示后继续输入循环，并保留已经返回的 durable Session identity，不得以 traceback 终止整个 Host。

### Print

`run --print` 输出 reasoning 渲染和最终文本，适合人工 shell 使用。未显式选择模式时 `run` 默认 Print。

### JSON

`run --json` 和各管理命令的 `--json` 只向 stdout 输出单个机器可读 JSON；诊断不得混入 stdout。

## 命令资源层级

- `provider add/update/replace/show/list/use/remove/check` 管理供应商；
- `provider add <id> --type <type>` 是唯一创建入口：内置厂商类型自动装载模板，compatible 类型创建空 Provider；
- 同 id 创建失败并提示 `provider update` 或 `provider replace`；内部 Faux 不出现在用户侧 Provider 命令中；
- `provider endpoint ...` 管理具体 Endpoint；`set` 通过互斥的 `--reasoning-preset` 或
  `--reasoning-config` 声明思考契约；
- `session ...` 管理持久化会话；
- `agent definition ...` 管理 builtin/user AgentDefinition 与工具兼容性；
- `agent list|show|spawn|wait|send|interrupt|close|tree|interactions` 查询和控制 durable child；
- `config prompt|permissions|tool-execution|queued-inputs|run-budget|agents|compaction` 管理独立配置分区；
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

运行命令统一构建 `LocalApplication` 并调用 `AgentService`。短生命周期 CLI 只能基于 completed parent Run 的冻结
Agent catalog 发起 Host delegation，child 固定使用 unattended interaction mode；spawn 与 follow-up 在命令进程内
等待 durable terminal，避免把只存在于即将退出进程的 Supervisor coroutine 宣称为后台 daemon。长期运行 Host 可对
active parent 使用同一 service 并保持 subscription。模型侧派遣仍只允许当前 active parent Run。
CLI spawn 固定 `notify_only`；next-boundary/follow-up 可能启动新的 parent Run，只属于能持续拥有 parent
subscription 与交互策略的长期 Host/模型控制面。

Agent handle JSON 至少包含 delegation/child identity、status、relation revision 和 control state；root tree、interaction
inbox 与 child stream 都来自 application DTO/cursor。CLI 不自行推断 cascade、permission、delivery 或恢复状态。

## Home 与覆盖

Home 解析优先级：子命令 `--home`、顶层 `--home`、`OURO_HOME`、`~/.ouro`。System prompt 和运行 Provider
参数可按命令覆盖，但不会隐式持久化。

## Application 边界

- Session 管理命令通过 `ApplicationFacade`。
- Runtime 命令和当前上下文投影通过 `RuntimeService`。
- Agent definition 管理通过 `AgentDefinitionService`；Agent 运行控制通过 `LocalApplication.agents` 暴露的
  `AgentService`。CLI 不导入 AgentService 实现，也不读取 Delegation 文件。
- Provider、config 和 Skills 命令通过类型化 `ConfigurationFacade`；公开 DTO 不暴露凭据值。
- CLI 不直接编辑 JSONL、Run 文件或 index。

`session messages/events` 使用 byte cursor 和 limit 增量读取 JSONL；响应返回 `next_cursor`。`run --resource`
可重复指定当前 Session 已导入资源，资源解析与模型投影由 application 完成。

`run --queue-mode follow_up|steer` 在目标 Session 已有 active Run 时选择交付边界。Interactive 中普通 prompt
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

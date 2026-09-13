# CLI 与 Host 规格

> 文档类型：现行规格
> 代码基线：2026-09-13

## 目标与范围

CLI 是组合根和终端界面适配器。它解析参数、构建 application 服务、读取用户输入并格式化结果，不能实现
运行、会话、Provider 或工具业务规则。

## 顶层命令

```text
ouro
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

- 顶层帮助说明无子命令时进入 TUI，并给出 TUI、Provider 与 Run 入门路径；
- 工作流父命令说明资源层级和下一条命令，避免要求用户先阅读外部文档才能发现子命令；
- 有状态差异或破坏性差异的命令必须说明边界，例如 `add/update/replace`、`--runtime-endpoint/--clear`；
- 枚举意图显示合法值；继承、持久化、互斥、严格失败等非显然语义在对应参数或命令说明中表达；
- 复杂工作流提供少量可复制示例，但业务校验仍由 application/core 完成，帮助文本不得成为另一套规则实现。

## 运行模式

### TUI

无子命令的 `ouro [--session <session_id>]` 启动 Textual 全屏 Host。顶层 `--approval-mode`、`--afk` 和 `--session`
作用于 TUI，不能隐式作用于子命令；子命令参数以各自的 `--help` 为准。

TUI 经 `TuiController` 调用 Application，使用 Runtime 事件与 Session observation 接收事实；运行、审批、
插件命令和持久化规则属于 Application/Core。页面、命令、输入、渲染及异步交互契约集中在
[TUI 规格](OURO_TUI规格_2026-09-02.md)，本规格定义 CLI 入口和普通命令行为。

### Print

`run --print` 输出 reasoning 渲染和最终文本，适合人工 shell 使用。未显式选择模式时 `run` 默认 Print。

### JSON

`run --json` 和各管理命令的 `--json` 只向 stdout 输出单个机器可读 JSON；诊断不得混入 stdout。

## 命令资源层级

- `provider list/show/templates/restore-template/add/update/replace/use/remove/check` 管理供应商与内置模板；
- `provider add <id> --type <type>` 是唯一创建入口：内置厂商类型自动装载模板，compatible 类型创建空 Provider；
- 同 id 创建失败并提示 `provider update` 或 `provider replace`；内部 Faux 不出现在用户侧 Provider 命令中；
- 模板来源 Provider 的 `provider update` 返回三方合并报告；TUI Provider 子菜单的 `Update from preset` 调用
  同一 application 用例，不在 Host 重写合并规则；
- `provider endpoint list/show/set/use/remove` 管理具体 Endpoint；`set` 通过互斥的 `--reasoning-preset` 或
  `--reasoning-config` 声明思考契约；
- `session ...` 管理持久化会话；
- `resource import/list/show/export/remove` 管理 Session 可达的内容寻址资源；
- `agent definition ...` 管理 builtin/user AgentDefinition 与工具兼容性；
- `agent list|show|spawn|wait|send|interrupt|close|tree|interactions` 查询和控制 durable subagent；
- `config prompt|permissions|skills|tool-execution|queued-inputs|run-budget|agents|compaction` 管理独立配置分区；
  permissions 同时支持 tool 与 plugin 默认权限，agents 覆盖完整的可配置 AgentSpawnPolicy；
- `mcp list/show/set/remove/check` 管理 MCP server 配置和连接检查；
- `plugin scaffold/validate/import-report/list/show/commands/run-command/install/configure/select/enable/disable/restart/check/update/reload/rollback/remove/gc`
  管理插件包、运行状态和命令；
- `skills list/show/roots/prompt/validate/install/update/remove/enable/disable` 管理 Agent Skills；
- `usage summary/list/show/modify/reset/audit` 查询并审计 Provider 返回的 Token 用量；
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

- Session 管理命令通过 `SessionService`；本地组合由 `LocalApplication` 提供一致的 SessionRepository。
- Runtime 命令通过 `RuntimeService`；最近 Provider 请求上下文通过 `SessionService`。
- Agent definition 管理通过 `AgentDefinitionService`；Agent 运行控制通过 `LocalApplication.agents` 暴露的
  `AgentService`。CLI 不导入 AgentService 实现，也不读取 Delegation 文件。
- Provider、config 和 Skills 命令通过类型化 `ConfigurationFacade`；公开 DTO 不暴露凭据值。
- CLI 不直接编辑 JSONL、Run 文件或 index。

`session messages/events` 使用 byte cursor 和 limit 增量读取 JSONL；响应返回 `next_cursor`。`run --resource`
可重复指定当前 Session 已导入资源，资源解析与模型投影由 application 完成。

`run --queue-mode follow-up|steer` 在目标 Session 已有 active Run 时选择交付边界；CLI adapter 将连字符形式映射为
application 的类型值。消费批量由配置决定。

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

## 会话撤销与维护

TUI 的撤销、输入恢复和继续执行通过 Application 完成，具体行为见
[会话撤销规格](OURO_会话撤销与Harness状态规格_2026-09-08.md)。

CLI 不提供历史数据或 Harness 状态迁移命令；不支持的数据格式明确报错。

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

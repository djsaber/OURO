# CLI 与 Host 规格

> 文档类型：现行规格

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
ouro data
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
- `provider add <display_name> --type <type>` 是唯一创建入口：内置厂商类型自动装载模板，`custom` 类型必须显式指定 `--protocol` 并创建空 Provider；Provider ID 由框架生成并随结果返回；
- `provider add/update/replace --headers '<JSON>'` 接受 Provider 级请求头声明，值来源为 literal/env/context；省略保留当前值（创建与 replace 使用空配置），`{}` 清空，非空对象整组替换。`show` 和 JSON 结果隐藏所有 literal 值，只公开头名、来源与引用；字段和解析契约见 Provider 配置规格。
- 相同展示名可创建为不同实例；`update`/`replace` 使用框架已返回的 ID 选择目标；内部 Faux 不出现在用户侧 Provider 命令中；
- 模板来源 Provider 的 `provider update` 返回三方合并报告；TUI Provider 子菜单的 `Update from preset` 调用
  同一 application 用例，不在 Host 重写合并规则；
- `provider endpoint list/show/add/update/use/remove` 管理具体 Endpoint；`add` 生成并返回 Endpoint ID，`update` 以该 ID 选择目标；两者通过互斥的 `--reasoning-preset` 或
  `--reasoning-config` 声明思考契约；
- `provider auth methods/login/status/logout/list/forget` 使用轻量认证用例，见下节；
- `session ...` 管理持久化会话；
- `session summary <session_id> [--json]` 通过轻量 SessionService 查询历次保留的压缩摘要，按持久化顺序输出完整正文、生成时间、触发方式与当前生效标记；支持 `--home` 和 `--session-backend`，不构造模型或触发压缩。JSON 返回 `status/session_id/summaries`，空历史为 `summaries: []`；查询错误沿用 CLI 错误与退出码契约。
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
- `usage summary` 支持日期、路由、工作空间、Session、Run、用途、计费模式的组合筛选与重复 `--group-by` 复合分组；`list` 与 `audit` 使用 `--before` 游标分页；
- `usage summary --group-by billing_mode` 按请求时的服务声明分组；summary 展示未知金额请求数及计费模式，list 输出原始归属，未知与部分金额语义见[用量与计费规格](OURO_用量与计费规格_2026-09-17.md)；
- `approve/reject/abort` 恢复或终止 active Run。

Provider check 的 provider id 使用唯一位置参数；运行时覆盖使用 endpoint 和参数 flags。Provider 创建、更新、
替换和 Endpoint 创建是独立意图；同 id 创建不得静默更新。Skills 单项安装可省略 selector，集合必须明确选择。
`run` 与 `config compaction` 使用统一的 `--thinking-mode`、`--thinking-effort` 输入。Host 只构造
`ReasoningRequest`；能力判断、默认解析和厂商参数编码不进入 CLI。非法的 `disabled + effort` 由统一错误边界
返回配置退出码 `2`，不得输出 traceback。

## Provider 认证命令

认证命令通过 `open_local_provider_auth` 装配同一 home 下的 ProviderAuthService、CredentialService 和文件 store。
不构造模型 adapter、Session、Runtime、Agent、插件或 MCP；CLI 不导入 auth/config 实现或访问秘密文件。
内建 openai-codex 浏览器订阅驱动已注册；static 的 methods 为空，login 明确不支持。Provider/Template 的只读 notice 由通用 CLI 展示，包含订阅服务没有请求输出上限的限制；不依据产品名推断能力。正式命令及人工验收见[订阅使用指南](../../docs/OURO_订阅支持使用指南_ZH_2026-09-19.md)。

```text
ouro provider auth methods <provider_id> [--json]
ouro provider auth login <provider_id> [--method <method_id>] [--open-browser] [--json]
ouro provider auth status <provider_id> [--json]
ouro provider auth logout <provider_id> [--json]
ouro provider auth list [--json]
ouro provider auth forget <credential_id> [--json]
```

各叶命令接受 `--home`，覆盖顶层 home；未指定时沿用已有解析顺序。login 不提供后台模式或跨进程 operation 查询：
命令必须等待已保存的终态再退出。只有一种方法时自动选择；多种方法要求显式 `--method`，使用驱动的稳定 id，
不按厂商或菜单序号分派。默认只显示授权信息，`--open-browser` 显式打开 challenge 的 URL；打开失败保留手动路径，
不取消授权，不回显浏览器异常原文。没有 GUI 或 stdin 非交互不影响 URL/设备码展示。

登录 challenge 的 URL/code、有效期和说明只写入本次发起进程的 stderr；这是交互材料，不进入普通状态、Session 或日志。
包括 login 在内的 `--json` 始终只向 stdout 写一个最终 JSON 对象。普通管理结果以 `status=ok` 标识查询/修改完成：
methods 返回 methods；status 返回 authentication；list 返回 credentials；logout/forget 返回 credential 摘要及 bound_provider_ids。
login 返回 operation（含终态及安全错误码）。begin 尚未交付 challenge 就失败/取消时，返回安全 error/error_type/error_code，
不伪造 operation。JSON 失败仍写 stdout，普通文本错误写 stderr。

Ctrl+C 取消本次登录并等待 Application 的资源和提交线程收尾。取消先行返回退出码 5；保存先行则以实际保存结果决定成功或失败，
不能已保存却显示取消。退出时先关闭认证用例，再收尾凭据服务，即使前者清理失败也必须关闭后者。登录不自动执行模型检查。

status 是本地查询：missing、logged_out、reauth_required 等可读取状态返回 0，不宣称远端可用；unavailable 返回 3。
认证失败、存储问题和不支持的认证操作返回 3；方法选择等 CLI 参数错误返回 2；取消返回 5。
logout/forget 只清除本地秘密，不撤销远端会话；共享引用和无绑定记录直接展示，不引入额外确认或配置删除。

Application 状态与条件提交的权威契约见[Provider 认证用例规格](OURO_Provider认证用例规格_2026-09-16.md)。
证据见 `tests/cli/test_provider_auth_commands.py`、`tests/application/test_local_provider_auth.py` 及架构依赖测试。
Runtime 共享认证、通用 TUI 管理及内建浏览器订阅驱动均已接线；现有模型协议的请求认证见[请求认证规格](OURO_请求认证规格_2026-09-17.md)。
模型请求的安全错误码由 run_failed.payload.error_code 传入 run --json 的 events；Host 无需解析错误文本推断认证原因。

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
- `3`：Provider connection check 或认证操作失败、认证状态不可读取；
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

## 数据传输

`ouro data export|import --kind <kind> --path <archive>` 是配置及数据归档的统一命令。
kind 为 `providers`、`provider-templates`、`mcp`、`agents`、`prompts`、`sessions`、`skills`。
会话两种操作均支持重复 `--id` 筛选，省略则全量；技能导出支持按名称筛选。
`--replace` 仅用于导出文件、领域配置及同名技能，不能覆盖会话。
CLI 不读取具体配置文件或解释会话记录；通过组合根装配 `DataTransferService`，输出其类型化结果。
具体边界见[配置与数据传输](OURO_配置与数据传输规格_2026-09-21.md)。

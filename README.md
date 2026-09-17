# OURO Agent

OURO 是一个本地优先的个人智能体运行时。它以可恢复会话为中心，提供模型调用、工具执行、
权限审批、上下文压缩、Skills、MCP、插件和本地可读持久化，并为后续桌面 Host 保留稳定的 Application 边界。

## 当前能力

- 默认 Textual 全屏 TUI，以及 `run --print`、`run --json` 两种单次运行模式。
- 基于 `provider/endpoint` 的模型配置、主流厂商模板、连接检查和会话级模型选择，以及共享安全重试契约的 OpenAI-compatible / Anthropic-compatible 适配。
- 多轮模型/工具循环，支持顺序或并行工具批次、审批、结构化用户输入请求、协作式中止，以及 run 级 follow-up / turn 级 steer 排队输入。
- 会话级 AFK 预设：自动批准可批准的工具，并以 provenance-bearing 无人值守 resolution 处理工具提问，不伪造用户回答。
- [自动任务](docs/OURO_自动任务_2026-09-06.md)：持久化执行计划、工作空间与模型配置，启动后静默调度，
  支持新建/复用会话、审批挂起、状态查询，以及 TUI Task 管理和 USER/TASK 会话域切换。
- 后端中立的会话、消息、事件、运行记录和压缩记录；本地文件与 SQLite 实现共享同一契约，支持显式恢复、
  原子验证导入、搜索、分支、回放、归档、置顶、回收站删除和长历史 cursor 分页。
- 内容寻址的本地资源导入、去重、校验、分支引用复制、导出、可达性清理，以及文本/图片请求内模型投影。
- 进程内 RuntimeSupervisor：跨 Session FIFO 调度、并发上限、有界类型化事件订阅、abort 与 shutdown 协议；
  `LocalApplication` 统一拥有 Runtime、MCP 和后台 worker 生命周期。
- 持久化 LLM 摘要压缩、上下文硬预算和可选的运行软预算。
- 供应商响应驱动的全局 token/费用账本，支持日期、Provider、Endpoint、模型、Session、Run、用途过滤，
  以及分组汇总、审计式修改和选择性重置。
- Read、Write、Edit、Glob、Grep、Session TodoList、宿主 shell 和 AskUser 内置工具。
  AskUser 与插件工具可用同一 opaque JSON 协议挂起 Run 并接收结构化回答。
- 一等子智能体：builtin general 与用户 AgentDefinition、Run-frozen 派遣目录、durable Delegation/独立 child
  Session/Run、sync/async、exact-once 结果交付、HITL 根路由、bound 级联、恢复对账、AgentTree
  subscription，以及 definition/runtime 控制 CLI；子智能体固定为单层并复用统一的工具、权限、预算和 Supervisor。
- 标准 `SKILL.md` 发现、提示词注入、校验、安装、更新、启停和移除。
- 官方 Python SDK 驱动的 MCP 客户端：stdio、Streamable HTTP、legacy SSE、握手、工具发现/调用、
  超时取消、健康检查、重连、凭据注入、隔离诊断和完整关闭。
- 插件管理闭环：严格 manifest、local/Git/archive 安装、不可变 digest store、显式启用与版本选择、
  declarative / Python in-process / stdio worker runtime、原子 owner contribution、Run generation snapshot、
  候选 digest 并排安装/回滚、插件 Skills/MCP、依赖解析、兼容报告、类型化 Application services/DTO 与 CLI；插件还可
  通过 durable continuation policy 和携带显式 role/scaffold/provenance 的输入实现跨 Run 的目标模式，并用
  Host-owned 工具执行上下文与限制性 tool policy 实现 plan mode、危险命令确认和受保护路径等通用扩展；
  类型化 Observer 以独立 mailbox 非阻塞接收已发生事实，Prompt Policy 在直接用户提示持久化前 fail-closed；
  Session Controller 通过声明式 control claim 仲裁长期模式，分别支持模型轮次级 `active_when` 可见性、
  `interaction_controller` 等待交互归属、`/control` 逃生口和 pending interaction 取消；Host 不包含 Plan/Goal 冲突特例。

完整文档从[文档索引](docs/README.md)进入；产品范围、代码分层和精确契约分别见
[项目概述](docs/OURO_项目概述_2026-09-02.md)、
[开发架构](docs/OURO_开发架构_2026-09-02.md)与[规格索引](dev/specs/README.md)。插件作者从
[插件开发指南](docs/OURO_插件开发指南_2026-09-02.md)和[可运行示例](examples/plugins/README.md)开始。

## 开发环境

要求 Python 3.14 或更高版本。

```bash
uv sync --all-groups
uv run pytest --cov=ouro --cov-report=term-missing
uv run ruff check ouro tests
uv run ruff format --check ouro tests
uv run mypy ouro
```

也可以激活虚拟环境后直接运行对应的 `python -m ...` 命令。

## 快速开始

```bash
# 默认全屏 TUI
uv run ouro --home ./.ouro-dev
uv run ouro --home ./.ouro-dev --approval-mode auto
uv run ouro --home ./.ouro-dev --afk
uv run ouro --home ./.ouro-dev --session <session_id>

# 单次运行
uv run ouro run --print --home ./.ouro-dev "hello"
uv run ouro run --json --home ./.ouro-dev "hello"
uv run ouro run --json --home ./.ouro-dev --approval-mode approval "review first"
uv run ouro run --json --home ./.ouro-dev --afk "continue autonomously"

# 继续会话
uv run ouro run --json --home ./.ouro-dev --session <session_id> "continue"

# 选择 SQLite Session 后端
uv run ouro run --json --home ./.ouro-dev --session-backend sqlite "hello"
uv run ouro session list --home ./.ouro-dev --session-backend sqlite

# 子智能体定义与运行策略
uv run ouro agent definition list --home ./.ouro-dev
uv run ouro agent definition tools --home ./.ouro-dev
uv run ouro config agents --home ./.ouro-dev
uv run ouro config agents --home ./.ouro-dev --expected-revision 0 \
  --allow-agent general --allow-model 'openai/*' \
  --execution-mode sync --execution-mode async --no-network

# 基于上面一次 completed Run 返回的 session_id/run_id 派遣无人值守 child
uv run ouro agent spawn <session_id> <run_id> general "inspect one bounded task" \
  --description "Bounded inspection" --request-id cli/example-1 --home ./.ouro-dev
uv run ouro agent tree <session_id> --home ./.ouro-dev
```

未指定 `--home` 时，OURO 使用 `OURO_HOME`；两者都未设置时使用 `~/.ouro`。
Session 后端默认是 `files`，也可通过 `OURO_SESSION_BACKEND=sqlite` 设置当前 Host 的默认值；命令行
`--session-backend files|sqlite` 优先于环境变量。交互、run、session、resource 和审批命令使用同一选择结果。
切换后端不会混读数据，文件和 SQLite 是两个独立数据集；当前格式的跨后端转移通过显式、可验证、可回滚的导入完成。

OURO 处于测试开发阶段，只支持当前数据格式，不提供历史兼容或迁移。遇到版本不符时，请使用新的 `OURO_HOME`
或清理失效测试数据；程序不会自动清空会话。

自动任务配置与执行记录也支持 `files`（默认）和 `sqlite`，通过 `--task-backend files|sqlite` 或
`OURO_TASK_BACKEND` 选择，命令行优先。文件数据位于 `<home>/tasks/`，SQLite 数据位于 `<home>/tasks.sqlite3`；
切换时不混读或自动迁移，已有 SQLite 任务可选择 `sqlite` 继续使用。任务会话始终走上述统一 Session 后端，
不会因任务记录使用 SQLite 而切换会话存储。配置与恢复边界见[自动任务规格](dev/specs/OURO_自动任务规格_2026-09-06.md)。

## 插件

原生插件使用 `.ouro-plugin/plugin.json`。插件是系统级扩展，只分 builtin 与 user；安装、state、CLI 和 DTO
均不定义 workspace/project scope。安装只做静态校验和不可变发布，不 import entrypoint、启动 worker 或 MCP。
用户通过 `enable` 显式接受并运行当前 selected digest；`update` 只并排安装候选，只有显式 `select`、`reload` 或
`rollback` 才切换版本。激活前 Host 重新校验不可变包内容、配置、依赖和 Manifest 契约。

```bash
uv run ouro plugin scaffold ./my-plugin --plugin-id dev.example.plugin --name "Example plugin"
uv run ouro plugin validate ./my-plugin --home ./.ouro-dev
uv run ouro plugin install ./my-plugin --home ./.ouro-dev
uv run ouro plugin enable dev.example.plugin --home ./.ouro-dev

uv run ouro plugin list --home ./.ouro-dev
uv run ouro plugin show dev.example.plugin --home ./.ouro-dev
uv run ouro plugin commands --home ./.ouro-dev
uv run ouro plugin run-command dev.example.plugin:command "argument" --home ./.ouro-dev

uv run ouro plugin update dev.example.plugin ./new-version --home ./.ouro-dev
uv run ouro plugin select dev.example.plugin <candidate_digest> --home ./.ouro-dev
uv run ouro plugin reload dev.example.plugin ./my-plugin --home ./.ouro-dev
uv run ouro plugin rollback dev.example.plugin <package_digest> --home ./.ouro-dev
uv run ouro plugin disable dev.example.plugin --home ./.ouro-dev
uv run ouro plugin remove dev.example.plugin --home ./.ouro-dev
```

`python-inprocess` 是与 OURO 同进程的 full-trust 代码；`worker` 提供进程和协议隔离，但不等于 OS 安全沙箱。
运行这些代码的信任动作是对精确 selected digest 的显式 `enable`、`select`、`reload` 或 `rollback`；Manifest
capability 继续作为 Host service 授权契约，不另设重复的 code-trust grant。`update` 只并排安装候选 digest，
不隐式切换运行版本。
插件配置中的凭据只接受 manifest `credentials.use` 声明槽位对应的 `env:VARIABLE` 引用；worker 只得到临时
环境变量名，秘密值不进入初始化 JSON。`reload` 先并排安装本地候选，只有候选就绪才原子切换；候选失败时旧
Catalog 保持不变。Git source 在 checkout 前按 tree 校验路径、文件数和大小，subdirectory 使用 sparse checkout，
不会先完整检出再校验。Claude/Codex/Pi/OpenCode 等社区包先通过 `plugin import-report` 输出 `supported`、
`converted`、`ignored`、`incompatible` 逐项结论和明确 OURO target，不透明执行未知社区 Hook 或安装脚本。
每个 Run 启动时取得一个结构冻结的 `ExtensionCatalogLease`；工具列表、工具执行、Observer、上下文、压缩、
continuation、Prompt Policy 与 tool policy 在该 Run 内只读取同一 Catalog。插件变更构建候选 Catalog 后只短暂切换 current
pointer；Host registry 不参与 generation 发布，也不会形成全局 Run barrier。已经持有 lease 的 Run 与后续 Run
可以同时使用不同 generation。
configure/select/restart 会先完整构建受影响依赖闭包与候选 Catalog，再以 expected-state 比较提交并切换 pointer；
失败不改变 current selection、active configuration revision 或旧 generation。disable 会关闭 generation gate，
因此已发放的 Catalog 保留结构但不能发起新的插件回调。进程内 Run 由 Catalog lease 保留，生命周期命令无需等待
无关 Run；
跨进程恢复按稳定 `execution_contract_hash` 匹配 package、配置修订、依赖锁、API/protocol 和 contribution contract，
不匹配随机 `generation_id`。插件已更新但旧 digest 与修订仍完整时，会构建只服务 durable Run 的非当前 Catalog，
不改变 current selection 或 Background Service 单例；缺包或契约不一致时明确失败。重复 `enable` 不会隐式重建
generation。

Host 支持的 13 种 contribution 由封闭 `ContributionTypeAdapter` 清单统一接入 SDK/worker、候选准备、contract 和
Catalog；插件不能动态注册新扩展点。Skill/MCP 是 generation-owned 资源，Background Service 是
`activation-singleton`，其余可调用贡献均冻结在 Run Catalog 中。

Plugin Skill 与启动 Runtime 时发现的 user/workspace Skill 进入同一 Run Catalog，按名称拒绝覆盖；每个声明必须
恰好解析一个同名 `SKILL.md`。模型提示词从 Run lease 生成，因此禁用只影响新 Run，旧 lease 仍固定到不可变包。
Plugin MCP 启用即连接：required 失败不发布候选，optional 失败进入 degraded，只有 connected server 产生 contract，
工具结构冻结到该代 Catalog。Python in-process 的 canonical owned module 是 `<namespace>_plugin`，每代实际加载到
`_ouro_plugin_<generation_id>`；bundled dependency 必须位于该 package 并使用相对 import，独立依赖环境使用 worker。

TUI 将插件 command 映射为 slash command：`/<plugin_id>:<name>` 是稳定全名，只有 local name
唯一时才允许 `/<name>`。builtin 始终优先。获得 `sessions.write` 与 `background.run` 的插件 command 可返回
类型化会话输入；producer provenance、`system | user | assistant | tool_result` model role、`scaffold` 和可选
`semantic_tag` 是彼此独立的维度，由输入提供方定义并由框架原样持久化。role 不改变完整 execution-contract
provenance，也不自动授予直接用户权限。Run
continuation policy 可在每轮完成后请求继续、完成、暂停或失败；多个 policy 竞争或 policy 异常时自动暂停。
插件工具可选接收 Host-owned `ToolExecutionContext`，获得当前 workspace、受约束路径解析和协作式取消。Host 统一拥有
内置、MCP 与插件工具的取消任务；运行先进入 `cancelling`，待工具资源收尾与审计状态落盘后才进入 `aborted`。无法安全
强杀且不协作的进程内同步 handler 会被持有到退出并记录为 `unknown_outcome`，不会作为成功结果提交；
插件 manifest 是工具权限的授权契约，运行时 SDK 贡献必须与之相同；受信插件与内置工具使用相同权限语义，
不会因来源降级。`tool_policies` 可把 Host 控制的工具提升为审批或拒绝，但不能批准或改写参数，失败时默认拒绝。

内置 `dev.ouro.goal` Goal 插件提供跨多个有界 Run 的持久目标模式，并随其他 OURO 内置插件
默认启用。可直接使用：

```text
/goal [--token-budget N] <request>
/goal status
/goal pause
/goal resume [--token-budget N]
/goal replace [--token-budget N] <request>
/goal cancel
```

省略 `--token-budget` 即不设预算。一次 Run 的自然结束不会完成 Goal；模型必须通过 `UpdateGoal` 明确报告
`complete` 或 `blocked`。用户保留
pause、resume、replace、cancel 与预算权限。Goal 与 Plan 竞争同一个 primary behavior claim，因此必须先退出或
暂停当前模式再进入另一个模式。完整设计与验收边界见
[Goal 插件](docs/OURO_Goal插件_2026-09-02.md)。

## Provider 与 Endpoint

Provider 用三个独立字段表达配置：`provider_id` 是用户实例标识，`provider_type` 是产品/通用配置类型，
`protocol` 是运行时 adapter 使用的线协议。Endpoint 保存具体模型及其能力。可先查看内置模板，再用一条命令
原子创建 Provider 与模板预置的整组 Endpoint：

```bash
uv run ouro provider templates
uv run ouro provider templates anthropic --json

# 默认引用模板建议的环境变量；此例为 ANTHROPIC_API_KEY
uv run ouro provider add anthropic --type anthropic --home ./.ouro-dev --default

# 可覆盖稳定配置标识、默认 Endpoint 的模型 ID、Base URL 或凭据环境变量
uv run ouro provider add google \
  --home ./.ouro-dev \
  --type gemini \
  --model gemini-3.5-flash \
  --api-key-env GEMINI_API_KEY \
  --default
```

当前模板包括 OpenAI、Anthropic、Gemini、DeepSeek、Qwen、KIMI、GLM、Grok、OpenRouter 和 Mistral；每个模板携带
一个明确的默认 Endpoint、多个当前主流模型 Endpoint、官方文档链接与核验日期。`--model` 和
`--endpoint-id` 只覆盖默认 Endpoint。`add` 根据 `--type` 自动判断是否装载内置模板；Provider id 已存在时会
提示使用 `provider update` 或 `provider replace`。内置类型的 `update` 会基于创建时记录的模板基线做安全三方
合并：未修改的预置值跟随新模板，用户修改或删除的预置 Endpoint、未知 Endpoint 和自定义连接信息保留；新增
模板 Endpoint 自动加入，同名占用会报告而不覆盖。旧版本创建且没有基线的 Provider 首次更新只补缺失 Endpoint
并建立跟踪，不猜测覆盖已有配置。`replace` 则用所选类型的模板完整替换现有 Provider。

CLI 更新会在文本或 `--json` 输出中给出 added/updated/removed/preserved/conflicts 报告；TUI 可在供应商列表选中
Provider 回车后，通过 `Update from preset` 执行同一更新逻辑。模板已删除、禁用或来源发生替换时，更新失败且
Provider 保持不变。

受管理 Provider 可在 TUI 的 `Manage providers → Provider → Authentication` 中登录、查看状态和注销，
或通过 `Manage local credentials` 清理共享及未绑定的本地凭据。登录方法由已注册驱动提供；浏览器只在选择
`Open browser` 时打开，也可复制授权链接手动操作。通用管理已实现，生产 OAuth 与订阅产品尚未注册。

需要完全自定义时，Provider 的创建、更新、替换分别使用 `add`、`update`、`replace`，Endpoint 仍由独立命令
管理，不会隐式创建另一层对象：

```bash
uv run ouro provider add glm \
  --home ./.ouro-dev \
  --type openai-compatible \
  --base-url https://example.test/v1 \
  --api-key-env GLM_API_KEY \
  --retry-attempts 3 \
  --retry-initial-backoff 0.5 \
  --retry-max-backoff 8

# update 不允许改变 provider type/protocol
uv run ouro provider update glm --home ./.ouro-dev --timeout 90

# 非内置厂商直接选择兼容协议类型
uv run ouro provider add private-model \
  --home ./.ouro-dev \
  --type anthropic-compatible \
  --base-url https://models.example.com

uv run ouro provider endpoint set glm reasoning \
  --home ./.ouro-dev \
  --model model-name \
  --temperature 0.2 \
  --context-window 200000 \
  --max-output-tokens 16000 \
  --tools --streaming --structured-output \
  --reasoning-preset thinking-toggle \
  --default

# Anthropic-compatible 自定义模型也可声明 adaptive thinking 与 effort
uv run ouro provider endpoint set private-model claude \
  --home ./.ouro-dev \
  --model custom-claude-model \
  --reasoning-preset anthropic-adaptive-effort \
  --default

# 单次运行覆盖统一的思考意图；不支持的模式或等级会在发起 HTTP 前失败
uv run ouro run --home ./.ouro-dev \
  --provider glm --endpoint reasoning \
  --thinking-mode enabled --thinking-effort high \
  "Analyze this problem"

uv run ouro provider use glm --home ./.ouro-dev
uv run ouro provider check glm --endpoint reasoning --home ./.ouro-dev
uv run ouro provider list --home ./.ouro-dev
uv run ouro provider endpoint list glm --home ./.ouro-dev
```

`--reasoning-preset` 提供 `none`、`openai-effort`、`reasoning-content-effort`、
`thinking-toggle`、`enable-thinking-toggle` 和 `anthropic-adaptive-effort`。Preset 是用户对目标模型协议能力的
显式声明，不是自动探测；若模型只支持部分等级，或参数值与 preset 不同，应使用
`--reasoning-config '<JSON>'` 完整声明 capability 与各 variant 的原生请求片段。思考配置归 Endpoint 所有，
因此 `openai-compatible` 和 `anthropic-compatible` 的任意自定义模型都使用同一套配置与运行时 flags。

TUI 欢迎页可通过 `/workspace <path>` 为尚未创建的会话选择工作空间；输入命令后回车可从最近 20 条
历史目录中选择，也可继续输入其他目录。会话一旦创建，工作空间即固定，需使用 `/new` 返回新会话欢迎页后
再选择其他目录；`/workspace clear` 只清空最近工作空间记录，不改变当前会话的工作空间。Actions 的
`Settings` 分组统一提供主题、思考内容展开方式、输入草稿记忆、排队输入交付方式和上下文压缩策略；关闭草稿
记忆会清空已保存草稿。排队输入交付方式与压缩策略的更新在下一次启动时生效。

会话中的模型路由始终使用完整的 `provider/endpoint` 形式。交互模式可通过 `/models` 查看模型，
通过 `/model provider/endpoint` 选择模型；若会话尚未创建，该选择会用于下一条普通消息创建的首个会话，
否则切换当前会话的模型。活动 Run 已发出的请求继续使用原模型，下一次模型请求重新读取会话路由。
`/thinking` 显示当前 Endpoint 的有效思考设置与可选值；
`/thinking default|enabled|disabled|minimal|low|medium|high|xhigh|max` 设置当前会话（或下一新会话）的
供应商中立思考意图。每个会话独立持久化该设置，共享同一 Endpoint 的会话不会相互影响；不受 Endpoint
支持的模式或等级会在状态变更前被拒绝。切换模型时也会重新校验当前会话设置，不会静默降级或重置。有多个
effort 的 Endpoint 使用显式 `default_effort` 解析未带等级的 `enabled`，不依赖厂商默认。TUI 菜单只显示不同的
有效配置：可切换且支持 effort 时显示 `disabled` 和各 effort，不支持 effort 时显示 `enabled`、`disabled`；
`Alt+T` 按相同集合循环，不经过重复的 `default` 状态。

## 会话与运行控制

TUI 的 `/undo` 撤回最近一条直接用户输入；有待消费的排队输入时，优先取消最新一条。
已进入历史的输入连同其后的消息被撤销，会话 Harness 状态恢复到该消息之前。正文、附件引用和技能调用
返回编辑草稿，编辑后可重新提交；连续撤销可逐次回溯。原生状态和插件状态遵守同一规则。

撤销 canonical 消息前需先中断活跃运行并完成取消结算。撤销取得执行所有权后，原子发布消息切口、状态指针
和新的历史代次，再结清受影响的输入租约与子会话关系。工具已经造成的文件与
外部系统变化、真实用量账本不回滚。旧运行及事件保留用于审计，不能重新写入撤销后的当前历史。

`/continue` 在当前历史代次恢复合法的中断、失败或暂停检查点，保留原 Run 身份、已提交工具结果与累计用量。
撤销后则基于保留消息和恢复后的 Harness 创建新 Run，不重复追加保留输入，也不复用旧代次的审批或工具执行。
不完整工具交换由 Provider 请求投影处理，不再隐式裁剪 canonical 消息。约束仍未满足时显示具体原因。

使用方式与边界说明见 [消息撤销与会话继续](docs/message-history-editing.md)。

```bash
uv run ouro session list --home ./.ouro-dev --search auth --no-archived
uv run ouro session show <session_id> --home ./.ouro-dev
uv run ouro session context <session_id> --home ./.ouro-dev
uv run ouro session context <session_id> --home ./.ouro-dev --content --json
uv run ouro session rename <session_id> "Auth refactor" --home ./.ouro-dev
uv run ouro session tags <session_id> auth review --home ./.ouro-dev
uv run ouro session approval-mode <session_id> auto --home ./.ouro-dev
uv run ouro session afk <session_id> on --home ./.ouro-dev
uv run ouro session branch <session_id> <message_id> --home ./.ouro-dev
uv run ouro session replay <session_id> --home ./.ouro-dev --json
uv run ouro session messages <session_id> --home ./.ouro-dev --cursor 0 --limit 100 --json
uv run ouro session events <session_id> --home ./.ouro-dev --run <run_id> --cursor 0 --limit 100 --json
uv run ouro session compact <session_id> --home ./.ouro-dev
uv run ouro session archive <session_id> --home ./.ouro-dev
uv run ouro session remove <session_id> --home ./.ouro-dev --force

uv run ouro approve <session_id> --home ./.ouro-dev
uv run ouro reject <session_id> --home ./.ouro-dev --reason "unsafe"
uv run ouro abort <session_id> --home ./.ouro-dev
uv run ouro run --session <session_id> --queue-mode steer "Change direction"

uv run ouro resource import <session_id> ./attachment.pdf --home ./.ouro-dev
uv run ouro resource import <session_id> ./context.txt --home ./.ouro-dev
uv run ouro resource list <session_id> --home ./.ouro-dev
uv run ouro resource show <session_id> <resource_id> --home ./.ouro-dev
uv run ouro resource export <session_id> <resource_id> ./copy.pdf --home ./.ouro-dev
uv run ouro resource remove <session_id> <resource_id> --home ./.ouro-dev --force
uv run ouro run --session <session_id> --resource <text_resource_id> "Use this context"
```

`session remove` resolves the owning primary Session and moves its complete subagent tree to one
trash entry. It first requests canonical abort for genuinely live Runs and waits for their locks,
then acquires the complete tree lock set and discards stale unfinished records. Tree scope never
depends on deserializing historical Run or Delegation records.

模型附件当前支持有大小上限的 UTF-8 文本/JSON/XML，以及 vision Endpoint 上的 PNG、JPEG、GIF、WebP。
PDF 可存储和导出，但在接入专用解析 adapter 前不能直接投影给模型。

资源通过 `resource import` 导入会话，再由 `run --resource <resource_id>` 附加到输入。TUI 可以恢复撤销消息的
资源引用；手动选择新附件的 Composer 界面尚未提供。

`run --queue-mode follow-up|steer` 选择排队交付边界。follow-up 在当前 Run 结束后进入后续 Run，steer 在当前
完整 model turn 结束后进入同一 Run。TUI 在 Composer 中提供同一输入交付模式。
`session context [--content]` 读取最近一次真实 Provider 请求的指标或完整 system prompt、messages 和 tools；
TUI 可从 Actions 查看相同的上下文快照。

批准模式是会话状态，不是工具配置：对于默认 `permission_control="host"` 的工具，`inherit` 沿用工具定义和
`config permissions` 的结果，`auto` 自动执行，`approval` 要求审批，`deny` 拒绝调用（`AskUser` 在可执行时
仍按用户问答流程处理，而不是转成普通工具审批）。单次运行可使用
`--approval-mode` 创建或更新空闲会话；TUI 可通过 `/approval` 或审批选择器切换，活动 Run 也可使用。已经产生的
审批保持原状态，下一次工具审批边界读取新模式。会话分支继承源会话的批准模式。

AFK 是应用层预设，不是新的权限枚举：`AFK = approval_mode:auto + interaction_mode:unattended`。可用
`--afk`、TUI `/approval afk` 或 `session afk <session_id> on|off` 原子切换。无人值守模式不会让 Run
停在 `waiting_user`：支持该能力的 resumable tool 收到显式 `UnattendedResolution`；其他工具得到可反馈给模型的
`UserInputUnavailable` 结果。AFK 只自动批准可批准的调用，不覆盖 Tool Policy `deny`、工具硬拒绝、schema、路径、
执行契约或取消边界。会话分支同时继承 interaction mode。

Harness 编排工具可声明 `permission_control="tool"`，此时工具定义的权限是唯一执行依据，只允许 `auto` 或
`deny`；用户同名权限覆盖、会话批准模式和限制性 `tool_policies` 均不参与决策。该控制权适用于内置和受信
插件工具，适合进入/退出计划模式、目标模式等必须保持可调用的控制面操作。

## 全局 Token 与费用统计

全局账本按每次完成的供应商模型请求记录 usage，覆盖普通 Agent 回合和上下文压缩请求。这里的 token
只来自 Provider 响应；`ContextBuilder` 对上下文长度的本地估算仅用于预算和触发压缩，绝不会作为账本
缺失值的回退来源。`estimated_cost` 的框架契约固定为 USD；只有 adapter 能保证该语义时才可自动填入，
否则保持未定价状态，可按账单手动修订。

```bash
# 全局汇总；裸日期按 UTC 自然日处理，--until 裸日期包含当天
uv run ouro usage summary --home ./.ouro-dev
uv run ouro usage summary --home ./.ouro-dev \
  --since 2026-07-01 --until 2026-07-15 \
  --provider anthropic --model claude-sonnet-5 --group-by date --json

# 请求级明细及更多过滤维度
uv run ouro usage list --home ./.ouro-dev --purpose compaction --limit 100
uv run ouro usage show <usage_entry_id> --home ./.ouro-dev --json

# 修改和重置都是追加式审计操作，不会悄悄覆写或删除历史事件
uv run ouro usage modify <usage_entry_id> --home ./.ouro-dev \
  --input-tokens 1234 --estimated-cost 0.42 --note "provider invoice correction"
uv run ouro usage reset --home ./.ouro-dev --model old-model \
  --note "start a new accounting period" --force
uv run ouro usage audit --home ./.ouro-dev --limit 100 --json
```

`summary`、`list` 和 `reset` 共享 `--since`、`--until`、`--provider`、`--endpoint`、`--model`、
`--session`、`--run`、`--purpose` 过滤器，重复同一参数表示匹配其中任意值。Provider adapter 会把
`input_tokens` 统一为包含缓存读写的总输入，cache read/write token 保留为其中的独立明细；
`total_tokens` 只计算 input + output，避免重复计入缓存明细。
全局账本不从 `run.usage` 猜测或回填缺少请求路由与请求边界的记录；统计从账本实际记录的供应商调用开始。
人工修改或重置只改变可审计的全局费用视图，不改写
Session 中保存的原始 Run usage。

## MCP Server

MCP server 配置保存在 `OURO_HOME/config/config.json` 的 `mcp.servers`。启用且 `auto_start` 的 server 会随
Host 启动在后台连接，不阻塞交互模式、`run` 或工具审批恢复。连接成功后，发现的工具会原子地以
`<server_id>__<tool_name>` 加入当前工具注册表；连接前或连接失败时其他 Agent 功能保持可用，Host 退出时统一关闭。
server 必须显式设置 `auto_start: true` 才会进入默认 Host，MCP 工具权限默认为 `approval`。

配置可通过 CLI 完整管理，无需直接修改 JSON：

```bash
# stdio；以 --arg=... 形式传入以 "--" 开头的子进程参数
uv run ouro mcp set filesystem --home ./.ouro-dev \
  --command npx --arg=-y --arg=@modelcontextprotocol/server-filesystem --arg=. \
  --auto-start

# Streamable HTTP；凭据引用优先使用环境变量
uv run ouro mcp set remote --home ./.ouro-dev \
  --transport http --url https://mcp.example.com/mcp \
  --credential Authorization=env:MCP_AUTHORIZATION --auto-start

uv run ouro mcp list --home ./.ouro-dev
uv run ouro mcp show remote --home ./.ouro-dev --json
uv run ouro mcp check remote --home ./.ouro-dev --json
uv run ouro mcp remove remote --home ./.ouro-dev
```

重复的 `--arg`、`--env`、`--inherit-env`、`--header` 和 `--credential` 会整体替换对应集合；使用
`--clear-args`、`--clear-env`、`--clear-inherit-env`、`--clear-headers` 或 `--clear-credentials` 显式清空。
stdio 与 HTTP/SSE 之间切换时，CLI 会原子移除旧 transport 的不兼容字段和凭据引用。`mcp check` 只做一次
临时连接检查，即使持久配置为 disabled 也不会改写它；连接失败退出码为 3，配置错误为 2。

```json
{
  "mcp": {
    "servers": {
      "filesystem": {
        "name": "Filesystem",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
        "auto_start": true,
        "credential_refs": {
          "FILESYSTEM_TOKEN": "env:FILESYSTEM_TOKEN"
        }
      },
      "remote": {
        "name": "Remote MCP",
        "transport": "http",
        "url": "https://mcp.example.com/mcp",
        "auto_start": true,
        "credential_refs": {
          "Authorization": "env:MCP_AUTHORIZATION"
        }
      }
    }
  }
}
```

stdio 中 `credential_refs` 的 key 是注入子进程的环境变量名；HTTP/SSE 中则是请求 header 名。环境变量的
值应包含完整 header 值，例如 `MCP_AUTHORIZATION="Bearer ..."`。MCP 状态由单一连接 owner 实时维护为
`starting`、`connected`、`reconnecting`、`failed` 或 `stopped`；运行中 Host 的 MCP 状态查询读取同一实时记录。
TUI 会在自身生命周期内同步该内存快照，实时更新连接中、已连接、失败和当前可用工具数量。
单个 MCP 连接失败不会进入同步启动失败，也不会阻止没有依赖该 server 的普通 Run。`inline:` 引用会以明文保存在
本地配置中，公开 CLI 输出统一显示为
`inline:***`，因此生产配置应优先使用 `env:`。

## 配置、Agent 指令与 Skills

```bash
uv run ouro config prompt --home ./.ouro-dev --set "Custom system prompt"
# Shell 工具名取决于宿主检测结果；Windows 通常为 PowerShell，POSIX 通常为 Bash
uv run ouro config permissions --home ./.ouro-dev --tool PowerShell --mode approval
uv run ouro config permissions --home ./.ouro-dev --tool Write --mode deny
uv run ouro config permissions --home ./.ouro-dev --plugin dev.example.plugin --mode approval
uv run ouro config tool-execution --home ./.ouro-dev --mode parallel --max-concurrency 4 \
  --workspace-only --max-output-chars 100000
uv run ouro config queued-inputs --home ./.ouro-dev --consumption-mode all-at-once
uv run ouro config run-budget --home ./.ouro-dev --max-model-rounds 50
uv run ouro config compaction --home ./.ouro-dev --provider glm --endpoint reasoning
uv run ouro config skills --home ./.ouro-dev --disable-source codex.user

uv run ouro skills list --home ./.ouro-dev
uv run ouro skills show pdf --home ./.ouro-dev
uv run ouro skills roots --home ./.ouro-dev
uv run ouro skills validate ./path/to/skill --json
uv run ouro skills install ./path/to/skill --home ./.ouro-dev
uv run ouro skills install github:owner/repo --skill skill-name --ref v1.0.0
uv run ouro skills disable skill-name --home ./.ouro-dev
uv run ouro skills update skill-name --home ./.ouro-dev
uv run ouro skills remove skill-name --home ./.ouro-dev
```

默认启用的 `dev.ouro.agent_context` 内置插件会在每次模型请求时加载开放规范的 `AGENTS.md`：依次读取
`<user-home>/.agents/AGENTS.md`、`OURO_HOME/AGENTS.md`，再从 Git 项目根目录到当前 Session workspace
逐层拼接 `AGENTS.md`。越靠后的项目规则越具体并在冲突时优先；合计上限为 32 KiB。没有 Git 根时不会向
workspace 外继续搜索。该机制只接受 `AGENTS.md`，不会把产品私有的 `CLAUDE.md`、`GEMINI.md`，或具有不同
生命周期的 `MEMORY.md`、`SYSTEM.md`、`SKILL.md` 当作别名。它追加规则而不替换基础 system prompt，并保留
原有请求级系统环境信息注入。

Skills 使用标准 `SKILL.md` YAML frontmatter。用户级原生目录是 `OURO_HOME/skills`，工作区原生目录是
`./.ouro/skills`；也可扫描 `.agents`、`.claude`、`.github`、`.copilot` 和 `.codex` 的 Skills 目录。
目录来源按 Host 与作用域使用稳定 id，例如 `ouro.user`、`ouro.workspace`、`codex.user` 和
`codex.workspace`。所有来源默认启用，可通过 `ouro config skills --disable-source <id>` 独立关闭；
TUI 的 `Actions > Skill sources` 提供相同的总开关与逐源开关，并即时刷新当前 Runtime；
自定义 `skills.paths` 条目使用自身的 `enabled` 字段。插件 Skill 属于插件贡献契约，继续通过所属插件启停，
不受目录发现源开关影响。格式错误会产生明确诊断，OURO 不会猜测或修复无效 frontmatter。

## 配置示例

`OURO_HOME/config/config.json` 使用分区结构：

```json
{
  "prompts": {
    "system": "Custom system prompt"
  },
  "tool_execution": {
    "mode": "parallel",
    "max_concurrency": 4,
    "workspace_only": true,
    "max_output_chars": 100000
  },
  "queued_inputs": {
    "consumption_mode": "all_at_once"
  },
  "run_budget": {
    "max_model_rounds": 50,
    "max_elapsed_seconds": 1800
  },
  "skills": {
    "enabled": true,
    "disabled": ["unsafe-community-skill"],
    "disabled_sources": ["claude.user"],
    "paths": [
      {
        "path": "shared-skills",
        "source": "user",
        "enabled": true
      }
    ]
  },
  "mcp": {
    "servers": {}
  },
  "providers": {
    "default": "glm",
    "items": {
      "glm": {
        "provider_type": "openai-compatible",
        "protocol": "openai-chat-completions",
        "provider_id": "glm",
        "credential_ref": "env:GLM_API_KEY",
        "base_url": "https://example.test/v1",
        "api_path": "/chat/completions",
        "retry_policy": {
          "max_attempts": 3,
          "initial_backoff_seconds": 0.5,
          "max_backoff_seconds": 8.0,
          "backoff_multiplier": 2.0
        },
        "default_endpoint": "reasoning",
        "endpoints": {
          "reasoning": {
            "model_id": "model-name",
            "temperature": 0.2,
            "context_window": 200000,
            "max_output_tokens": 16000,
            "supports_tools": true,
            "supports_streaming": true,
            "supports_structured_output": true,
            "reasoning": {
              "capability": {
                "activation": "toggle",
                "supported_efforts": [],
                "default": {"mode": "enabled", "effort": null},
                "output": "text",
                "continuation": "text"
              },
              "profile": {
                "name": "thinking-object-toggle",
                "variants": {
                  "enabled": {"thinking": {"type": "enabled"}},
                  "disabled": {"thinking": {"type": "disabled"}}
                }
              }
            }
          }
        },
        "timeout_seconds": 60,
        "enabled": true
      }
    }
  }
}
```

凭据优先使用 `env:VARIABLE_NAME` 引用。CLI 和公开 JSON 输出不会回显凭据值。

## 项目文档

- [文档索引](docs/README.md)：现行说明、规范、规划和调研的完整入口。
- [项目概述](docs/OURO_项目概述_2026-09-02.md)：当前范围、核心概念和未来边界。
- [开发架构](docs/OURO_开发架构_2026-09-02.md)：模块职责、依赖方向和关键不变量。
- [规格索引](dev/specs/README.md)：领域契约、失败/恢复语义和验收证据。
- [规划索引](dev/plans/README.md)：仅列尚需执行或验证的工作。
- [贡献指南](CONTRIBUTING.md)、[安全策略](SECURITY.md)、[版本能力摘要](CHANGELOG.md)与[发布流程](RELEASING.md)。

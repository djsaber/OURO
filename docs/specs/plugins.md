# OURO 插件系统规格

> 文档类型：现行规格

本文定义 OURO 插件系统的规范性契约。插件包创建、SDK 调用和操作示例见
[插件开发指南](../development/plugin-development.md)。

不受支持的 Manifest、state、Plugin API 与 worker protocol 版本严格拒绝，不提供历史兼容或迁移。

## 1. 产品边界

OURO 是强扩展的个人智能体。插件是系统级扩展模块，不是项目资源，也不是面向多租户或企业分发的应用平台。

插件只有两类：

- `builtin`：由 OURO 装配的内置插件；
- `user`：用户安装到当前 `OURO_HOME` 的插件。

以下资源与插件作用域相互独立：

- Skills、提示词和项目规则可以来自 user 或 workspace；
- workspace 可以作为 Tool、Observer、Policy、Command 和 Run 的执行上下文；
- workspace 不能发现、安装、覆盖、启用或禁用插件。

## 2. 目标

插件系统必须满足：

- **系统级扩展**：插件是一等 application resource，而不是 CLI 脚本或 prompt 拼接约定；
- **深层能力**：支持 Tool、Command、Observer、Context、Compaction、Skills、MCP、Background Service、Run
  Continuation、Prompt Policy 和 Tool Policy；
- **Host 中立**：插件 API 不依赖 CLI、TUI 或 Desktop 对象；
- **Run 一致性**：一个 Run 使用一个不可变 Extension Catalog，不在执行中混用不同插件代际；
- **原子替换**：候选插件完整启动后才影响新 Run，失败不改变当前可用版本；
- **可恢复**：durable record 按执行契约恢复，不按偶然的进程实例或同名 contribution 猜测；
- **本地可控**：安装、配置、启停、选版、数据、诊断和删除都由本地 `PluginService` 管理；
- **必要信任边界**：明确 full-trust 与 process isolation，不宣称不存在的 OS sandbox；
- **可演进内核**：新增 contribution 类型有统一内部接入边界，但用户插件不能动态定义扩展点。

## 3. 非目标

当前目标不包括：

- project/workspace/managed plugin scope 或多 scope overlay；
- 对恶意 in-process 或 worker 插件的 OS 安全沙箱；
- Marketplace、远程分发 registry、签名、publisher trust root 或企业策略中心；
- 每插件虚拟环境、自动 pip/npm 安装和第三方构建脚本；
- 自动 worker 重启、熔断和复杂 backoff policy；
- 任意 HTML/Python widget、Host 专用 UI DSL；
- 万能事件总线、共享可变 middleware、任意 request/response transformer 或反向 `host.call`；
- 插件直接取得 `RuntimeService`、`SessionStore`、数据库连接或全局 registry；
- 对社区可执行插件 API 的透明仿真；
- 对开发期旧格式、旧 API 和旧本地状态的兼容层。

## 4. 强制不变量

1. `ouro.core` 不读取 Manifest、不 import 插件代码、不依赖安装和 worker adapter。
2. Host 只调用 Application service/DTO，不直接操作插件目录、state、registry 或 import adapter。
3. Package、Install、Activation、Generation 和 Extension Catalog 是不同对象。
4. User plugin 安装后默认 disabled；只有显式 enable/select/reload/rollback 才接受并运行目标 digest，且配置、
   依赖和包完整性必须先通过校验。
5. Provenance 由 installer 根据真实来源生成，Manifest 不能自报 builtin、verified 或可信等级。
6. Builtin identity 永久保留，user plugin 不能覆盖 builtin contribution。
7. Contribution 必须先完整 prepare，再经 catalog transaction 原子发布；失败不得部分可见。
8. 候选 generation 构建、插件回调和外部 I/O 不得在 catalog swap 临界区执行。
9. 新 Run 获取不可变 catalog；current catalog 更新不改变已有 Run。
10. User plugin Tool 始终经过 `ToolExecutor`；Manifest 不能单方面授予自动执行权。
11. Tool Policy 只能提升审批或拒绝，不能批准、放宽 Host policy 或修改参数。
12. Observer 只能异步读取不可变事实，不能阻断、改写或同步反压 canonical Session/Run、durable sequence
    或用户消息来源。
13. Command 和 Continuation 只返回类型化意图，只有 application 层可以持久化消息、排队和创建 Run。
14. 插件输入的 producer provenance、model role、`scaffold` 和可选 `semantic_tag` 必须分离：始终保留 plugin、
    contribution 和 execution contract 来源；每个消息属性由插件输入显式声明，Host 不相互推断。
15. 所有 task、进程、MCP、listener 和 connection 都属于一个可关闭 generation owner。
16. Disable 后的每次插件调用必须经过 generation gate；关闭 gate 后不得开始新调用。
17. 依赖未 active 时依赖者不得 active。
18. 状态查询和管理命令必须使用同一 observed-state reconciler。
19. Pending tool/approval 恢复必须验证发布引用（user package digest 或 builtin contract hash）和 execution
    contract，不能按同名新工具替代。
20. 只支持当前格式，不提供旧 schema、插件身份或 API 别名的兼容转换。
21. 依赖契约变化时必须原子重建受影响的 active dependant closure，不能在新 Catalog 中保留旧 dependency lock。
22. 候选阶段不得执行自主后台副作用或破坏性修改共享 plugin data。
23. Prompt Policy 必须在用户提示持久化和 Run 创建前、基于 prospective Run 的同一 Catalog lease fail-closed；
    拒绝不得留下消息或 Run。

## 5. 领域模型

```text
PluginPackage
  manifest + immutable files
        |
        v
InstalledUserPlugin
  source facts + package digest
        |
        v
PluginActivation
  selected digest + desired state + active configuration revision
        |
        v
PluginGeneration
  process-local owner + instance + resources + prepared contributions
        |
        v
ExtensionCatalog
  immutable contribution directory leased by Runs
```

### 5.1 Plugin identity

- `plugin_id` 使用反向域名风格，安装后不能由更新改变；
- `version` 使用 SemVer，描述作者发布版本；
- `package_digest` 是规范化包文件树的 SHA-256；
- `generation_id` 标识当前进程内资源 owner；
- `execution_contract_hash` 标识可跨进程重建的运行契约；
- `configuration_revision_id` 是规范化配置和 credential binding reference 的内容摘要，不包含秘密值；
- 相同 version、不同 digest 是不同内容，不能静默覆盖。

### 5.2 Plugin class

`PluginClass = builtin | user`。

Builtin plugin：

- 由 application composition root 提供；
- 由显式、不可变的 builtin definition registry 提供，不扫描 workspace、`OURO_HOME` 或用户插件目录；
- definition 包含稳定 identity、version、Manifest contribution/dependency contract、in-process factory、
  `default_enabled`、显式 `runtime_resources` 和由规范化 Manifest 与运行输入派生的 builtin contract hash；
- 与 user plugin 复用同一 generation、gate、owner、contribution preparation、catalog transaction 和 durable
  execution contract；builtin 不是 Host baseline，也不建立第二套 runtime；
- 不进入 user install/update/remove 流程；
- 不经过 user install/select/remove 流程；
- identity 和公开名称不能被 user plugin 覆盖；
- 以稳定 version 和 builtin contract hash 参与 dependency lock；实现内容变化即使忘记提升 version，也必须产生不同 release identity。

Builtin contract hash 的输入格式版本为 2，包含规范化 Manifest、definition root 下全部 Python 源文件，
以及 `runtime_resources` 声明的包内相对文件路径与内容摘要。插件读取的非 Python 提示词、模板、schema 等
随代码发布的运行资源必须显式列入；缺失的声明资源使 definition 构造失败。普通说明文档不作为运行输入，
判断依据是是否参与执行，不是文件名或扩展名。外部工作区指令、Session 制品和用户配置属于动态业务输入，
继续由各自的读取与持久化契约管理。插件间依赖通过 dependency lock 纳入 execution contract，
SDK 和 Host 通过既有 API、贡献与执行契约约束，不把整个应用文件树纳入局部插件摘要。

User package digest 仍覆盖完整规范化包文件树，用于安装内容身份与完整性检查；不能套用 builtin 的运行输入范围。
`builtin_contract_hash` 作为 release identity，并参与 execution contract。

`builtin | user` 是代码供应类别，`declarative | python-inprocess | worker` 是运行方式，二者相互独立。Manifest
不能声明 plugin class，也不设置可由 user package 伪造的 `runtime.kind = builtin`。Builtin 使用 Plan/Goal
模式所需的 `python-inprocess`、空 configuration 和无 package resource 形态；其他 runtime、configuration、Skill/MCP
package resource 只有在真实 builtin 用例形成共同契约时扩展，不预建第二套配置、凭据或资源协议。

启用的 builtin 必须拥有正常 `PluginGeneration`。只有不可关闭、属于 Agent 正确运行前提的 Host/Core facility 才进入
Catalog baseline；“随 OURO 分发”本身不足以把一个可关闭模块放入 baseline。

User plugin：

- 安装到当前 `OURO_HOME`；
- 每个 plugin id 可以保存多个 installed digest，但只有一个 selected digest；
- 每个 plugin id 只有一份 desired enablement 和 active configuration revision reference；
- 不包含 workspace、managed 或 development activation scope。

`PluginService.show` 的安装列表按 SemVer 与 package digest 降序返回；排序规则由应用层拥有，CLI/TUI
直接消费 DTO，不导入插件内部版本解析器。

### 5.3 Persistent 与 observed state

持久化只保存事实和用户意图：

- installs、provenance 和 selected digest；
- desired enablement；
- 每 digest 的 immutable configuration revisions 与 active revision reference；
- user plugin data reference。

Builtin code/definition 不写入 user install state。`state.json` 另存稀疏的
`builtin_activation_overrides[plugin_id].desired_enabled`；没有 override 时使用 definition 的 `default_enabled`。
显式 enable/disable 必须保存 override，以免后续产品默认值变化覆盖用户选择。当前 OURO 版本不存在的 builtin override
作为 dormant intent 保留，不视为 state 内容损坏。

运行时状态由 reconciler 投影：

- `invalid` / `incompatible`；
- `disabled`；
- `needs_configuration`；
- `ready`；
- `starting` / `active` / `degraded` / `blocked` / `stopping` / `failed`。

`active` 等瞬时状态不能作为崩溃后的 canonical 事实写回 state。

## 6. 原生包格式

Manifest 固定为 `.ouro-plugin/plugin.json`。其他位置的 `plugin.json` 只是普通文件。

建议布局：

```text
calendar-plugin/
  .ouro-plugin/
    plugin.json
  calendar_plugin/
    __init__.py
  skills/
    calendar/SKILL.md
  mcp/
    calendar.json
  schemas/
    config.schema.json
```

### 6.1 Manifest 示例

```json
{
  "schema_version": 3,
  "plugin_id": "dev.ouro.calendar",
  "name": "Calendar",
  "version": "1.0.0",
  "description": "Calendar tools and context.",
  "namespace": "calendar",
  "engines": {
    "ouro": ">=0.1.0,<0.2.0",
    "plugin_api": "^3.0.0"
  },
  "runtime": {
    "kind": "python-inprocess",
    "entrypoint": "calendar_plugin:create_plugin"
  },
  "contributes": {
    "tools": [
      {
        "id": "search",
        "exposed_name": "calendar__search",
        "description": "Search calendar events.",
        "permission_mode": "approval"
      }
    ],
    "commands": [
      {"id": "today", "description": "Show today's schedule."}
    ],
    "skills": [
      {"id": "calendar", "path": "./skills/calendar"}
    ],
    "mcp_servers": [
      {"id": "calendar", "path": "./mcp/calendar.json", "optional": false}
    ]
  },
  "permissions": [
    {
      "id": "credentials.use",
      "scopes": ["calendar_api"],
      "reason": "Authenticate calendar requests."
    }
  ],
  "configuration": {
    "schema": "./schemas/config.schema.json",
    "version": 1
  },
  "dependencies": {
    "plugins": []
  }
}
```

### 6.2 Schema 规则

- 规范对象只消费已声明字段；未知字段由统一 OURO 模型边界忽略，不能进入授权、运行决策或再次序列化；
- 已声明的 security、runtime、contribution 和 permission 字段仍按类型、必填项与领域约束严格校验；
- `metadata` 只保存 namespaced 非规范元数据，不参与授权和运行决策；
- Manifest 不包含 credential 值、用户授权结果、source trust 或 plugin class；
- contribution identity 和简要契约必须在执行前静态可见；
- `optional_feature`、MCP `auto_start` 和无消费者 placeholder 字段不存在；
- schema 变化直接更新 schema version 和所有调用方，不保留开发期旧 reader。

### 6.3 Package 路径

路径字段必须：

- 以 `./` 开始并使用 `/`；
- 不包含空段、`.`、`..`、drive、UNC 或 alternate stream；
- 解析后位于 package root；
- 指向安装时存在的普通文件或目录。

包拒绝 symlink、junction、reparse point、device、socket、FIFO、VCS 和 cache 目录。materialization 必须有文件数、
单文件、总大小、archive compression 和 Git 时间/下载边界。

## 7. Runtime

### 7.1 Declarative

- 没有 entrypoint；
- 用于 Skills、MCP descriptors 和明确支持的静态资源；
- 启用 generation 时启动其全部 MCP；
- 必需 MCP 失败使 activation 失败；可选 MCP 失败使 generation degraded。

用户显式选择并启用新 package digest，即接受其中静态资源变化。

### 7.2 Python in-process

Python factory 在 OURO 进程中运行，是 full-trust runtime：

- 只有显式 enable/select/reload/rollback 接受的 selected digest 可以执行；
- permission 只约束 Host service，不能限制直接 Python/OS 调用；
- entrypoint 使用 plugin-owned 顶层 namespace；
- top-level module 冲突时拒绝 activation；
- Manifest `namespace=<name>` 对应唯一 canonical Python 顶层名：若 `<name>` 已以 `_plugin` 结尾则直接使用，
  否则使用 `<name>_plugin`；entrypoint 的首段必须与之相同；
- Host 不把 canonical 名写入 `sys.modules`，而是把每个 generation 加载到
  `_ouro_plugin_<generation_id>` 私有 module tree，使同一插件的新旧代可并存并可按 owner 完整卸载；
- bundled dependency 必须位于 plugin-owned package 内并使用相对 import；需要主环境之外第三方依赖的插件使用
  worker，不增加每插件 venv 或通用 import virtualization；
- 插件不能向 OURO 主环境动态安装依赖；
- `prepare` 不启动长期资源、不修改全局 registry；
- 候选 `start` 只建立可并存资源；通过 Host data API 的写入先进入 generation-local staging，发布成功后才提交；
- 自主长期任务必须声明为 Background Service，不能用普通 supervised task 绕过 service gate；
- `start/stop` 使用 generation owner，stop 幂等且有 deadline。

### 7.3 Worker

Worker 是语言无关的 JSON-RPC/stdio 进程边界：

- stdout 只承载协议；stderr 限流、脱敏；
- request 有大小、timeout 和 correlation id；
- timeout 与外层 task cancellation 都发送 `cancel`；
- worker exit 使 generation failed，并撤销新调用入口；
- Host shutdown 关闭 worker 进程树；
- worker 不被描述为 OS sandbox；
- 当前只支持显式 restart，不要求自动 restart/backoff/circuit breaker；
- 反向 Host method 只能为真实能力逐项增加，禁止万能 RPC。

Worker 返回的 contribution 必须是 Manifest 静态声明集合的子集；缺少必需项或返回未声明项使 prepare 失败。

调用响应由对应方法的解码器同时校验并构造最终 SDK 输出，handler 直接返回该对象。
片段、Session 输入、批次与控制转换在同一次响应中只解码一次；Tool call/resume 共享结果解码契约。
外部类型、默认值、互斥输出和语义约束仍在 worker 边界检查，SDK 到 core 的转换保留各层职责。

## 8. Plugin API

插件只依赖 `ouro.sdk.plugins.v2`，不依赖 `ouro.core.*`、`ouro.application.*`、具体 registry
或 Host 类型。

```python
def create_plugin(context: PluginContext) -> PluginInstance: ...

class PluginInstance(Protocol):
    async def prepare(self) -> ContributionSet: ...
    async def start(self) -> None: ...
    async def stop(self, reason: StopReason, deadline: float) -> None: ...
```

### 8.1 PluginContext

Context 只暴露稳定、必要的能力：

- identity、provenance 摘要和 generation；
- 只读 configuration；
- plugin-private data view；候选读取共享数据，但写入/删除进入隔离 staging，active 发布后才提交到共享 data；
- credential slot resolver；
- supervised tasks；
- redacted logger 和 diagnostics；
- 已授权 Host-service permission 集合；
- 可选的 plugin-scoped artifact root，用于用户可见、可由 Host 文件工具处理的制品；它与 plugin-private data 和
  canonical Session 数据分离。

不暴露 RuntimeService、SessionStore、全局 registry、Host UI、原始配置文件或 credential store。

以 Session 为作用域且影响后续模型行为的业务状态属于原生 Harness。它不经 PluginContext 写入：
调用方通过 `call.session_state` 获得框架绑定 owner/Session 的短期视图；`read()` 返回独立值，
`replace(...)` 与 `delete()` 只暂存变化。框架随合法输出接受提案并提交，异常或取消丢弃提案。
回调结束、关闭或取消后，保存到后台任务或线程的旧视图失效。业务状态不复制到插件私有文件中。

Observer、Context、Prompt/Tool/Turn Policy、Compaction、read_only Tool 获得只读视图；
Command、状态变更 Tool、Session Controller、Continuation 获得可暂存视图。没有目标 Session 的调用中视图为 None。
`branch_policy` 与 semantic projection tags 仍通过替换操作声明；插件不传 owner_id、session_id 或版本条件。
框架将状态、模式关系、输出和等待阶段按 `SessionCommit` 结算；嵌套 Controller 从同一操作的暂存值
读取，输出无效或冲突时不发布部分状态。状态不变的调用不创建版本。具体锚点和撤销契约见
[Harness 规格](session-undo.md)。

Worker 随调用在 params 根对象接收 `session_state = {writable, value, controls, run_controls, usage_tokens}`，
后面三个字段为 owner 只读控制关系、请求绑定和原生用量投影；其中 value 为
`{state_schema_version, payload}` 或 null。返回根对象省略 session_state 表示无变化，null 表示删除，
对象表示替换（字段与 SDK replace 一致）。SDK 与 worker 经过同一个状态边界；不增加反向状态写 RPC。
Worker 先完整解码方法输出，再通过框架绑定的状态视图验证并暂存提案；两者成功后才交付结果。
无效输出不暂存状态，无效状态提案不交付输出；成功暂存仍不等于持久化，最终接受与提交属于 application。

### 8.2 Request context

需要 workspace/session/run 的能力通过每次调用的 request context 传入，不能缓存到调用结束以后。Tool context 至少
提供 workspace、Host artifact path resolution、abort 状态和可取消 sleep。worker 获得等价的序列化字段和 cancel。
Session Controller 的 activate/deactivate call 还必须提供 Host 已知的 `session_kind = primary | subagent`；插件不能
从 session id、父目录或私有存储推断 Session 类型。

### 8.3 Session input

插件 command 可返回单条 `SessionInput`，或返回一个或多个 `SessionInputBatch`；同一 command 结果不能混用两种
形态。batch 必须非空、保持组内顺序且所有成员使用相同 delivery mode。Application 以一个 Session mutation 原子
接受完整 batch，并在同一安全边界把整组转为 canonical messages；`one_by_one` 只能选择完整消费单位，不能拆分
batch。SDK、in-process adapter、worker protocol、CLI 和 TUI 使用同一类型化契约。

`role` 只表达 `system | user | assistant | tool_result` 四种模型消息角色；`scaffold` 只表达 Harness 是否有意注入，
`semantic_tag` 只表达可选的模型可见语义，producer provenance 独立保存。插件必须定义这些值，Application 仅校验
和传递，不根据任何一个维度推导另一个。任意 role 都可作为脚手架并可选择语义标签；这不会把 producer 改成直接
用户，也不会自动授予用户权限。只有标签存在时，模型投影才使用统一 semantic envelope renderer。可选
`coalesce_key` 只允许新批次替换同一 Session、plugin owner 和 contribution 下
尚未消费的旧控制投影；不能删除 canonical message、跨 owner 搜索、重排或建立通用优先级。

### 8.4 API 与协议版本规则

- `plugin_api` 和 worker protocol 使用显式版本；
- 不兼容变更须明确提升相应版本，并检查公开调用方及持久化契约影响；
- Host 不提供 deprecated API 周期或旧 protocol adapter；
- 更新必须同步 Manifest schema、SDK、worker、测试、示例和文档；
- durable record 依赖 contract hash，不依赖 Python 类型名。

## 9. Contribution 与 Catalog

### 9.1 目标稳定 contribution

- `tools`；
- `commands`；
- `observers`；
- `context_providers`；
- `prompt_policies`；
- `turn_policies`；
- `compaction_strategies`；
- `skills`；
- `mcp_servers`；
- `background_services`；
- `continuation_policies`；
- `tool_policies`；
- `session_controllers`。

Host capability 清单由以下 13 类组成。这里的“消费者”指真正解释该类型语义的 Host 边界，Catalog projection
本身不算消费者；“证据”至少包含 builtin、可运行 example 或跨 SDK/worker/application 的契约测试。

| capability | 生命周期 | producer / contract 证据 | Host 语义消费者 | 等级 |
|---|---|---|---|---|
| `tools` | snapshot | Goal/Plan builtin、worker-tool example | `ToolCallCoordinator` / `ToolBatchExecutor` | stable |
| `commands` | snapshot | Goal builtin、inprocess-command example | `PluginService.execute_command` 与 Host command route | stable |
| `observers` | snapshot | inprocess-observer example、observer contract tests | `ObserverDispatcher` 的有界 mailbox | stable |
| `context_providers` | snapshot | System Information/Goal builtin、worker semantic contract | `ContextPreparationService` | stable |
| `compaction_strategies` | snapshot | SDK/worker/contract adapter 与 compaction tests | `ContextPreparationService` 的 strategy selection | stable |
| `skills` | snapshot | declarative-skill example、plugin skill catalog tests | Runtime prompt/context preparation | stable |
| `mcp_servers` | snapshot | generation resource/MCP catalog contract tests | MCP runtime 投影到 Run-local `ToolRegistry`，随后走 Tool consumer | stable |
| `background_services` | activation singleton | in-process/worker lifecycle contract tests | `PluginRuntime._handover_background_services` | stable |
| `continuation_policies` | snapshot | Goal builtin、worker semantic contract | `RunContinuationService` | stable |
| `prompt_policies` | snapshot | inprocess-prompt-policy example | Runtime prompt admission boundary | stable |
| `turn_policies` | snapshot | SDK/worker semantic contract、Runtime turn tests | Runtime terminal-candidate boundary | stable |
| `tool_policies` | snapshot | Plan builtin、SDK/worker semantic contract | `ToolCallCoordinator` preflight 与 execution recheck | stable |
| `session_controllers` | snapshot | Goal/Plan builtin、worker semantic contract | `SessionControlCoordinator` 及 Run resume/settlement | stable |

每种 capability 必须同时具备 Manifest/SDK 表达、Host adapter 和真实语义消费者。系统保持 13 类 capability；
新类型必须由真实 builtin 或最小可运行 user plugin 驱动。失去消费者的类型应同步从 Manifest、SDK、worker、
prepare、contract、projection 与文档删除，不保留空 adapter 或迁移别名。

观察型扩展只公开 `observers`；`prompt_policies` 是独立语义边界。Manifest schema 3 不接受 `hooks` 字段；
Plugin API 使用 3.0，worker protocol 使用 3.0；Python SDK 入口为 `ouro.sdk.plugins.v2`。

### 9.2 Identity 与冲突

Canonical identity：

```text
<plugin_id>/<kind>/<local_id>
```

- command 对外使用 `<plugin_id>:<local_id>`；
- Tool 暴露名使用 Manifest namespace；
- builtin identity 和 builtin tool name 永久保留；
- user plugin 之间的冲突必须使候选 catalog 构建失败并报告双方 owner；
- load order 不能成为未声明的覆盖规则；
- Host 别名只能影响显示，不能改变 durable identity。

### 9.3 ContributionTypeAdapter

Host 为每个稳定 contribution type 提供内部 adapter，统一承担：

- declaration validation；
- SDK/worker adaptation；
- conflict 和 optional 规则；
- contract hash；
- catalog projection；
- owner cleanup 和 diagnostics。

Adapter 不是公开动态扩展点。不同 contribution 的组合和失败规则继续由各自类型定义。Adapter 还必须声明类型是：

- `snapshot-bound`：投影到 Run Catalog，可由旧、新 generation 并存；
- `activation-singleton`：不进入 Run 可调用目录，每个 plugin id 同时只允许一个 generation 的 gate 打开。

Tools、Commands、Observers、Context、Prompt Policy、Turn Policy、Compaction、Skills、MCP tool projection、
Continuation、Tool Policy 和 Session Controller 是 snapshot-bound；Background Service 是 activation-singleton。Session Controller 的
activation 本身属于 durable Session state，不属于进程级插件 activation。

`ouro/plugins/contribution_types.py` 是唯一 Host-owned 清单。每条记录组合 SDK、worker、prepare、
optional、contract、resource 与 projection facet；facet 保留各类型独立语义，Catalog、Coordinator、worker 与
generation resource 主流程只遍历清单，不维护第二份 capability 分支。新增 Host capability 必须通过该清单接入。
该清单没有注册 API，用户插件不能追加类型。
Skill/MCP resource facet 只为成功加载/连接的资源生成 contract；可选资源失败只产生 degraded observed state。

### 9.4 ExtensionCatalog

Catalog 是不可变对象，包含 builtin 和 active user generation 的 snapshot-bound 贡献目录及 owner metadata。
Background Service 由 activation owner 管理，不是 Catalog callback。Catalog 构建必须：

1. 从 current catalog 和候选 generation 形成候选集合；
2. 验证全部 identity、依赖、必需 contribution 和 contract；
3. 不执行插件代码或外部 I/O；
4. 成功后原子替换 current catalog pointer；
5. 为旧 catalog 保留独立 lease，直到使用它的 Run 结束。

基础 user/workspace Skill 与 Plugin Skill 是同一 Catalog 中的 `SkillMetadata`。Skill 名称全局唯一；与 Host baseline
或其他 active user generation 冲突时，候选 Catalog 整体失败，不存在加载顺序覆盖。每个插件 Skill 声明必须恰好
解析一个 `SKILL.md`，且 frontmatter `name` 等于 contribution id。完整 Skill 正文仍按需要从 Catalog 固定的
immutable package path 读取，不能重新发现 current plugin state。Catalog 对外返回防御性副本，消费者不能修改
共享 Skill metadata。

## 10. Observer、Policy 与 Continuation

### 10.1 Observer 与语义类型

公共扩展点不使用万能可变 output。稳定类型按语义分类：

| 类型 | 示例 | 组合 | 失败 |
|---|---|---|---|
| observation | runtime_event、context/tool/run facts | 每个 registration 独立 FIFO，不改变语义 | 异步隔离 |
| enrich | context provider | 追加带 owner 的 typed fragment | optional 省略，required 阻断 |
| transform | 当前未开放；未来只允许具体 typed patch | 字段级授权、重新校验 | 拒绝无效 patch |
| guard | prompt/tool policy | allow/deny 或只允许收紧 | fail-closed |
| turn guard | turn policy | allow 或带 feedback 的同 Run continue | fail-closed |
| lifecycle | generation start/stop | 幂等、有 deadline | 按阶段失败 |

Observer 接收 frozen、以事件名为判别字段的 typed observation，handler 返回 `None`。Application 只执行非阻塞
enqueue，不等待回调；每个 registration 有 generation-owned、有界、FIFO mailbox 和独立 consumer。queue full、
timeout、异常或取消只影响该 observer，不能改变模型、工具、Run 或 Session 的结果。投递为进程内 at-most-once；
可靠审计以 canonical event store 为准。diagnostic 走统一的 Host 通道，不通过无人消费的返回值传递。

Observer provenance 只用于归属、过滤、诊断和 contract，不建立不同 observer 之间的业务顺序。`runtime_event` 可声明
精确 `event_types` allowlist；Host 在入队前过滤和脱敏。队列项持有精确 generation lease，处理、丢弃或取消后恰好
释放一次；generation retire 和 Host shutdown 按 deadline drain，随后取消并回收。

Context Provider、Prompt Policy、Tool Policy 和 Turn Policy 是独立 registry，不通过 Observer 承载语义。只有在
producer、typed input/output、组合、失败和授权规则同时明确时才增加新的 observation 或 semantic contribution。

### 10.2 Context Provider

Context Fragment 显式声明 `persistence = ephemeral | message`，默认 `ephemeral`，并独立携带 role、scaffold 与
可选 semantic tag（SDK 默认值是显式契约值 `role=system, scaffold=true, semantic_tag=None`）。`message` 表示该
片段必须在模型请求前由 application 以提供方给出的消息属性和 provider/generation provenance 持久化为 canonical
message；
Run/model round/provider/fragment index 构成幂等 delivery identity。持久化失败阻断模型请求，不能退化为临时注入。
该机制服务需要审计和恢复的 Harness 消息，不把所有动态 Context Provider 输出强制写入历史。

Fragment 的 `target = message | system_prompt` 默认是 `message`。`system_prompt` target 只允许 ephemeral：
Application 在基础 system prompt 和 Skills 之后按 provider/fragment 稳定顺序追加片段，但不得为它构造 canonical
message。两种 target 都可以由 producer 通过可选 `semantic_tag` 声明语义；标签存在时由 Core 的同一 renderer
校验、转义正文中的同名包络分隔符并构造 model-visible envelope，不存在时正文保持原样。producer 不得把包络
写入 `text`。投影结果计入 token 预算与请求 digest。Context Provider call 可以读取 Host 提供的 Session workspace。

### 10.3 Prompt Policy

Prompt Policy 在 Host command routing 确认输入是模型提示后、持久化 user message 和创建 Run 前执行，返回
`allow | deny`。

- Application 先获取 prospective Run 将使用的 `ExtensionCatalogLease`，并用同一 lease 评估全部 active policy；
- 全部 allow 后才持久化提示并把该 lease 转交新 Run；deny、timeout、异常或非法返回均关闭 lease，不创建消息或 Run；
- 多策略按 canonical identity 稳定评估，`deny` 短路；
- Policy 只能拒绝，不能改写提示、注入 Context、创建 Session/Run 或改变 input source；
- 被拒绝提示正文不进入 state、event、log 或 diagnostic，只允许记录无正文的 attributed audit summary；
- User plugin 声明该 contribution 时必须请求 `sessions.prompt.inspect`。

Plugin command、Host 管理命令、审批回答和 ToolInput resolution 使用各自现有类型化边界，不进入 Prompt Policy。

### 10.4 Tool Policy

`ToolPolicyDecision = ignore | require_approval | deny`。

- 多策略按稳定 identity 排序，取最严格结果；
- `deny` 短路；
- 不能改写参数、批准工具或降低 Host permission；
- 对 host-controlled 和 tool-controlled 工具统一执行；`permission_control="tool"` 只绕过 Host permission override；
- 调用输入包含 canonical tool identity、owner、effect、batch behavior、validated arguments 和 Session/Run context；
- 审批修改参数后以最终参数重新评估；
- timeout、异常和非法返回视为 deny；
- 决策 owner 和 reason 进入 invocation provenance。

### 10.5 Turn Policy

Turn Policy 在模型给出无工具的候选完成、Run 尚未完成时执行，返回 `allow | continue`。

- `continue` 必须提供 feedback，由 application 与候选 assistant message 一起持久化到同一 Run；
- 多策略按 priority 和 canonical identity 稳定评估；任一 continue 使当前 Run 进入下一模型轮次；
- timeout、异常和非法返回暂停 Run，不能静默完成；
- 它不创建新 Run，也不替代只消费终态 `RunOutcome` 的 Continuation Policy。

### 10.6 Continuation Policy

Continuation 接收只读 `RunOutcome`，返回 `ignore | continue | complete | pause | fail`。

- `continue` 必须包含 prompt 和 `follow_up|steer`；
- policy 对所有 terminal RunOutcome 执行；非 `completed` Run 只允许 `pause | fail`，不能 `continue`；
- 非 continue 决策可以携带一个 release `session_control_transitions` local intent；Application 只允许它释放同 plugin、
  且由该 Run checkpoint 捕获的精确 activation；
- 一个非 ignore 决策由 application 执行；
- 多个控制决策、timeout 或异常使自动续跑暂停；
- application 先持久化 generation-independent settlement plan，再通过原生提交将 Harness、release、queue
  和 checkpoint resolve 一起幂等发布。恢复完成决定，不重调已经形成该决定的插件；
- terminal Run 的结算必须先从 Run metadata 恢复该 Run 绑定的精确 extension Catalog；存在持久化 Catalog 绑定时不得
  回退到当前全局 registry。Catalog 暂时无法恢复时保留 checkpoint 为 pending，并在恢复成功前拒绝启动后续 Run；
- 精确 Run Catalog 已恢复后，activation-bound policy 不可用、timeout、异常或返回非法终态时，Host 只
  fail-closed release 其精确 activation，
  并记录 owner 私有清理未确认；
- 插件不能自行创建 Run 或伪造 user message。

### 10.7 Session Controller 与激活绑定

长期改变一个 Session 行为的插件必须使用 Host-owned Session Controller 协议，不能只把“模式已开启”写入插件私有
JSON。Controller 使用 `<plugin_id>/session_controllers/<local_id>` canonical identity，静态声明一个或多个
`domain + exclusive|shared` claim，以及有界、幂等的 activate/deactivate callback。

- Command 和 Tool 只返回 `acquire | release | replace` transition；Application 在 Session mutation 边界内校验
  generation/execution contract、expected revision 和 claim 冲突，再提交 activation；
- 依赖目标 Session 才能执行的 Command 必须在 Manifest 声明 `requires_session: true`。Application 在调用 handler 前
  强制该前置条件；交互命令用例在尚无当前 Session 时，按 Host 传入的模型与交互策略创建并绑定 Session。默认
  `false`，无状态命令不能因此隐式创建 Session；
- acquire payload 传给 activate，release payload 对称传给 deactivate；owner 私有终态应由幂等 deactivate 消费
  release payload 后提交，不能先改私有状态再尝试释放 Host activation；
- 同 domain 的 exclusive/exclusive 或 exclusive/shared 在 activate 前拒绝；不同 domain 或 shared/shared 可共存；
- 冲突不能按 priority、加载顺序或 last-command-wins 自动解决，Host 不维护 Plan/Goal/第三方插件冲突表；
- `replace` 仅允许同一插件内部显式交接，插件不能替换其他 owner 的 activation；跨插件切换必须先明确 release；
- Harness 版本只发布已经完成准备的 activation，包含 activation id、controller id、
  release/generation/execution contract、claims 和 revision；activate/deactivate 在提交前生成提案，
  失败不产生半激活状态，启动和 undo 不重放 Controller 回调；
- Tool、Context Provider、Prompt Policy、Tool Policy、Turn Policy、Continuation Policy 和 Observer 可用
  Manifest/SDK 的
  `active_when=<controller-local-id>` 绑定 activation；Manifest 引用未知 controller 时拒绝；
- Runtime 在每个模型轮次开始时把 active activation refs 写入 Run metadata，并以该快照筛选本轮贡献。工具在本轮
  acquire 后从下一轮生效；并发 release 不得放宽已开始轮次的 Tool Policy；
- activation-bound Tool 的 pending interaction 在 controller release 时以 `SessionControlReleased` 取消，旧回答不能
  恢复或重新激活；由正在 resolve 的同一 Tool 发出的 release 不取消自身提交；
- Host 提供 `/control status`、`/control release <activation-id|controller-id>`、`/control release --all`，并允许
  `--force` 在 owner contract/callback 不可用时只撤销 Host gate。force release 必须明确说明私有清理未确认。

Tools 与 Commands 默认保持可见，以便提供 acquire/status/release 入口；只有显式声明 `active_when` 的 Tool 才会从模型
tool list 和执行 registry 中按轮次隐藏。插件回调可以读取业务私有状态，但不能把它当作 Host activation 的真实性来源。

### 10.8 Tool contract 与用户输入恢复

每个工具显式声明 `effect = read_only | state_changing | unknown`、
`batch_behavior = compatible | exclusive` 和 resumable 能力。builtin 与插件在定义处声明；MCP 依次采用用户 override、
协议只读注解或 `unknown`，不做名称/描述启发式推断。归一化结果进入所有 contribution/execution contract hash。

含 exclusive 工具的批次若还有兄弟调用，必须在任何 handler 执行前整批拒绝。工具返回 `ToolInputRequest` 后，只有
声明 resumable 且具有 resume handler 的工具能消费回答继续执行；恢复绑定原 Catalog 和精确 execution contract，
并允许再次请求输入。非 resumable 工具仅支持回答直接成为结果的单阶段交互。

恢复输入必须使用 `AnswerResolution | UnattendedResolution` 判别联合，不能把 resolver provenance 混入工具拥有的
任意 JSON answer。工具只有显式声明 `supports_unattended` 才能接收无人值守 resolution；该能力要求 resumable，
并进入 Manifest、SDK、worker protocol 与 contribution/execution contract。无人值守 handler 必须返回最终结果，
再次请求输入属于协议错误。Host 不为未知 request schema 猜测默认答案。

## 11. Trust、Permission 与 Credentials

### 11.1 Trust 模型

- Builtin plugin 由 OURO composition root 提供并信任；
- User plugin 安装仅保存经校验的不可变 bytes，不 import entrypoint、不启动 worker/MCP、不发布 contribution；
- 初次 `enable` 显式接受并运行当前 selected digest；
- `update` 只并排安装候选，不自动选版；`select`、`reload` 和 `rollback` 显式接受并切换目标 digest；
- 激活前必须重新验证 digest store 完整性、配置、依赖、Manifest 与运行契约；
- `python-inprocess` 是 full-trust，worker 不是 OS sandbox；Host 必须诚实展示 runtime 类型，不能把 permission
  声明描述成系统调用级隔离。

OURO 是单用户本地智能体，不另存 capability authorization 或 code-trust grant。它们与显式生命周期操作属于同一
用户对同一 digest 的重复同意，不能建立额外执行边界。需要停止信任时直接 disable/remove；需要接受新内容时显式
select/reload/rollback。前端只呈现这些真实状态与动作，不维护第二套 trust 状态机。

### 11.2 Permission registry

Permission id 必须由 OURO 核心注册表定义。每个 id 都要有真实消费者：

- `credentials.use`：允许解析声明的 credential slot；
- `process.spawn`：披露并授权插件 MCP/Host service 启动子进程；
- `network.outbound`：披露并授权插件 MCP/Host service 建立远程连接；
- `sessions.write`：允许请求带完整插件 provenance，以及显式 role、scaffold、semantic tag 契约的 Session input；
- `sessions.prompt.inspect`：允许 Prompt Policy 接收用户提示正文和附件的安全 metadata；
- `background.run`：允许请求 continuation/background Run。

未知 permission 严格拒绝。只有真实 Host service 落地后才新增 filesystem、resource、model 等 permission。
对 full-trust runtime，permission 不是 OS 强制边界，界面和文档必须明确这一事实。

### 11.3 Credentials

- Manifest 只声明 slot，不包含秘密或环境变量值；
- activation configuration 把 slot 绑定到本地 credential reference；
- resolver 只解析本插件声明且绑定的 slot；
- 秘密不进入 state、event、diagnostic、exception、worker initialize 或公开 DTO；
- worker 需要环境变量时使用 Host 生成的临时名称和 allowlisted environment。
- worker close 必须拥有 worker、后代进程树、stdin/stdout/stderr 和 Windows tree-terminator 的完整生命周期；
  业务关闭预算不能通过遗弃 OS 清理进程或等待对象来实现。

## 12. Configuration 与 Data

- Configuration schema 安装时静态校验；
- 每个 installed digest 可保存多个 immutable configuration revision；
- configuration 规范化后保存，credential secret 不进入 revision；
- 没有 configuration schema 时使用规范化空 revision；只有 schema required 字段未满足时才缺少有效 revision；
- 缺少 active configuration revision 时不得执行 runtime；
- 配置先校验并创建候选 revision，再构建候选 generation；
- 候选失败时 current selection、active revision 和 catalog 不变；
- 插件只能读取自身 configuration；
- candidate generation 的 Host data API 不直接修改共享 data：写入/删除暂存于候选视图，候选失败时整体丢弃；
- `python-inprocess` 是 full trust，直接绕过 Host data API 的 Python/OS 写入不属于该事务保证，也不伪装成沙箱能力。

被 active generation 或 durable execution contract 引用的 configuration revision 不得删除。未引用候选 revision
可以由 GC 清理。

数据布局：

```text
OURO_HOME/plugins/
  store/<plugin-id>/<digest>/
  data/<plugin-id>/
  staging/
  locks/
  state.json

OURO_HOME/artifacts/
  <plugin-id>/
```

- store 不可变且只保存 user plugin；builtin code 随 OURO distribution 发布，不复制或伪装安装到 store；
- data 同时服务 builtin/user plugin，并按稳定 plugin id 隔离；data 跨 user plugin 版本和 OURO 进程重启保留；
- artifacts 保存用户可见、可由 Host 文件工具处理的插件制品；它不参与 candidate data staging，也不是 canonical
  Session 后端的一部分；Host 的路径策略只开放这一根目录，不开放整个 `OURO_HOME`；
- state.json 只接受当前 schema 4；违反当前契约（不支持的 schema、非法编码、截断或
  损坏）时不得阻断 Host 启动：boot 路径将其隔离
  重命名（不删除）并以空注册表降级启动，记录 diagnostic 与 audit；临时读取、权限、锁和其他 I/O 错误不得被
  误判为内容损坏或触发隔离；
- `LocalApplication` 将插件与 MCP 启动降级投影为 Host-neutral `ApplicationStartupReport`；普通 run、
  JSON 和未来 Host 必须显式呈现或编码该结果，不得静默运行于缺失的系统扩展之上；mutation 路径仍严格拒绝
  非当前状态，并通过类型化 persistence error 返回，不得在无法解析的注册表上增量写入；
- remove 默认保留 data；purge 必须显式；
- 不提供历史数据迁移或回滚兼容协议；格式不兼容时按上述读取边界处理，由用户选择新目录或显式数据维护，不自动清空用户数据；
- 不为尚未出现的迁移需求预建 schema registry、backup engine 或兼容分支。

## 13. Source、Install 与 Dependencies

### 13.1 Source

当前支持 local directory、Git URL/ref/subdirectory 和本地 archive。Source provider 只 materialize，不激活。

- Git 操作有 timeout、clone/download 边界；
- ref 解析为 commit 并进入 provenance；
- HTTP 明文来源默认拒绝或显著确认；
- source credential 和敏感 URL 必须脱敏。

### 13.2 Install

1. 在临时目录 materialize；
2. 校验 package tree、Manifest、配置 schema、MCP descriptor 和引擎范围；
3. 计算 tree digest；
4. 生成 provenance；
5. copy 到同文件系统 staging 并原子 publish 到 digest store；
6. 最后原子写 install record；
7. record 失败留下的无引用 store 由 GC 清理。

安装不 import、不执行 package script、不 enable。

### 13.3 Dependencies

Plugin dependency 只有 `plugin_id + SemVer range`。

- resolver 使用 composition root 提供的 builtin contract 与已安装且 selected 的 user digest，不隐式下载、安装、
  选版或改变 dependency desired state；
- builtin contract 必须由同一 builtin definition registry 派生，不能由 composition root 维护一份可能漂移的手写
  contract 表；
- cycle、缺失和 version conflict 使依赖者 blocked/incompatible；
- dependencies 按拓扑先激活，未 active 时不得启动依赖者；
- shutdown 按逆拓扑；
- active dependent 存在时不能 disable/remove dependency；
- dependency digest、configuration 或 contract 变化时，受影响 active dependant 必须进入同一 activation plan；
- Python/Node library dependency 不是 plugin dependency；
- 不存在 `optional_feature`，直到 OURO 有真实 feature negotiation 模型。

Builtin dependency graph 必须闭合于 builtin definitions；user plugin 可以依赖 builtin，builtin 不依赖 user
install。启用 dependant 时启用 dependency closure；存在 active dependant 时不能关闭 builtin dependency。

## 14. Activation 与 Reconciliation

Reconciler 是 desired state 与 observed state 的唯一协调者：

```text
read persistent activation or mutation candidate
  -> validate target package/configuration revision and explicit lifecycle intent
  -> resolve dependency graph
  -> ensure dependencies active
  -> compute affected active dependant closure
  -> build candidate generation set in topological order
  -> prepare/start candidate resources; keep autonomous services stopped
  -> build candidate catalog
  -> compare-and-commit persistent state; open generation gates and atomically swap current catalog
  -> stop old autonomous services, then start new services in topological order
  -> commit observed active
  -> retire previous generation set in reverse topological order
```

候选失败：

- current catalog 和 previous generation 保持不变；
- 清理候选 contribution、task、process、MCP 和 listener；
- 保存有界、脱敏 diagnostic；
- 不影响无关插件和 builtin。

当前单进程 Host 使用一个 runtime mutation lock 串行化候选提交，不建立容易死锁的 per-plugin 多锁图；插件代码、
I/O 和 start/stop 均不进入 catalog pointer lock。Background Service 的声明与 owner 在候选期完成准备，但实际
`start` 延迟到发布后，并严格等待旧代 `stop` 完成，因此同一 plugin id 不会同时运行两份声明式自主服务。
Generation lease 可以继续服务已绑定的 Run；activation-singleton Background Service 只属于 current generation。

Disable：

1. 持久化 desired disabled；
2. 关闭 generation gate，阻止新调用；
3. 发布不含该 generation 的新 catalog；
4. 等待旧 catalog lease 和 in-flight call 释放；
5. stop instance、background service、MCP、task 和 worker；
6. 释放 owner。

## 15. Durable Run

Run 获取一个 `ExtensionCatalogLease`，并持久化所使用 builtin/user plugin generation 的 execution contract：

- plugin id；
- release reference：user 使用 package digest，builtin 使用 builtin contract hash；不得给 builtin 伪造 package digest；
- configuration revision id/fingerprint（覆盖规范化配置和 credential binding reference，不包含秘密值）；
- dependency lock，包含 builtin/user dependency contract；
- Plugin API/worker protocol；
- contribution contract hashes。

`execution_contract_hash` 对以上事实计算稳定摘要。`generation_id` 只用于当前进程 owner、日志和诊断。

Prompt Policy 在 Run 尚未创建前执行，不产生可恢复 pending state；allow 后必须把评估时取得的同一 Catalog lease 转交
Run，不能在策略检查后重新读取 current catalog。Observer registration 属于执行契约，但观察投递不成为 durable Run
恢复条件；未投递或未完成的 observation 不阻断恢复。

恢复时：

- 相同 package、configuration revision、依赖和 contract 可以创建新 generation 并恢复；
- package 缺失、contract 改变或依赖不满足时稳定失败；
- terminal Run 的 pending continuation settlement 在目标 Session 激活时重放，只提交 durable
  release/queue/checkpoint，不因 Host 启动直接扫描历史或执行新 Run；
- 不能仅按 tool name、plugin version 或随机 generation id 猜测等价性；
- pending approval/tool batch 必须重新通过当前 generation gate 和 ToolExecutor。

Builtin 不保留随旧 OURO 版本发布的代码副本。恢复所需 builtin contract 与当前 definition 不一致时必须确定性失败，
不能按同名 Tool、相同 plugin id 或当前 factory 猜测兼容。哈希输入格式也是契约身份的一部分；绑定不同格式所得
哈希的待恢复 Run 同样不支持续跑，不自动重绑、不重放待审批动作、不清空会话事实。继续原 Run 需要匹配的
运行实现；需要当前实现执行新的工作时使用新 Session。

## 16. Update、Rollback 与 Remove

### 16.1 Update

- side-by-side 安装候选 digest；
- 不隐式改变 selected digest；
- 用户完成候选配置后显式 select；
- select 通过包含受影响 active dependant closure 的候选 activation 流程；
- 只有新 catalog 发布成功才提交 active selection；
- 失败时 current selection/catalog 不变。

Active plugin 的配置更新使用相同流程：创建 immutable candidate revision，成功后提交 active revision reference，
失败时保留旧 reference。

### 16.2 Rollback

- 选择仍存在的 immutable digest；
- 使用同一配置、依赖和候选 activation 流程；
- 失败不改变 current catalog；
- 不承诺兼容任意旧 OURO state 或插件私有 data schema。

### 16.3 Remove

- selected/active digest 先 disable；
- 有 active dependent 或 durable contract reference 时拒绝；
- 删除 install reference 后由 GC 删除无引用 store；
- 默认保留 plugin data；
- 删除最后一个 digest 后才允许显式 purge data。

## 17. Community interoperability

社区兼容是导入与形态互操作，不是 OURO 历史兼容。

- Agent Skills 复用标准 `SKILL.md`；
- MCP descriptor 显式转换到 OURO MCP config；
- Claude/Codex 包可以检查 Skills、MCP 和不可移植资源并输出 CompatibilityReport；
- Pi/OpenCode 可提取 Skills/MCP，或把 executable capability 移植到 OURO SDK/worker；
- 不透明执行未知 Hook、LSP、theme、app 或 JS runtime；
- compatibility command 对每项输出 `supported`、`converted`、`ignored` 或 `incompatible`，并在可迁移时给出明确
  `ouro_target`；命令只能报告或显式生成目标文件，不能把“发现”宣称为完整兼容。

当前不建设 Marketplace、签名和自动依赖安装。

## 18. Application 与 Host

`PluginService` 提供类型化用例：

- scaffold、validate、install、list、show、diagnostics、dependencies；
- configure；
- enable、disable、restart、check；
- update、reload、select、rollback、remove、gc、purge-data；
- list/execute plugin command；
- compatibility report。

查询、enable、disable、restart、check、diagnostics 和 dependencies 同时适用于 builtin/user。install、update、reload、
select、rollback 和 remove 只适用于 user plugin；对 builtin 调用时 application 层返回
类型化的 operation-not-allowed 错误，不能静默 no-op。builtin disabled 后允许显式 purge 私有 data，但 GC 永不处理
builtin code。

CLI/TUI/Desktop 只映射这些用例。Plugin command canonical name 为 `<plugin_id>:<command>`；builtin command
优先，只有 local name 唯一时 Host 才能提供短别名。

TUI 和程序化 Host 通过 `application.plugin_commands.submit_plugin_command` 选择命令、按需创建 Session 并调用
`PluginService.execute_command`。未知名称返回空结果，重名返回明确的歧义错误；无目标 Session 的纯查询不创建
空会话。`requires_session` 在 handler 前满足，其他命令仅在输出输入时创建目标；创建使用 Host 传入的模型、
推理、审批、交互与工作区设置；标题优先取输入标题，否则使用命令参数或名称。

创建 Session 是一个由 `run_blocking` 承担的完整同步操作；取消须等待已取得所有权的创建结束，并通过
`on_session_created` 报告已完成创建的身份，再传播取消。该通知在事件循环上执行，Host 可立即更新受控会话，
之后的命令异常不使已经创建的会话失去身份。命令准备在一次锁内捕获 Session/Harness 和活动 Run，离开事件循环
执行；Catalog 按该 Run 的原有代次与执行契约选择，命令复用已经捕获的上下文，不再次读取或跨命令缓存。
Catalog 获取期间发生的历史撤销不能通过重取较新 Harness 而被隐藏，既有提交冲突检查保护同一接受边界。

`PluginCommandSubmission` 仅携带目标 Session 和现有执行结果，不是新的持久化回执。`consume_plugin_command`
统一处理已接受回执、独立输入和不可分批次，通过原有 Runtime 入口消费；已接受回执不再次提交输入，待接受输入
仍由对应 Runtime 入口决定接受时点、FIFO 和活动 Run 的处理。输出、接受与完成不混为一个状态：命令 handler
执行完成不代表所有输入已接受或执行；异常与取消不自动重试 handler，不将不确定结果报告为“未执行”。

消费用例逐个交付 Run 结果；TUI 按呈现需要收集并通过 Textual 工作流等待。
应用层不创建 Host 任务或依赖 CLI/TUI/Textual，不要求 Runtime 等待界面刷新。底层 `PluginService.execute_command`
及非交互式 `plugin run-command` 的显式目标与输出契约保持独立。

`scaffold` 只生成一个经完整 package validation 的最小 user plugin，并以临时目录 + rename 原子发布，绝不覆盖已有
目录。`reload` 是显式本地开发用例：先按 immutable digest 并排安装，再检查候选配置、依赖和包完整性；只有候选
就绪才通过既有 candidate activation 原子选版，失败时旧 selection/Catalog 不变。

Git source 必须在 checkout 前读取 tree metadata 并执行 portable path、文件数、单文件与总大小限制；使用 shallow、
blob-filtered fetch，指定 subdirectory 时使用 sparse checkout，并禁用 LFS 自动 smudge。无论来源为何，最终 package
仍经过同一静态验证边界。

## 19. Diagnostics、Concurrency 与 Resource Ownership

Diagnostic 至少包含 code、phase、severity、plugin id、digest、generation、contribution、retryable、建议动作和
correlation id，并经过敏感值脱敏。每个 generation 的 diagnostic 必须有界。

并发规则：

- install/update/remove/state mutation 使用跨进程文件锁；
- 同一 plugin id 的 activation mutation 串行；
- 多插件 activation plan 按稳定 plugin id 顺序获取受影响 owner lock；
- catalog swap 只保护不可变 pointer 交换；
- 插件 import、start、stop 和 I/O 不在 catalog lock 内；
- callback 有类型相关 timeout、cancel 和 output limit；
- 每个 Observer registration 使用独立有界 mailbox；业务路径只做非阻塞 enqueue，关闭时有 deadline 且 lease
  精确释放；
- owner close、gate close、lease close 和 unregister 幂等；
- Host shutdown 先停止 Run consumer，再按依赖逆序关闭插件 owner。

## 20. 模块边界

目标职责：

```text
ouro/plugins/
  builtin.py         # immutable builtin definition registry
  candidates.py      # unified builtin/user runtime candidate
  manifest.py        # package schema
  models.py          # install/activation/generation/catalog records
  sources.py         # source adapters
  scaffold.py        # minimal validated user-plugin scaffold
  package.py         # static package validation
  store.py           # immutable code and mutable state/data
  readiness.py       # configuration and credential-binding readiness
  contribution_types.py # internal ContributionTypeAdapter definitions
  catalog.py         # immutable ExtensionCatalog and leases
  dependencies.py    # dependency graph and reconciliation inputs
  runtime.py         # generation lifecycle and reconciliation
  worker.py          # worker protocol/process adapter
  compatibility.py   # community inspection/import

ouro/sdk/plugins/v2.py
  # public plugin API only

ouro/application/plugins.py
  # Host-neutral use cases and DTO orchestration

ouro/builtin_plugins/
  # product-owned plugin implementations assembled only by the application composition root
```

不要求为目录结构预建空文件。只有在对应职责开始实现时拆分；拆分目的是明确变化轴，不是追求文件数量。

## 21. 验收矩阵

### Package 与 activation

- path traversal、symlink/reparse、archive bomb、文件/总大小和 Git source 边界；
- 安装阶段 entrypoint side effect 从未执行；
- builtin/user class 不能由 Manifest 伪造；
- 安装从不执行插件；enable/select/reload/rollback 只运行显式目标 digest；
- secret 不进入公开输出、协议和 diagnostic。

### Catalog 与 lifecycle

- 候选 start/commit 失败时 current catalog 不变；
- 新旧 Run 同时持有不同 catalog，不互相阻塞；
- enable/disable/restart 不等待无关 Run；
- gate close 后不能开始新插件调用；
- retire 和 shutdown 无 task/process/MCP/listener 泄漏；
- builtin 和其他 user plugin 不被失败候选影响。

### Dependencies 与 status

- cycle、missing、range conflict；
- dependency activation failure 阻止 dependant；
- dependency 恢复后 reconciler 可重新激活 dependant；
- command result、list/show/check 使用一致 observed status。

### Runtime

- in-process namespace conflict 明确失败；
- worker timeout 和 task cancellation 都发送 cancel；
- worker exit 撤销调用入口并投影 failed；
- background task 失败投影 degraded；
- 新旧 Catalog 并存时每个 plugin id 最多运行一份声明式 Background Service；
- candidate start 的 Host data 写入被隔离暂存，失败候选不修改共享 plugin data；
- optional MCP 失败 degraded，required MCP 失败不发布候选。

### Skills、MCP 与深层扩展

- Plugin Skill 使用统一 Skill Catalog，disable 后只影响新 catalog；
- Plugin MCP 随 generation 启停；
- contribution adapter 统一执行 declaration、contract、projection 和 cleanup；
- Context Provider、Turn Policy、Continuation、Tool Policy、Command attributed input 继续满足各自来源、
  model role 和组合规则。

### Observer 与 Prompt Policy

- Observer 不被 Agent 语义关键路径 await，慢、失败或积压的 registration 不影响其他 observer 或业务结果；
- 同 registration FIFO，queue drop、timeout、cancel、retire 和 shutdown 均不泄漏 task 或 Catalog lease；
- Prompt Policy 的 allow/deny、失败关闭、permission 和 same-catalog 语义可由 application 集成测试证明；
- deny 不创建 canonical user message、Run、continuation 或可恢复残留，且 diagnostic 不泄露提示正文；
- in-process 与 worker 的 observation/policy 类型、错误、取消和 contract 行为等价。

Plugin Skill 不转换为 Observer；`RuntimeService` 每轮从该 Run 的
`ExtensionCatalogLease.skills` 生成 Skill 提示词。Plugin MCP 只为 connected server 生成 contract，required 失败
中止候选，optional 失败产生 degraded；发现工具结构冻结在 Catalog，连接由 generation owner 在 retire 时关闭。

### Durable recovery

- 相同 execution contract 可跨进程重建；
- 配置、依赖或 contract 改变时稳定拒绝；
- pending tool/approval 不调用同名不同 contract contribution。

### 开发体验与社区资源

- scaffold 成功后 package 可直接 validate，失败不留下公开半成品，且不覆盖用户目录；
- active 插件 reload 未满足授权/信任时只保留候选，旧 Catalog 继续服务；候选就绪后原子切换；
- Git source 在 blob checkout 前拒绝超限或不可移植 tree，subdirectory 不物化范围外文件；
- declarative、in-process、worker 三类示例均通过真实 install/enable 生命周期；
- compatibility report 的四种 outcome、目标描述与不执行未知代码边界可验证；
- 开发指南明确 builtin/user、full-trust、request context、Catalog、owner 和 durable contract。

### 架构与质量

- `core` 不依赖 plugin package/runtime；
- Host 不绕过 `PluginService` 与 `LocalApplication` 组合边界；
- 没有 workspace plugin discovery/activation 路径；
- 没有旧 schema/protocol 兼容分支；
- pytest、ruff、format、mypy、compileall、dependency tests 和 `git diff --check` 通过。

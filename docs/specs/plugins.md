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
- **决策一致性**：一轮模型请求及其调用共享不可变 Extension Catalog，后续决策使用当前目录；
- **原子替换**：候选插件完整启动后才影响新的决策和操作，失败不改变当前可用版本；
- **可恢复**：会话依据持久事实和当前能力继续；待完成操作按精确执行契约处理，不按同名 contribution 猜测；
- **本地可控**：安装、配置、启停、选版、数据、诊断和删除都由本地 `PluginService` 管理；
- **必要信任边界**：明确 full-trust 与 process isolation，不宣称不存在的 OS sandbox；
- **可演进内核**：原生 contribution 保留领域语义；插件通过具名服务和受管理 handler 提供自己的业务扩展接口。

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
9. 每轮模型决策获取不可变 catalog；发布更新不替换已经发出的请求及其调用绑定，等待不持有环境租约。
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
23. Prompt Policy 在接收输入和消费排队输入时检查当前策略；拒绝接收不得留下消息或 Run，拒绝消费保留已接受输入。

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
  immutable contribution directory leased by decisions and active callbacks
```

### 5.1 Plugin identity

- `plugin_id` 使用反向域名风格，安装后不能由更新改变；
- `version` 使用 SemVer，描述作者发布版本；
- `package_digest` 是排除缓存后的规范化包文件树的 SHA-256；
- `generation_id` 标识当前进程内资源 owner；
- `execution_contract_hash` 标识可跨进程校验的运行契约；
- `configuration_revision_id` 是规范化配置与完整凭据绑定的内容摘要；环境引用按引用计算，直接密钥按真实值计算；
- 相同 version、不同 digest 是不同内容，不能静默覆盖。

配置修订属于插件记录，包选版只引用其中一个修订。修改配置或移除某个包版本不删除既有修订，等待中的 Run 可以
继续引用原配置。最终安装记录删除时一并删除配置修订；插件私有数据按显式保留／清除选项处理。

Run Driver 的持久绑定包含所属插件与传递依赖的 release、配置修订和执行契约。恢复按需租用或装载该闭包，使用
原依赖服务实例，不修改当前目录；临时装载的实现不发布全局贡献、不启动后台服务，最后一个调用释放后按 generation
关闭协议回收。禁用同时撤销依赖该插件实际 generation 的旧调用，即使当前选版已经移除了相应依赖。

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

User package digest 覆盖排除缓存后的完整规范化包文件树，用于安装内容身份与完整性检查。
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
    "ouro": ">=0.3.0,<0.4.0",
    "plugin_api": "^5.0.0"
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
- 静态 contribution identity、动态工具组 identity 与执行策略必须在执行前静态可见；动态工具定义在发布边界校验；
- `optional_feature`、MCP `auto_start` 和无消费者 placeholder 字段不存在；
- schema 变化直接更新 schema version 和所有调用方，不保留开发期旧 reader。

### 6.3 Package 路径

路径字段必须：

- 以 `./` 开始并使用 `/`；
- 不包含空段、`.`、`..`、drive、UNC 或 alternate stream；
- 解析后位于 package root；
- 指向安装时存在的普通文件或目录。

包拒绝 symlink、junction、reparse point、device、socket、FIFO 和 VCS 目录。materialization 必须有文件数、
单文件、总大小、archive compression 和 Git 时间/下载边界。

`__pycache__`、`.pytest_cache`、`.mypy_cache`、`.ruff_cache`、`.cache` 目录及 `.pyc`、`.pyo` 文件属于缓存，
不进入安装载荷、包文件树校验或 package digest。遍历时直接剪枝缓存目录；运行过程中生成或更新缓存不会改变包身份，
也不要求卸载插件时清理缓存。Manifest 的资源路径不得指向这些排除项。源代码、Manifest 和其他发布资源继续参与校验。

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
- `prepare/start` 期间私有 data 只读，候选 `start` 只建立可并存且由 generation 拥有的资源；写入由发布后的回调或 Background Service 执行；
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

双方 reader 只解析和分派，不等待业务回调。请求携带 `parent_id` 时，接收方绑定到仍在执行的父调用；嵌套
Host 调用继承其只读和 workspace 约束。请求有独立关联 ID、有限正数 `timeout_seconds` 和取消通知；父调用
结束后不接受其迟到的业务结果。默认每侧同时处理 32 个独立请求，每个方向的在途请求上限为 128；同一调用链的
重入不占用新的独立请求槽位，但仍受总上限约束。业务完成时释放独立槽位，响应写入任务仍由传输层持有。

反向方法包括 `services.call`、`services.register`、`registrations.close`、`handlers.invoke`、
`processes.run`、`environment.agent_instructions` 及 Run 的模型、工具和 `run.inputs.consume` 领域操作。
`run.inputs.consume` 只允许持有有效父 Driver 调用上下文的请求，输入交付与进程内 Driver 遵循同一原生契约。
Host 仅调用明确的 worker 方法，handler 通过所属 worker
内部的 ID 寻址；进程输出通过 `processes.output` 回调传递。没有任意对象反射或远程属性访问。

后台服务使用 `background.start` 和 `background.stop`，参数为 `contribution_id`，成功响应为空对象。
这两个方法负责对应服务的实际启动与资源释放；`lifecycle.start/stop` 负责插件实例本身，不能代替服务启停。

Worker 返回的 contribution 必须是 Manifest 静态声明集合的子集；缺少必需项或返回未声明项使 prepare 失败。

调用响应由对应方法的解码器同时校验并构造最终 SDK 输出，handler 直接返回该对象。
片段、Session 输入、批次与控制转换在同一次响应中只解码一次；Tool call/resume 共享结果解码契约。
外部类型、默认值、互斥输出和语义约束仍在 worker 边界检查，SDK 到 core 的转换保留各层职责。

## 8. Plugin API

插件只依赖 `ouro.sdk.plugins.v5`，不依赖 `ouro.core.*`、`ouro.application.*`、具体 registry
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
- plugin-private data view；候选只能读取共享私有数据，发布后允许写入和删除，资源清理完成后撤销写权限；
- credential slot resolver；
- supervised tasks；
- redacted logger 和 diagnostics；
- `services`：访问本插件或显式依赖插件的具名服务；
- `processes`：有权限、可取消的 argv 执行及有序输出；
- `environment`：读取部署和 workspace 对应的 Agent 指令文档；不暴露整个 Host home；
- `tool_groups`：发布 Manifest 已声明的动态工具组；worker 使用同语义的 `WorkerToolGroups` 端口；
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

命令也可返回 `CommandOutput.run_action = stop | continue`，要求目标 Session、有效执行契约及
`sessions.write`、`background.run` 权限；不能与新输入混用。`stop` 将当前执行的取消意图与命令状态共同提交，
Host 提交用例经 Runtime 结算已接受的取消意图，不重复发起取消；迟到的提交返回不能停止已经恢复的执行。
`continue` 保留没有取消请求的活动执行；存在取消请求时先完成收尾。会话空闲时先按 FIFO 消费已接受的排队输入；
没有排队输入时恢复现有执行检查点，最近执行已完成时基于保留历史开始新的执行。
续跑先恢复中断状态、结算待处理的终态策略，再选择排队输入或保留历史；恢复检查点和新建执行都等待前一执行释放锁。
`continue` 与命令状态共同保存一次性续跑凭据，绑定历史代次和控制修订。新命令的 `stop`、控制关系变化或历史撤销
使未消费凭据失效；原生对话执行启动时原子消费凭据，重复或迟到消费不再启动工作。
已有活动执行或无可执行工作时结算凭据，不创建空输入。凭据不参与 Harness 撤销或分支复制，Host 启动不自行消费它。
运行控制不生成消息，也不禁止之后的普通输入或手动续跑。框架负责执行所有权、取消和工具结果恢复，
插件只声明意图，不直接操作 Run。Worker 通过 `command_run_actions` feature 宣告支持此契约。

`SessionInput.driver_id` 可为 follow-up 选择 `plugin_id/run_drivers/id` 或 `ouro.default`；省略时使用默认算法。
接受输入时保存完整 Driver 实现及依赖契约，队列消费不重新选版。batch 的所有成员必须使用相同选择，批量消费不
合并不同的契约。`steer` 不能指定 Driver。命令在调用嵌套 Session Controller 前解析全部 Driver 选择；解析失败
不提交命令状态、控制变更或输入。排队输入通过原生 Run 所有权、预算、审批和 checkpoint 执行。

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

### 插件服务导出

Manifest 顶层 `exports` 声明具名服务，每项包含 `id` 和 `methods`。方法声明输入 JSON Schema、
`effect = read_only | state_changing | unknown` 及是否为 `registration`。可执行插件在 `ContributionSet.services`
提供 `ServiceContribution`；服务和方法必须与 Manifest 完全一致。输入按声明校验，返回值必须是有限数值的
JSON 数据。服务定位使用字典索引，不复制整份 Catalog。

单次服务方法可显式声明 `schedulable=true`，并由具有 `background.run` 权限的提供者执行原生 Task。
注册方法不能作为调度目标。领取时固定方法参数、发布、配置修订及传递依赖的精确实现，调用结束后释放租约。
`ServiceCall.scheduled` 提供真实的任务、执行和发生点身份，worker 在 `services.call` 的 `scheduled` 对象中接收
相同字段。结果是 Task 执行记录中的 JSON；插件调用不借用或制造 Session／Run。禁用撤销在途调用，缺失或不可用
目标在领取前暂停计划，进程退出后的未知结果不自动重放。详见[自动任务](automated-tasks.md)。

`PluginContext.scheduler` 与 `WorkerScheduler(peer)` 提供任务创建、分页、查询、修改、启停、删除、执行历史和
中断。原生 `TaskServicePort` 连接应用用例，适配器绑定当前插件为不可改写的 owner；插件只能访问自己的任务。
写计划需要 `background.run`，拥有 Agent 目标还需要 `sessions.write`，服务目标限本插件或直接声明的依赖。
只读回调不能写计划，未发布或已撤销代次不能调用端口。修改与控制使用预期修订；中断提交持久请求，调度 owner
负责结算。启用插件不自动启用暂停计划，SDK 不自动重试结果未知的创建。持续服务的 start 可以在发布后使用端口，
候选准备和候选 lifecycle start 不可以。

调用使用 `context.services.call(plugin_id, service, method, arguments)`。目标只能是本插件或 Manifest
直接声明的依赖，版本兼容性由包依赖表达。绑定固定到消费代次对应的提供者代次；升级重建受影响的消费代次。
每次普通服务调用及注册 handler 的实际执行均持有 owner 与其传递依赖的精确代次租约；共享依赖在同一绑定内只持有一次。
返回、异常或取消结算后释放租约，不保活无关插件。空闲注册不持有调用租约，显式停用仍撤销入口并取消在途回调。
服务不获取 Session writer，也不自动创建持久任务或进行重试；外部写入结果不明时由业务协议负责恢复。

注册方法通过 `context.services.register(..., handler, arguments)` 调用。提供者先用 `call.on_close(cleanup)`
登记移除动作，再发布 `call.handler`。框架返回幂等关闭句柄；注册同时属于调用者和提供者，任一方停止都会
先撤销 handler，再执行清理。失败的清理保留 owner，不能把未完成的注销报告为成功。普通服务不能返回裸对象、
回调或生成器。只读调用不能借嵌套服务修改状态、注册 handler 或启动进程。

服务可使用 `PluginContext.models.request`、`.tools` 和 `.agents` 调用当前 Run 的原生能力。它们不持有固定 Session 或 Run，
只在调用链包含活跃原生 Run 作用域时可用。方法需使用非只读 effect 并声明 `sessions.write`；模型调用产生的
canonical 历史、预算、计数和用量仍属于调用者的 Run。跨 worker 调用保留相同作用域，Host 不接受服务传入的 Run ID。
`WorkerRunPorts(peer)` 与进程内端口等价；服务调用不使用普通 RPC 默认期限截断原生工作，取消跟随调用者和代次。

### 独立模型请求

`PluginContext.models.generate(ModelRequest, on_output=...)` 执行一次独立文本对话请求，要求 `models.request`。
输入包含 system/user/assistant 消息、临时系统指令、模型路由、思考配置、输出上限和请求期限。输出上限缺省为 None，表示不限制；有限值依据当前请求窗口校验，不与端点的输出配置比较。
`default` 解析当前配置的默认端点；缺少默认配置明确失败。Application 复用模型目录与 Core 请求执行器，
插件适配层仅校验权限、调用作用域并转换 SDK 数据，不持有供应商客户端或存储实现。

输出回调按序号串行 await，支持 text/reasoning 块；慢消费者保留原生流的有界背压。超时、调用取消、代次撤销
或输出消费者失败会关闭供应商流。只有供应商报告的用量进入原生账本，记录插件和请求 ID；Session 与工作空间
按实际调用来源关联，不创建 Run。已接收完成事件的用量在返回结果前结算，结算期间取消也等待写入结束。

准备阶段、只读回调、已结束调用或已撤销代次不能发起请求。活跃 Run 中必须使用 `models.request`，保留原生
预算与历史提交；独立请求不能绕过这些约束。worker 使用 `WorkerModels(peer)`，注册其 `output` 到
`models.output`；双向调用继承父请求生命周期，SDK 请求期限不受普通 RPC 默认超时截断。

### 摘要策略

`CompactionCall` 提供 session/run 身份、可选只读状态视图、已有摘要与待压缩材料，不传入主请求预算。`CompactionOutput.summary` 是规范化的非空摘要正文；插件自行定义提示词、原始格式与解析过程，正文无需使用内置策略的 XML 标签。

Host 用实际正文重新投影主上下文，通过容量和状态校验后提交。策略可报告用量，失败或取消不激活部分摘要。具体边界见[上下文与压缩](context-and-compaction.md#摘要执行)。

### 受管理进程与环境

`context.processes.run(ProcessRequest(...), on_output=...)` 要求 `process.spawn`。执行使用 argv，不自动加入
shell；cwd 遵守当前调用的 workspace 约束。调用取消、代次撤销和输出消费者异常均进入原生进程树清理。
输出回调按序号串行接收 UTF-8 文本块，慢消费者使读取背压；完成结果保留最多 100,000 字节的共享输出前缀并
标注截断。需要完整大输出的插件通过输出回调写入自己的制品。进程和 worker 都不构成 OS 沙箱。

工具子进程与 worker 共用本地进程 owner。Windows 进程以 suspended 状态创建，加入配置为
`KILL_ON_JOB_CLOSE` 的 Job Object 后才恢复主线程；父进程提前退出不会丢失后代归属。POSIX 使用独立进程组。
这是进程生命周期管理，具体 Windows 约束见 [AssignProcessToJobObject](https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-assignprocesstojobobject)。

`context.environment.agent_instructions(workspace)` 返回带来源路径、user/project 作用域和内容的只读文档。
文档读取在调用时完成，保留大小和编码检查；Agent Context 使用同一公开端口生成自己的上下文贡献。

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
- `run_drivers`；
- `providers`；
- `tool_policies`；
- `session_controllers`。

Host capability 清单由以下 15 类组成。这里的“消费者”指真正解释该类型语义的 Host 边界，Catalog projection
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
| `providers` | request | faux-provider、原生 Faux 事件桥接、in-process/worker 路由与卸载测试 | 配置模型目录、默认与自定义 Driver、独立生成及连接检查 | stable |
| `run_drivers` | invocation | review-driver 与 delegating-driver 的 in-process/worker、等待重启与契约测试 | Runtime Driver 选择、原生模型／工具／Agent 端口与 checkpoint 消费 | stable |
| `prompt_policies` | snapshot | inprocess-prompt-policy example | Runtime prompt admission boundary | stable |
| `turn_policies` | snapshot | SDK/worker semantic contract、Runtime turn tests | Runtime terminal-candidate boundary | stable |
| `tool_policies` | snapshot | Plan builtin、SDK/worker semantic contract | `ToolCallCoordinator` preflight 与 execution recheck | stable |
| `session_controllers` | snapshot | Goal/Plan builtin、worker semantic contract | `SessionControlCoordinator` 及 Run resume/settlement | stable |

每种 capability 必须同时具备 Manifest/SDK 表达、Host adapter 和真实语义消费者。系统保持 15 类 capability；
新类型必须由真实 builtin 或最小可运行 user plugin 驱动。失去消费者的类型应同步从 Manifest、SDK、worker、
prepare、contract、projection 与文档删除，不保留空 adapter 或迁移别名。

观察型扩展只公开 `observers`；`prompt_policies` 是独立语义边界。Manifest schema 3 不接受 `hooks` 字段；
Plugin API 使用 5.0.0，worker protocol 使用 3.0；Python SDK 入口为 `ouro.sdk.plugins.v5`。

公开交互 DTO 位于 `ouro.sdk.plugins.interactions`。计划确认请求包含预览文本、计划引用、可选方案和明确的动作，
答案包含动作及用户选择或反馈。Host 按 DTO 类型呈现，不以 builtin 身份或工具显示名赋予特殊界面；请求来源仍
保存在原生调用事实中。业务校验与 controller 变更由请求所属插件通过公开 Session 状态与控制端口完成。
Goal 和 Plan 的业务模块仅依赖 SDK；builtin definition 只负责在发行版中装配 Manifest 和 factory。

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

- `snapshot-bound`：投影到不可变 Catalog，当前决策或回调的租约允许旧、新 generation 并存；
- `activation-singleton`：不进入 Run 可调用目录，在声明的 host/home 作用域内，同一插件的同一服务最多运行一代；旧服务停止失败时，不启动替代服务。

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
5. 模型决策保留完整 Catalog lease，相关响应及调用收尾后释放；局部消费者仅保留所用贡献及其 owner 依赖。

管理查询通过 `catalog()` 读取不可变目录，不取得执行租约。命令绑定包含目标命令、所属插件的控制器、
目标 Session 已激活的控制器及各 owner 的传递依赖；Observer mailbox 只保留实际匹配的 registration 及其
owner 的传递依赖。这些局部绑定不保留父 Catalog，关闭时释放各自的租约。

基础 user/workspace Skill 与 Plugin Skill 是同一 Catalog 中的 `SkillMetadata`。Skill 名称全局唯一；与 Host baseline
或其他 active user generation 冲突时，候选 Catalog 整体失败，不存在加载顺序覆盖。每个插件 Skill 声明必须恰好
解析一个 `SKILL.md`，且 frontmatter `name` 等于 contribution id。完整 Skill 正文仍按需要从 Catalog 固定的
immutable package path 读取，不能重新发现 current plugin state。Catalog 对外返回防御性副本，消费者不能修改
共享 Skill metadata。

### 9.5 动态工具组

可执行插件通过 Manifest 顶层 `tool_groups` 声明组 ID 和固定执行策略。组内每个工具的权限、effect、批次行为、
恢复能力及 Controller 绑定必须与组声明一致；工具 ID、暴露名称、描述和参数／结果 JSON Schema 在发布时校验。
未声明组的插件不持有发布端口。工具名称在整个 Catalog 中唯一，静态工具和其他组的工具不能被隐式覆盖。

`ContributionSet.tool_groups` 提交候选初始组；active generation 通过 `PluginContext.tool_groups.publish()`
发布 `ToolGroupContribution(id, revision, tools)`。发布构造并校验完整候选 Catalog 后一次替换目录。
失败保留原组，空组撤去该组全部工具。只读、已结束调用及非当前 generation 不能发布；生命周期变更占用期间立即
拒绝发布，避免正在停止的回调等待自身退出。发布不增加持久化状态或后台刷新循环。

组内工具规范身份为 `plugin_id/tools/group_id/tool_id`，契约散列包含稳定实现 revision 与工具定义。
revision 由插件标识行为语义，行为变化必须更换，即使 schema 不变。组修订不改变 generation 的执行契约；
Driver、依赖及 Session 控制绑定继续使用所属 generation 的稳定身份。

模型决策租约同时保留工具组修订。替换或撤销只影响后续决策，已发出的请求和工具调用保留原 schema、处理器和
执行契约；旧修订排空租约后关闭调用入口。Catalog 重建 Host baseline 时保留所绑定的组修订。命令、Driver 与
Observer 的局部绑定不持有无关组租约。禁用插件撤销所有修订的 generation gate。

`publish()` 返回幂等 `Registration`。关闭只匹配该次发布，不能撤销替代它的新组，Host 随 generation 清理注册。
等待释放执行租约；重启恢复须匹配原工具契约，缺失或不同 revision 产生明确契约错误，不替换执行。
工具组不持久化 Python 回调，插件负责在发现阶段提供可重复的实现 revision。

worker 的 `contributions.prepare.tool_groups` 为按组 ID 索引的 descriptor，包含 `id`、`revision`、
`registration_id` 和 `tools`。`tool_groups.publish` 提交同形 descriptor；Host 对包声明、执行策略及整个
Catalog 作同一校验。随机 `registration_id` 只标识本次进程内发布，不进入持久工具契约。
`tool_groups.call`／`tool_groups.resume` 在普通工具调用与 Session 状态协议之外携带该发布身份。

`tool_groups.close` 按发布身份撤销，返回 Host 是否仍持有它。旧决策排空后，Host 在同一有序通道发送
`tool_groups.release` 通知；worker 同步移除对应处理器引用，不占用业务请求并发槽。发布回执取消时仍须按身份
结算，不能提前删除已接受调用的实现。禁用或 worker 退出后，Host 清理通知任务和注册记录，关闭的 worker
不会因旧 Catalog 后续释放而重新启动。

Python SDK helper 将单参数工具 handler 的文本、结构化内容、等待请求和恢复结果映射到协议，提供本次调用的
Session 状态视图。状态返回与 Host 接受保持分离；只读与失效视图拒绝变更，同步 handler 取消后等待线程退出，
丢弃迟到结果。工具与恢复调用不使用传输层默认超时，其实际生命周期由 Run、调用方取消及 generation gate 控制。

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
可选 semantic tag（SDK 默认值是显式契约值 `role=user, scaffold=true, semantic_tag=None`）。`message` 表示该
片段必须在模型请求前由 application 以提供方给出的消息属性和 provider/generation provenance 持久化为 canonical
message。Run/model round/provider/fragment index 标识请求边界内的片段位置；重试时，正文、role、scaffold 和
semantic tag 与该位置最近一次持久消息一致才跳过。内容或消息属性变化时追加新消息，不改写已有历史；
恢复为更早的值也按新变化追加。持久化失败阻断模型请求，不能退化为临时注入。
该机制服务需要审计和恢复的 Harness 消息，不把所有动态 Context Provider 输出强制写入历史。

Fragment 的 `target = message | system_prompt` 默认是 `message`。`system_prompt` target 只允许 ephemeral：
Application 在基础 system prompt 和 Skills 之后按 provider/fragment 稳定顺序追加片段，但不得为它构造 canonical
message。两种 target 都可以由 producer 通过可选 `semantic_tag` 声明语义；标签存在时由 Core 的同一 renderer
校验、转义正文中的同名包络分隔符并构造 model-visible envelope，不存在时正文保持原样。producer 不得把包络
写入 `text`。投影结果计入 token 预算与请求 digest。Context Provider call 可以读取 Host 提供的 Session workspace。

### 10.3 Prompt Policy

Prompt Policy 在 Host command routing 确认输入是模型提示后、持久化 user message 和创建 Run 前执行，返回
`allow | deny`。

- Application 用当前 `ExtensionCatalogLease` 一致地评估全部 active policy；
- 全部 allow 后才接受输入；接收阶段的 deny、timeout、异常或非法返回不创建消息或 Run；
- 排队仅保存输入事实和接收来源，消费时重新评估当前策略；消费被拒绝时保留队列输入，不创建消息或新 Run；
- 多策略按 canonical identity 稳定评估，`deny` 短路；
- Policy 只能拒绝，不能改写提示、注入 Context、创建 Session/Run 或改变 input source；
- 接收被拒绝的提示正文不进入 state、event、log 或 diagnostic，只允许记录无正文的 attributed audit summary；
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

执行事实 `tool_call_count` 和 `automatic_continuation` 由 Application 从该 Run 的原生消息投影。
工具调用同时包括模型请求和 Driver 程序化调用，按轮次和调用身份对请求与结果去重。
前者统计已提交的工具调用，后者表示消费的输入均来自自动继续；混入普通输入时为 false。
同一 Run 内的策略反馈不算新输入；缺少输入来源事实时为 null。
未提供这些事实的观察返回 null，不能当作零次调用或自动执行。续跑策略不得通过回复文本猜测执行事实。
这些字段只描述执行事实；零次工具调用本身不触发框架暂停，继续或停止的业务判断由插件策略返回。

- `continue` 必须包含 prompt 和 `follow_up|steer`；
- policy 对所有 terminal RunOutcome 执行；非 `completed` Run 只允许 `pause | fail`，不能 `continue`；
- 非 continue 决策可以携带一个 release `session_control_transitions` local intent；Application 只允许它释放同 plugin、
  且由该 Run checkpoint 捕获的精确 activation；
- 一个非 ignore 决策由 application 执行；
- 多个控制决策、timeout 或异常使自动续跑暂停；
- application 先持久化 generation-independent settlement plan，再通过原生提交将 Harness、release、queue
  和 checkpoint resolve 一起幂等发布。恢复完成决定，不重调已经形成该决定的插件；
- 已持久化的 settlement plan 只依赖原生提交身份、history epoch、Harness 版本、activation 与队列优先级，
  不需要插件仍然存在；队列来源保留作出决定时的 generation，不改写为当前代次；
- 尚待回调决定的 checkpoint 使用当前 registry 中精确匹配的 policy。不可用、timeout、异常或非法决定使自动
  继续暂停；已有 Session controls 保持有效，管理入口可显式释放。当前环境获取暂时失败时保留未决定的 checkpoint；
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

`batch_behavior` 控制批次是否允许组合；Host 的 `execution_mode` 控制合法批次的执行顺序。含 exclusive 工具的批次
若还有兄弟调用，无论顺序还是并行都必须在任何 handler 执行前整批拒绝。Host 从结构化契约统一生成模型可见的调用说明，
插件无需在 description 中重复维护此规则；呈现不修改插件注册定义和执行契约。详见[工具规格](tools-and-interactions.md)。

工具返回 `ToolInputRequest` 后，只有声明 resumable 且具有 resume handler 的工具能消费回答继续执行；
当前实现必须匹配该调用的 execution contract，并重新通过权限和控制检查。
回答与 running 意图先持久化，再调用 handler；允许再次请求输入。非 resumable 工具的
回答直接成为原调用结果，不需要执行插件。无法承接的调用保存回答并形成局部错误结果。

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
- `models.request`：允许不属于活跃 Run 的独立模型请求，并按插件身份记录供应商用量；
- `sessions.write`：允许请求带完整插件 provenance，以及显式 role、scaffold、semantic tag 契约的 Session input；
- `sessions.prompt.inspect`：允许 Prompt Policy 接收用户提示正文和附件的安全 metadata；
- `background.run`：允许请求 continuation/background Run。

未知 permission 严格拒绝。只有真实 Host service 落地后才新增相应 permission。
对 full-trust runtime，permission 不是 OS 强制边界，界面和文档必须明确这一事实。

### 11.3 Credentials

- Manifest 只声明 slot，不包含秘密或环境变量值；
- activation configuration 把 slot 绑定到环境引用或直接密钥；
- resolver 只解析本插件声明且绑定的 slot；
- 凭据槽位支持 `env:VARIABLE` 或 `{"type":"inline","value":"key"}`，插件统一按槽位解析；
- 直接密钥以明文保存在当前用户专属权限的 state 文件中；普通 DTO JSON、event、diagnostic、exception 和 worker initialize 不包含密钥；
- worker 需要环境变量时使用 Host 生成的临时名称和 allowlisted environment。
- worker close 必须拥有 worker、后代进程树、stdin/stdout/stderr 和 Windows tree-terminator 的完整生命周期；
  业务关闭预算不能通过遗弃 OS 清理进程或等待对象来实现。

## 12. Configuration 与 Data

配置采用自包含 Draft 2020-12 Schema 与独立 credential slots。静态查询、预检、完整候选、默认值、不可变
修订、编辑冲突和 Host 自动交互的精确契约由[插件配置规格](plugin-configuration.md)统一维护。
配置修订属于插件记录，每个 installed digest 引用一个当前修订；不删除仍被执行契约使用的历史修订。
安装落实默认值并保持禁用，缺少必填值或凭据绑定时为未就绪。期望启用的选中包通过候选 generation 应用
配置，候选失败不切换持久引用或 Catalog；发布后的状态按本规格的服务交接规则报告。

插件只能读取自身配置快照和已声明、已绑定的 credential slots。配置不承载业务数据：

- candidate generation 的 Host data API 只允许读取；写入和删除明确失败。发布只改变访问权限，不执行数据提交 I/O；
- generation 关闭后撤销私有 data 写入；停止回调可以在清理完成前保存自身状态；
- `python-inprocess` 是 full trust，直接绕过 Host data API 的 Python/OS 写入不受上述边界约束，也不伪装成沙箱能力。

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
- artifacts 保存用户可见、可由 Host 文件工具处理的插件制品；它不是 candidate 准备时的私有数据事务，也不是 canonical
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

### 私有数据访问、维护与清理

每个 generation 从准备开始持有插件私有数据的共享访问锁，直到调用、后台任务、worker、服务和模块全部关闭。
关闭失败保留占用，管理入口不能根据 desired-disabled 或某一个 Host 的内存状态推断全局资源已退出。
锁由 OS 文件描述符拥有，不建立心跳、持久租约记录或失效时间；不同 Host 和同一进程中的多代可以同时持有共享锁。
Windows 使用 [LockFileEx](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-lockfileex)，
POSIX 使用 [flock](https://docs.python.org/3/library/fcntl.html#fcntl.flock)。

`purge-data` 取得非阻塞独占锁后检查停用状态和持久引用，再清理该插件的 `plugins/data/<plugin_id>`。
启用意图的提交同时取得共享访问锁，因此不能在独占清理期间保存新的启用状态。任一 Host 仍占用资源时明确拒绝，
不强行解除锁。框架锁约束其管理的访问，不是对外部程序或受信 Python 代码的 OS 沙箱。

此操作适用于已停用或已卸载的插件，也可重复执行。可恢复 Run、待消费 Driver 输入及控制 activation 的引用继续
保留数据；查询按插件身份扫描，不要求 install record 仍存在。清理只删除私有目录，不删除 Session 扩展状态、
Task 计划或历史、用量账本、用户制品及其他插件数据。成功操作进入插件审计。CLI 不为清理启动插件或模型客户端。

`plugin maintain <id> --execute <argv...>` 使用相同的独占访问、停用与持久引用检查，在已存在的私有目录中运行
用户明确指定的维护程序。入口不加载插件，按 argv 执行，不插入 shell；输出缓冲有界，支持超时，取消和退出均
等待受管理进程树收尾后才释放访问锁。结果与退出状态进入审计，不记录命令参数或输出内容。维护不修改启用意图；
成功、失败和取消后均保持停用。命令不具备 OS 沙箱隔离。

数据格式由插件拥有。`prepare()` 可只读校验现有数据并拒绝不支持的格式；启用失败保留启用意图，维护前必须显式
停用。插件自己的维护程序定义校验、备份和恢复边界，框架不推断业务格式或自动转换。`scheduled-sync` 样例只接受
schema 1 快照；备份原始字节到私有目录外且不覆盖已有备份，恢复前校验输入并保存当前原始快照，再原子替换。
维护不会自动修复或回滚任意程序的外部副作用。

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
- 清理失败或尚未结束时，generation 继续保留资源所有权和诊断，阻止同一插件再次激活；
- 保存有界、脱敏 diagnostic；
- 不影响无关插件和 builtin。

Host 冷启动使用一次共享状态快照规划所有期望启用插件，按依赖顺序为每个未激活插件构建一次候选，统一发布后
启动后台服务；不能逐根套用显式 restart 的依赖方重建规则。依赖解析和已解析节点只在本次操作中复用。
一个候选的 prepare/start 失败只阻断其依赖方，健康候选继续使用；目录贡献冲突按 owner 丢弃冲突候选及依赖方后
重新投影，健康实例不重复构建。状态在构建期间发生变化则禁止发布旧候选，清理后由下一同步边界处理新状态。
显式启用、更新和重启仍要求对应变更事务完整成功，不将启动的独立失败隔离改成部分提交配置。

当前单进程 Host 使用一个 runtime mutation lock 串行化候选提交，不建立容易死锁的 per-plugin 多锁图；插件代码、
I/O 和 start/stop 均不进入 catalog pointer lock。Background Service 的声明与 owner 在候选期完成准备，但实际
`start` 延迟到发布后，并严格等待旧代 `stop` 成功完成；旧服务停止失败时保留其归属，新代降级且不启动替代服务。
Generation lease 可以继续服务已绑定的模型响应及调用；Background Service 由实际启动它的 generation 拥有，成功停止后才能交给新代。

生命周期回调在已提交 Catalog 上接纳，其嵌套模型请求直接从该 Catalog 绑定所选 Provider 及必要依赖，
不再次等待本次生命周期的状态同步，也不持有全目录租约。Host 和 home 服务的启动及交接遵循相同规则。
回调结束即释放这项临时目录引用；继承执行上下文的后台任务也不能永久使用它。新的独立 Provider 请求仍先同步共享意图。
其他 Host 在回调期间写入的状态变化由后续准入边界处理，不使已经发布的初始化回入自身或并发启动新旧服务。

Manifest 的每个 `background_services` 声明可指定 `scope`：默认 `host` 在每个已激活的 Host 中运行一份；
`home` 仅在持有现有 TaskScheduler owner 锁的 Host 中运行。scope 属于执行契约，SDK 只提供 start/stop 实现。
未参与调度选举的管理进程不会启动 home 服务。同一 owner 内的启用和升级沿用 generation 的发布与停止顺序。
调度器停止时先等待 home 服务释放资源，仍持有 owner 锁以结算原有 Task；close 才释放锁。
服务停止失败时保留 owner 锁并报告错误，禁止另一个 Host 在旧服务未停止时接管。服务 stop 必须等待自己的任务、
连接和进程实际退出；普通任务或实例启动不能绕过这一归属约定。该机制复用现有跨进程文件锁，不增加租约注册表。

Disable：

1. 持久化 desired disabled；
2. 关闭该插件当前及退休 generation 的 gate，阻止新调用并取消正在执行的回调；
3. 发布不含该 generation 的新 catalog；
4. 等待实际执行的回调退出；已撤销的空闲 Catalog 引用不阻止清理；
5. 停止 Background Service、instance、task、MCP 和 worker，释放模块；
6. 清理成功后释放 owner 并呈现 `disabled`。

`disable(wait=True)` 在本 Host 的停止预算内等待，未结束返回 `stopping`，清理失败返回 `failed`。
`disable(wait=False)` 只等待入口撤销和目录发布；`wait_stopped(plugin_id)` 可再次等待本 Host 的清理结果。
调用方取消不会遗弃清理；失败资源保留归属，显式再次 disable 或 Host shutdown 可以重试清理。
插件撤销的取消限于回调范围，对外呈现不可用错误；它不取消等待回调的 Host 任务，也不接受吞掉取消后的迟到成功结果。
卸载和私有数据清除必须等待本 Host 相应资源退出；卸载某一 digest 不要求无关版本退出。
Host shutdown 按已发布 Catalog 的依赖顺序关闭资源，不依赖持久状态文件仍然可读。

进程内插件遵守合作式停止。同步回调的线程不能强杀，取消后仍保留执行归属直到线程结束；其迟到结果不作为成功
提交。worker 的停止 RPC 和进程树清理由同一 owner 收尾。上述完成状态只代表当前 Host，不声明其他 Host 已停止。

### 共享 Home 的状态传播

共享 state 文件承载 selected release、配置和启用意图。Host 在接受 Run、开始下一模型轮次、执行插件命令及
已有调度器的刷新周期检查文件身份、修改时间和大小；未变化时不解析 state，不重新校验包。发生变化后在本 Host
的 mutation lock 下协调启停和依赖绑定。调度刷新对 owner 与非 owner Host 都生效，默认最长刷新间隔为 30 秒，
不增加每插件 watcher 或后台循环。移除意图先撤销入口，不等待新候选构建成功。

变更文件在读取或构建期间再次替换时，下一个边界继续协调。独立插件的激活失败不阻止其他有效组件发布；尚未
退出的旧资源继续保留，清理未完成时不重启同一插件。关闭结果只承诺当前 Host；无全员 ACK、Host 名册或跨进程
瞬时停机保证。私有数据维护必须取得独占访问；尚未处理停用意图的 Host 仍持有共享锁时，维护明确拒绝。

## 15. Durable Run

每次模型决策取得当前 `ExtensionCatalogLease`。同一响应及其工具调用共享该绑定，下一轮决策可以采用新环境。
审批、用户回答和 child join 的等待不持有整组插件实例。历史消息与已完成结果不要求产生它们的工具仍然存在。

待完成插件调用保存精确执行契约：plugin id、release（user package digest 或 builtin contract hash）、配置 revision、
传递依赖、Plugin API/worker protocol 及 contribution contract。`generation_id` 是进程实例和审计身份，不是跨重启
等价性的判据。当前同一契约的新实例可承接；包、配置或契约不匹配时，调用产生明确错误，不转调同名新实现。
框架不重建历史 Catalog，也不为继续 Session 要求无关插件的旧代码或配置。

Prompt Policy 在接受直接用户输入前执行；排队输入在消费前按当前策略再检查。已接受的输入保留原文、技能正文、
来源及接受凭据，旧策略允许不覆盖未来执行。Observer 观察结果不成为 Session 恢复前提。

恢复先处理局部事实，再开始下一次允许的模型决策：

- 已完成结果按原 ToolCall identity 复用；缺失工具、契约不匹配或权限收紧返回真实局部结果；
- 请求已经发出但结果不确定时保留 unknown outcome，不自动重放；
- 必需控制器缺失或不兼容时保持限制，管理入口可检查、正常释放或显式 force release；
- 已接受 continuation 直接提交持久方案，未决定的回调只在当前精确契约存在时执行；
- 通用工具帧保持完整，不伪造成功、不清空会话，也不把局部调用失败扩大为新建 Session 的要求。

只支持当前持久数据格式与对应 owner 状态版本；组件版本更新不等于支持任意数据迁移。无法解释的数据保留并明确
报告。数据支持范围见[会话与持久化规格](session-persistence.md)。

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
- 有 active dependent 或本 Host 尚未退出的资源时拒绝；
- 删除 install reference，结果列出仍保留该包的可恢复 Run、待消费输入或控制 activation；这些引用不阻止逻辑
  卸载，也不重新启用或装载代码。未安装的实现不能继续执行，恢复前需显式重新安装所需包并满足精确配置契约；
- GC 按管理冷路径一次扫描持久引用，保留其包字节；已完成历史和 Catalog 摘要不构成保留条件。结束相关运行后
  可再次 GC。仍安装的包和任一 Host 仍持有数据访问的插件也不会删除；
- 发布包与提交 install record 使用同一个插件文件锁，GC 在锁内核对最新安装状态；发布暂存目录按插件归属，
  同一锁保护其创建和清理，不回收正在发布的文件；
- 默认保留 plugin data；
- `remove --purge-data` 只允许删除最后一个 digest 时同时清理数据，并在独占访问下检查该插件所有 release 的
  持久引用，包括已经逻辑卸载的包；独立 `purge-data` 使用相同的数据保全规则。

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
- update、reload、select、rollback、remove、gc、purge-data、maintain；
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
之后的命令异常不使已经创建的会话失去身份。命令准备在一次锁内捕获 Session/Harness，离开事件循环
执行；命令使用当前 Catalog，复用已经捕获的上下文，不再次读取或跨命令缓存。
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

插件列表从一次读取的持久状态投影配置意图和依赖状态，并结合当前 Host 的 generation 状态呈现。状态投影复用调用方持有的快照，不按列表项重复读取，也不跨管理操作缓存持久状态。

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

职责划分：

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
  catalog.py         # immutable ExtensionCatalog, invocation gates and contribution bindings
  dependencies.py    # dependency graph and reconciliation inputs
  generation.py      # candidate/active resource ownership and shutdown
  resources.py       # generation-owned tasks and private data access
  generation_resources.py # resource adapters used during candidate preparation
  runtime.py         # candidate publication, service handover and retirement
  worker.py          # worker protocol/process adapter
  compatibility.py   # community inspection/import

ouro/sdk/plugins/v5.py
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
- 并发决策可持有不同 Catalog，在途调用不被替换，等待不延长整组环境寿命；
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
- 新旧 Catalog 并存时，同一插件的同一 Background Service 在声明的 host/home 作用域内最多运行一份；
- candidate prepare/start 的 Host data 写入明确失败，失败候选不修改共享 plugin data；
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
- Prompt Policy 的 allow/deny、失败关闭、permission 及接受与消费边界语义可由 application 集成测试证明；
- 接收 deny 不创建 canonical user message、Run 或 continuation；消费 deny 保留已接受输入，diagnostic 不泄露提示正文；
- in-process 与 worker 的 observation/policy 类型、错误、取消和 contract 行为等价。

Plugin Skill 不转换为 Observer；`RuntimeService` 每轮从该 Run 的
`ExtensionCatalogLease.skills` 生成 Skill 提示词。Plugin MCP 只为 connected server 生成 contract，required 失败
中止候选，optional 失败产生 degraded；发现工具结构冻结在 Catalog，连接由 generation owner 在 retire 时关闭。

### Durable recovery

- 待完成操作的相同 execution contract 可跨进程校验；
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

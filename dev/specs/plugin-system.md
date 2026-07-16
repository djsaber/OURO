# OURO 插件系统目标规格

状态：首个稳定面已实现（2026-07-16），长期目标尚未全部实现；准确边界见第 24 节

本文定义 OURO 插件系统的包、来源、信任、授权、运行时、贡献、生命周期、兼容和恢复契约。schema v1、
安装/存储、entrypoint、基础 worker、授权、generation、应用 facade 与 CLI 已实现。本文前半部分保留目标
架构与长期约束，不等于每一项均已交付；当前能力、部分实现与明确延期项在第 24 节逐项列出，不以空接口
或预留字段表示支持。

外部项目事实与设计经验见
[Agent 插件系统调研报告](../../docs/Agent_插件系统调研报告_2026-07-16.md)。

## 1. 目标

OURO 插件系统应同时满足：

- **插件原生**：插件是 application 装配的一等资源，不是 CLI 启动脚本或 prompt 拼接约定；
- **深度扩展**：稳定扩展工具、Hook、命令、上下文、压缩、Skills、MCP 和受监督后台服务；
- **本地优先**：安装记录、授权、配置、代码缓存、数据和诊断可在本地审计、导出和删除；
- **Host 中立**：插件不直接依赖 CLI/TUI/Desktop，Host 只映射类型化 command、configuration 和 event DTO；
- **durable-safe**：升级、禁用和崩溃不能破坏 Run 事务、工具恢复、事件顺序和持久化 lineage；
- **兼容优先但不伪兼容**：复用 Agent Skills、MCP 和社区包中的可移植资源，对不可移植代码给出明确报告；
- **明确的信任边界**：不把 Python 进程内权限声明或普通子进程包装宣称为安全沙箱；
- **可演进**：包 schema、Plugin API、worker protocol 和 OURO 产品版本分别协商，不以内部模块路径作为 API。

## 2. 非目标

首个完整版本不提供：

- 在 CPython 进程内对恶意插件的安全沙箱；
- 对 Claude Code、Codex、Pi 或 OpenCode 任意可执行插件 API 的透明仿真；
- 允许插件绕过 `ToolExecutor`、application service 或 SessionRepository 直接接管 canonical 状态；
- 任意 HTML、Python widget 或 Host 专用 UI extension DSL；
- 在安装阶段执行 npm lifecycle、Python sdist build、任意 shell installer 或插件自定义迁移脚本；
- 通过 manifest 自报 `official`、`verified` 或自动执行资格。

## 3. 强制不变量

以下约束优先于扩展便利性：

1. `ouro.core` 不读取 manifest、不 import entrypoint、不依赖插件包格式。
2. 插件管理用例位于 application 层；文件、Git、archive、Python import 和 worker process 位于 adapter 层。
3. discovery、install、enable、code trust、capability authorization 和 activation 是不同状态。
4. 未满足 code trust、权限授权和必填配置前，宿主不得 import entrypoint、启动 worker 或启动插件 MCP server。
5. provenance 只由 installer/store 根据实际来源生成，manifest 中不存在可决定信任级别的字段。
6. 插件贡献只能经 owner-aware registry transaction 发布；失败时不得留下部分必需 contribution。
7. 插件工具始终进入 `ToolExecutor`；第三方工具不得仅凭 manifest 获得 `auto`。
8. 插件不得通过 Hook 放宽用户权限、批准自身工具、修改 durable sequence 或直接写 SessionStore。
9. 插件异常、超时、日志和公开 DTO 经过统一脱敏；observer 失败不能回滚已持久化事件。
10. 代码缓存不可变，插件数据与代码版本分离；卸载默认保留数据，清除数据必须显式请求。
11. Run 使用不可变 extension generation；更新不能让一个 Run 在执行中混用两个插件版本。
12. pending tool call、approval 和恢复记录必须能验证插件 id、版本、digest、generation 与工具 contract hash。
13. 所有后台 task、进程、MCP 连接、订阅和 listener 都由一个可关闭的 plugin owner 管理。
14. Host 只使用 application DTO/facade；CLI/TUI/Desktop 不直接操作插件目录、registry 或 import adapter。

## 4. 四层模型

插件系统明确拆成四层：

```text
Plugin Package
  manifest + immutable files + optional runtime
        |
        v
Installed Plugin
  provenance + digest + version + dependency graph
        |
        v
Authorized Activation
  desired state + code trust + capability grants + config refs
        |
        v
Plugin Generation
  instance + contribution snapshot + resources + run leases
```

- **Package** 说明插件是什么；
- **Install record** 说明字节从哪里来、实际安装了什么；
- **Authorization** 说明用户允许当前内容做什么；
- **Generation** 说明哪个不可变版本正在为哪些 Run 服务。

不得用一个可变 `Plugin` 对象同时承担这四种状态。

## 5. 身份与记录

### 5.1 Plugin identity

- `plugin_id` 使用反向域名风格，例如 `dev.ouro.calendar`；安装后不可由更新改变。
- `version` 使用 SemVer，只描述插件发布版本。
- `package_digest` 对规范化文件树计算 SHA-256，是实际代码内容身份。
- `plugin_api` 描述宿主/插件编程协议版本。
- `manifest_schema` 描述 manifest 格式版本。
- `generation_id` 由 OURO 在每次成功激活时生成，不能由插件指定。

版本相同但 digest 不同必须视为不同内容，并产生 `version_digest_mismatch` 诊断；不能静默覆盖。

### 5.2 Provenance record

Installer 生成不可变 `PluginProvenance`，至少包含：

- source kind：`builtin`、`local`、`git`、`archive`、`catalog`；
- 用户输入的来源标识和脱敏后的公开形式；
- resolved commit/ref/version；
- package digest；
- 可选 publisher/signature/trust-root 验证结果；
- 安装时间、安装作用域和 installer 版本。

Marketplace/catalog 只是索引来源，不自动成为信任根。签名验证成功只证明来源身份，不证明插件行为安全，
也不授予自动工具权限。

### 5.3 Persistent state 与 derived status

持久化状态只记录事实和用户意图：install records、selected digest、desired enablement、授权、配置引用和数据
版本。`starting`、`active`、`degraded`、`failed` 等是运行时投影，不作为跨崩溃的 canonical 状态。

对外状态至少包括：

- `invalid` / `incompatible`；
- `disabled`；
- `needs_configuration`；
- `needs_code_trust`；
- `needs_authorization`；
- `ready`；
- `starting` / `active` / `degraded` / `stopping` / `failed`。

### 5.4 Scope 与选择规则

激活 scope 为 `builtin`、`managed`、`user` 或 `workspace`：

- builtin identity 永久保留，其他 scope 不能替换；
- managed policy 可以限制 source、publisher、版本和权限，但不能让 manifest 自报 managed；
- user activation 作为默认，已信任 workspace 可以显式 pin 同一 plugin id 的另一 digest；
- workspace 未被信任前，只允许静态 validate，不 import、不启动进程、不请求 credential；
- 同一 workspace 的同一 plugin id 只能选择一个 digest，冲突必须展示来源并要求用户选择；
- grant 绑定 scope，workspace grant 不能自动扩大为 user/global grant；
- 临时开发加载使用独立 `development` 状态和显著诊断，不写成普通不可变 install record。

Worker 可以让不同 workspace 并行使用同一 plugin id 的不同 digest。`python-inprocess` 因 Python module cache
和全局状态无法可靠隔离，同一 OURO 进程不得并行激活同一 plugin id 的多个 digest；需要这种能力的插件必须
使用 worker，或由 Host 使用独立 OURO 进程。

## 6. 原生包格式

### 6.1 目录布局

原生插件 manifest 位于 `.ouro-plugin/plugin.json`：

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
  assets/
```

当前根目录 `plugin.json` 作为 legacy schema 读取一个兼容周期，但新安装器不得生成该布局。原生 manifest
存在时优先；同时存在且内容冲突时安装失败，不能猜测。

路径字段必须：

- 以 `./` 开始；
- 使用 `/` 作为 manifest 分隔符；
- 不包含空段、`.`、`..`、驱动器或 UNC；
- 解析后位于 package root；
- 指向安装时已存在的普通文件或目录。

首个版本拒绝所有符号链接、junction 和 reparse point。未来若允许，只能允许解析后仍在包内且被 digest
覆盖的链接。

### 6.2 Manifest 示例

```json
{
  "schema_version": 1,
  "plugin_id": "dev.ouro.calendar",
  "name": "Calendar",
  "version": "1.2.0",
  "description": "Calendar tools and context.",
  "namespace": "calendar",
  "engines": {
    "ouro": ">=0.1,<0.2",
    "plugin_api": "^1.0"
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
    "hooks": [
      {"id": "daily_context", "event": "context.enrich", "priority": 100}
    ],
    "skills": [
      {"id": "calendar", "path": "./skills/calendar"}
    ],
    "mcp_servers": [
      {"id": "calendar", "path": "./mcp/calendar.json", "auto_start": false}
    ]
  },
  "permissions": [
    {
      "id": "network.outbound",
      "scopes": ["api.example.test:443"],
      "reason": "Read the user's calendar."
    },
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
  },
  "metadata": {}
}
```

### 6.3 Schema 规则

- 安全相关对象使用 `extra=forbid`；未知字段不能被静默忽略。
- 第三方扩展 metadata 只能放在 `metadata` 的 namespaced key 中，不参与安全决策。
- manifest 不包含 credential 值、环境变量值、来源信任等级和用户授权结果。
- 所有 contribution 必须静态声明 identity、类型和简要说明，以便 import 前完成审计。
- 动态发现只允许在该 contribution 类型明确支持时使用，并必须声明唯一 namespace/prefix 和上限。
- schema 重大语义变化增加 `schema_version`，不能通过产品版本猜测字段含义。

## 7. Runtime 类型

### 7.1 `declarative`

无 entrypoint，只包含 Skills、prompt resources 和不自动启动的 MCP 配置等静态资源。没有可执行资源时不要求
code trust；启动 MCP server 前仍需完成对应代码信任和权限确认。

### 7.2 `python-inprocess`

Python factory 在 OURO 进程中运行，提供最深扩展能力和最低调用开销。它是 **full-trust runtime**：

- permission 只能限制 OURO 提供的 service/API，不能阻止插件直接调用 Python/OS API；
- 启用前必须明确展示“同进程完整用户权限”，并将 code trust 绑定到 digest 或受信 publisher policy；
- 普通安装不得向 OURO 主环境执行 `pip install`；
- 插件依赖必须由包自带且位于插件私有 namespace，或只使用公开 Plugin SDK/标准库；
- top-level module 与其他已激活插件或 OURO 包冲突时拒绝激活；
- local development link 是单独命令和状态，不能伪装成普通不可变安装。

该 runtime 适合 builtin、官方维护和用户明确完全信任的深度插件，不是第三方插件的默认推荐分发方式。

### 7.3 `worker`

Worker 通过版本化双向 JSON-RPC/stdio 协议贡献工具、Hook、命令和 Host service 调用：

- 长期目标是每个 digest 拥有独立依赖环境；Python 只安装带 hash 的预构建 wheel，拒绝 sdist/build script；
- stdout 专用于协议，stderr 进入限流、脱敏 diagnostic；
- 当前进程、请求超时、cancel 通知和 shutdown 由 plugin owner 管理；自动重启/熔断属于后续 supervisor；
- 当前初始化校验精确的 worker protocol 版本并传递 plugin id、digest、grants、feature flags 和调用上限；
- runtime 返回的 contribution 必须与 manifest 声明匹配；
- worker crash 隔离于 OURO 进程，依赖也不进入主解释器。

Worker 提供崩溃和依赖隔离，**不自动提供 OS 权限隔离**。未来 OS sandbox 只能作为 worker process runner 的
附加 policy，不改变 worker protocol 或夸大当前安全性。

当前协议方法：

- `initialize`、`contributions.prepare`、`lifecycle.start`、`lifecycle.stop`、`shutdown`；
- `tool.call`、`hook.call`、`command.call`、`compaction.call`；
- `cancel`、`ping`、`diagnostic.emit`。

反向 `host.resources.*`、`host.sessions.*`、`host.models.invoke`、`host.credentials.resolve` 是后续协议扩展，
当前 worker 不得假定这些方法存在。

## 8. Plugin API

### 8.1 稳定入口

插件只能依赖独立、版本化的 `ouro-plugin-sdk` 公共契约；不得以 `ouro.application.*`、`ouro.core.*` 或
具体 registry 类作为公共 API。仓库内可以先由 `ouro.sdk.plugins.v1` 承载，发布时仍保持独立 API 边界。

Python entrypoint 形态：

```python
def create_plugin(context: PluginContext) -> PluginInstance: ...

class PluginInstance(Protocol):
    async def prepare(self) -> ContributionSet: ...
    async def start(self) -> None: ...
    async def stop(self, reason: StopReason, deadline: float) -> None: ...
```

- import/factory/`prepare` 不得启动后台任务、打开长期连接或修改 registry；
- `prepare` 只构造 contribution 和受 owner 管理的资源描述；
- `start` 只能通过 context task group/supervisor 启动长期资源；
- `stop` 必须幂等，适用于正常禁用、启动失败回滚、升级、崩溃恢复和 Host shutdown；
- Host 最终仍强制取消逾期 task/worker，不能无限等待插件自行关闭。

### 8.2 PluginContext

Context 只暴露按授权裁剪的稳定 service ports：

- identity、provenance 摘要、generation 和 abort signal；
- 只读配置视图和 credential reference resolver；
- 私有 data/resource store；
- 脱敏 logger 和 structured diagnostics；
- 受监督 task/process client；
- 后续可增加经授权的 session query、resource、model invoke 等 application service；
- 后续可增加当前 workspace/session/run 的最小类型化 view，仅在具体调用需要时提供。

不得暴露 RuntimeService、SessionStore、底层数据库连接、全局 registry、Host 对象、原始配置文件或凭据仓库。
即使授予 `sessions.write`，Context 也只得到类型化 application command，不得到任意 record mutation 或 repository
句柄；事务、状态转换、事件和恢复不变量仍由 application 层执行。

### 8.3 API 兼容

- `plugin_api` 遵循 major/minor：major 可破坏，minor 只能增加可选字段、方法或 feature；
- 当前 worker handshake 只接受精确 `1.0`；共同 minor/feature 协商在协议出现首个兼容扩展时再引入；
- 插件只能使用 manifest `engines.plugin_api` 声明范围内的能力；
- Host 对已废弃 API 提供至少一个明确迁移周期和 diagnostic；
- durable record 存 contribution contract version/hash，不能仅按 Python 类型名恢复。

## 9. Contribution 模型

### 9.1 首个稳定 surface

完整插件生命周期首先支持当前已有稳定边界：

- `tools`；
- `commands`；
- `hooks`；
- `context_providers`；
- `compaction_strategies`；
- `skills`；
- `mcp_servers`；
- `background_services`，但只作为受监督生命周期资源，不获得新的 core 状态写入口。

当前 placeholder 中的 `model_providers`、`ui_extensions` 不得继续假装为已支持 capability。Provider factory
只有在 `RuntimeAdapterFactory`、配置 schema、credential 和 close contract 成为稳定 Plugin API 后才能作为
新 API minor/major 加入。UI 在真实 TUI/Desktop 共同需求出现前只使用 commands、configuration schema、
events 和 application DTO。

### 9.2 Canonical identity 与显示名

所有 contribution 具有不可变 canonical identity：

```text
<plugin_id>/<kind>/<local_id>
```

- command 对外 identity 保持 `<plugin_id>:<local_id>`；
- plugin tool 暴露名使用 manifest namespace，例如 `calendar__search`；
- Skills、MCP server、context provider 和策略都保留 owner metadata；
- Host 可显示别名，但 durable record 和 registry 冲突使用 canonical identity；
- `builtin/*`、未加 namespace 的 builtin tool 和 `builtin.*` 策略空间永久保留；
- 插件不能覆盖 builtin，也不能通过 load order 静默覆盖其他插件。

namespace 冲突、exposed tool name 冲突或 canonical id 冲突使整个必需 contribution set 激活失败，并给出
双方 owner、来源和解决建议。用户别名只能改变呈现，不能改变 canonical identity。

### 9.3 Owner-aware transaction

引入 application 层 `ContributionCoordinator`，以 `(plugin_id, package_digest, generation_id)` 为 owner：

1. 收集 `ContributionSet`；
2. 校验 manifest 声明、capability grant、schema、identity 和冲突；
3. 生成不可变 `PreparedContributionSet`；
4. 在一个 coordinator lock 中对各 registry 执行 compare-and-swap；
5. 任一步失败都恢复提交前快照；
6. 返回 `ContributionLease`，关闭 lease 即按 owner 撤销所有 contribution。

Tool、Hook、command、compaction 和后续 registry 必须支持 `replace_owner`/`unregister_owner`，不能依赖调用方
保存一组松散名称。当前 `PluginLoader` 的逐类部分成功仅作为 legacy bridge；完整 activation 默认 all-or-none。
只有 manifest 显式标为 `optional` 的 contribution group 可以失败后进入 `degraded`，并必须出现在状态和 DTO。

## 10. Hook 协议

### 10.1 类型分类

通用可变字典 Hook 会使安全和组合语义失控。稳定 API 应按事件定义不可变 input 和类型化 output：

| 类别 | 示例 | 组合方式 | 默认失败策略 |
|---|---|---|---|
| observational | durable event、after run、diagnostic | 独立并发/有界队列，结果不修改语义 | fail-open |
| enrich | context enrichment、model metadata | 确定顺序，追加有来源的 typed fragment | 隔离失败项 |
| transform | 受控 request patch | 确定顺序，校验每个 typed patch | 拒绝无效 patch |
| guard | tool/permission preflight | 只能 deny 或 escalate，不能放宽 | fail-closed 或回到用户审批 |
| lifecycle | run start/stop、plugin start/stop | 确定顺序、幂等 | 按事件定义 |

### 10.2 排序

- builtin policy 使用保留 phase，不与第三方 priority 竞争；
- 第三方默认按用户显式 order、manifest priority、plugin id、hook local id 排序；
- provenance 的 `verified`/`official publisher` 不自动获得更高语义优先级；
- 同一配置和插件集合必须跨进程得到相同顺序；
- observer 可以并发，但结果和 diagnostic 仍按稳定 identity 呈现。

### 10.3 安全单调性

- guard Hook 可以拒绝、缩小 scope 或把 `auto` 提升为 `approval`；
- guard Hook 不能把 `deny`/`approval` 降为 `auto`；
- 插件不能批准由自己贡献的工具；
- transform Hook 不能修改 session/run/tool identity、approval id、durable sequence 或 provenance；
- context fragment 保留 plugin id、generation、trust class 和 token estimate；
- 超时或异常按 Run 隔离该 Hook，Run 结束清理临时隔离状态。

## 11. 权限与授权

### 11.1 三个独立维度

1. **Contribution capability**：插件希望注册哪些扩展类型；
2. **Host-service permission**：插件希望通过 OURO API 做哪些操作；
3. **Code trust**：用户是否允许当前 digest/publisher 在对应 runtime 中执行。

三者不能用一个 `trusted=true` 合并。

### 11.2 Permission request

权限使用稳定 id、最小 scopes 和必填 reason，例如：

- `filesystem.read` / `filesystem.write`，scope 为 workspace-relative glob 或 plugin data；
- `network.outbound`，scope 为 host/port pattern；
- `process.spawn`，scope 为允许的 executable identity；
- `credentials.use`，scope 为 credential ref 名称；
- `sessions.read` / `sessions.write`；
- `resources.read` / `resources.write`；
- `models.invoke`，scope 为 provider/model/budget policy；
- `background.run`。

这些权限在 Host service 边界强制执行。对 `python-inprocess` 和未沙箱 worker，它们同时是审计声明，不能
阻止代码绕过 Host service；授权 UI 必须明确展示这一限制。

### 11.3 Grant binding

`CapabilityGrant` 至少绑定：

- plugin id；
- scope（user/workspace）；
- permission request fingerprint；
- contribution capability fingerprint；
- runtime kind；
- 用户/策略来源和时间。

`CodeTrustGrant` 绑定 exact package digest，或绑定明确配置的 publisher key + 允许的版本/权限更新 policy。

- 权限或 capability 扩大：必须重新授权；
- runtime 从 declarative/worker 改为 in-process：必须重新 code trust；
- publisher/source identity 改变：必须重新 code trust；
- 权限缩小：可保留 grant，但记录新 fingerprint；
- unverified/local executable 默认每个新 digest 需要确认；
- verified publisher 是否允许无交互补丁更新由用户 policy 决定，默认仍提示。

工具 `auto` 是额外的 per-tool execution grant，不由 manifest 或 code trust 推导。

## 12. 配置、凭据与数据

### 12.1 Configuration

- 插件配置由中央配置管理器按 plugin id 和 scope 保存，不改写第三方 package；
- schema 使用受支持的 JSON Schema 子集并在授权/import 前静态校验；
- public DTO 只返回脱敏值、字段状态和 credential ref，不返回秘密；
- 缺失必填配置时状态为 `needs_configuration`，不得 import/start；
- 配置变化先校验和原子写入，再触发 generation replacement；
- 插件只能读取自己的配置，不能读取完整 OURO config。

### 12.2 Credentials

Manifest 只声明 credential slot。用户将 slot 绑定到现有 credential ref；运行时通过授权 service 按需解析。
秘密不进入 manifest、安装记录、事件、日志、异常、worker initialize payload 或公开 DTO。若 worker 确需环境变量，
Host 只为声明且授权的 slot 创建临时 allowlisted environment，并在诊断中隐藏值。

### 12.3 Data

推荐布局：

```text
OURO_HOME/plugins/
  store/<encoded-plugin-id>/<package-digest>/
  envs/<package-digest>/
  data/<encoded-plugin-id>/
  staging/
  locks/
```

- store/env 不可变，可按引用和 lease 垃圾回收；
- data 跨版本保留，不位于代码目录；
- 插件优先使用 scoped Resource/KV service，而不是任意文件路径；
- remove 默认删除 install/activation 引用，保留 data；`remove --purge-data` 单独确认；
- 数据迁移、data schema 和 rollback compatibility 尚未进入首个稳定面。引入时必须满足：只在新代码已
  code-trusted 后执行，迁移前备份，失败恢复；不可逆迁移明确阻止自动 rollback。

## 13. 来源、安装与依赖

### 13.1 Source providers

统一 `PluginSourceProvider` 支持：

- local directory；
- Git URL + optional ref/subdirectory；
- 本地 archive；
- 后续 catalog/registry 解析出的以上来源。

Provider 只负责 resolve/materialize，不负责授权或激活。Git ref 必须解析为 commit；catalog 条目必须解析为
不可变版本/digest 才能安装。

### 13.2 安装事务

安装流程：

1. 在受限临时目录 materialize；
2. 校验文件数、单文件/总大小、路径、压缩比、manifest 和兼容范围；
3. 拒绝 symlink/reparse、设备文件、socket、VCS/cache 和越界路径；
4. 对规范化相对路径、类型、长度和文件字节计算 tree digest；
5. 生成 provenance 和 manifest/permission fingerprints；
6. 复制到同文件系统 staging；
7. fsync/rename 到 digest store；
8. 最后原子写 install record；
9. record 写失败留下的无引用 store 是安全 orphan，由 GC 清理。

安装不自动 enable，不 import、不启动代码、不执行 package script。更新先并排安装新 digest，不覆盖旧目录。

### 13.3 Dependencies

- Plugin dependency 使用 plugin id + SemVer range + optional feature；
- resolver 生成固定版本和 digest 的 lock graph；
- cycle、身份冲突或不满足范围使整组安装/更新失败；
- dependencies 先激活，按逆拓扑停止；
- 有 active dependent 时不能 remove dependency；
- 自动安装的孤儿依赖可以 prune，但数据仍默认保留；
- Python/Node library dependency 不是 plugin dependency。worker 自带独立、锁定的运行环境，in-process 不向
  主解释器动态安装依赖。

## 14. Activation 生命周期

### 14.1 启动

```text
installed
  -> validate compatibility/config
  -> validate code trust/grants
  -> instantiate runtime
  -> prepare contributions
  -> validate complete set
  -> commit owner transaction
  -> start supervised resources
  -> publish active generation
```

任何步骤失败：

- 不影响其他插件和 builtin；
- 撤销已提交 contribution；
- 调用幂等 stop 并强制清理 task/process/connection；
- 保存脱敏、稳定 error code 和失败 phase；
- Host 继续启动，插件状态为 failed/degraded。

### 14.2 Disable 与 shutdown

Disable 先阻止新 Run 获取 generation lease，再等待现有 lease 释放。没有 active lease 后按以下顺序关闭：

1. 标记 stopping 并取消新 background work；
2. stop plugin instance/worker；
3. 关闭 MCP、listener、task group 和进程树；
4. 释放 contribution lease；
5. 清理 per-run Hook isolation 和 transient handles；
6. 投影 disabled。

Host shutdown 按依赖逆序关闭，并有总 deadline；逾期资源强制取消。`stop`、lease close 和 unregister 必须幂等。

### 14.3 Run generation lease

- 新 Run 开始时获取不可变 `ExtensionCatalogSnapshot`；
- snapshot 包含 contribution owner、plugin id/version/digest/generation 和 contract hash；
- Run 的模型工具目录、Hook 和 compaction selection 都来自该 snapshot；
- 更新只影响后续 Run；旧 generation 在最后一个 Run/恢复 lease 释放后关闭；
- pending approval/tool batch 持久化 owner identity；恢复时优先重建 exact generation；
- exact digest 不存在或不兼容时进入可解释的 recovery failure，绝不能用同名新工具代替。

这是 OURO durable Run 与普通编辑器插件系统之间最重要的差异。

## 15. 更新、回滚和卸载

### 15.1 Update

1. side-by-side 安装候选 digest；
2. 计算版本、manifest、capability、permission、runtime、config/data schema diff；
3. 缺少新授权时保持旧 generation active，候选状态为 needs authorization；
4. 完成 code trust 和 migration backup；
5. prepare 候选并验证 contribution；
6. 为新 Run 发布新 generation；
7. 旧 generation drain 后关闭；
8. 保留至少一个可回滚 digest，直到用户 policy/GC 清理。

不能以原地 reload 模块对象代替 generation replacement。开发模式可以提供显式 reload，但仍必须创建新
generation 并使旧 context 失效。

### 15.2 Rollback

- 选择已有 immutable digest，不重新从网络下载同版本；
- 重新验证当前 OURO/API 兼容、code trust 和 grant；
- 检查数据 schema 是否支持向后；
- 使用相同 activation 流程发布旧版本的新 generation；
- rollback 失败时当前 active generation 不变。

### 15.3 Remove

- 先执行 disable/drain；
- 有 dependent 或 durable recovery lease 时默认拒绝；
- 删除 install selection 和授权引用，再由 GC 删除无引用 store/env；
- 默认保留 data、credential binding 的引用状态和审计记录；
- purge data/credentials 是单独、显式且可预览的操作。

## 16. Community compatibility

### 16.1 兼容原则

兼容分为三个等级：

1. **Native**：完整 OURO manifest 和 Plugin API/worker protocol；
2. **Portable resource**：Agent Skills、MCP 等有共同协议的资源直接复用；
3. **Imported with diagnostics**：显式转换可映射部分，列出忽略和不兼容项。

不得把“发现了目录”宣称为“兼容整个插件运行时”。

### 16.2 Agent Skills

- 继续原生发现 `SKILL.md` 和当前社区 roots；
- 插件包声明的 Skill 进入 `source=plugin`，保留 plugin id/digest/generation；
- catalog 只读 metadata，正文按需加载；
- plugin disable 后新 Run 不再发现该 Skill，旧 Run snapshot 仍可读取对应 immutable digest；
- Skill 自身不获得代码权限，引用脚本执行仍走工具/worker/用户权限边界。

### 16.3 MCP

- 支持导入可映射的 `.mcp.json`、Claude/Codex `mcpServers`；
- transport、credential、auto-start 和 approval 统一转换为 OURO MCP config/permission；
- plugin MCP server identity 带 owner namespace；
- 不能映射的 transport/字段进入 CompatibilityReport；
- 导入不会因来源项目的默认行为而自动启动进程。

### 16.4 Claude/Codex 包

显式 `plugin import` 可以读取 `.claude-plugin/plugin.json`、`.codex-plugin/plugin.json` 和约定目录：

- Skills 可原生挂载；
- MCP 可按上一节转换；
- prompt commands/agents 只有在用户明确选择时转换为 Skill，并标记 lossy；
- Hooks、LSP、monitors、themes、apps 不自动执行或伪装为兼容；
- 输出逐项 `supported`、`converted`、`ignored`、`incompatible` 和原因。

原生 OURO manifest 存在时不自动混入其他 manifest；用户必须显式选择 import source。

### 16.5 Pi/OpenCode

Pi/OpenCode 的 in-process TS API 与 OURO Python/application 契约不同，不做透明加载。可移植路径是：

- 抽取 Skills/MCP；
- 将工具服务暴露为 MCP；
- 使用语言无关 worker protocol 重写 runtime adapter；
- 由兼容工具生成 porting report，而不是执行未知 JS Hook。

### 16.6 CompatibilityReport

所有 validate/import/check 操作返回类型化报告，至少包含 source format/version、detected resources、映射结果、
丢失语义、安全差异、所需权限和建议迁移目标。Host 不解析自由文本来决定能否启用。

## 17. Application 与 Host surface

引入 `PluginService`/`PluginFacade`，提供类型化用例：

- `discover`、`validate`、`import_compatibility`；
- `list`、`show`、`status`、`diagnostics`、`dependencies`；
- `install`、`update`、`rollback`、`remove`；
- `configure`、`authorize`、`revoke`、`trust_code`；
- `enable`、`disable`、`restart`、`check`；
- `gc` 和显式 `purge_data`。

CLI 只调用 facade，可映射为：

```text
ouro plugin validate <source>
ouro plugin install <source> [--ref ...]
ouro plugin show <plugin-id>
ouro plugin authorize <plugin-id>
ouro plugin enable|disable <plugin-id>
ouro plugin update|rollback <plugin-id>
ouro plugin remove <plugin-id> [--purge-data]
ouro plugin check <plugin-id>
```

TUI/Desktop 使用同一 DTO 展示 provenance、digest、权限 diff、配置缺失、依赖、贡献、active generation 和
脱敏诊断。Host 可以把 `<plugin_id>:<command>` 映射为 slash command/action，但插件 API 不包含 Host widget。

当前 `install`、`authorize`、`trust-code`、`configure` 支持 preview；其余变更命令尚未宣称通用 dry-run。
`request-id` 当前仅作为审计关联 id，不提供重复请求去重语义；真正幂等请求需要持久化结果账本后再承诺。

## 18. Diagnostics 与审计

每条 diagnostic 至少包含：

- stable code 和 phase；
- plugin id/version/digest/generation；
- contribution id（如适用）；
- severity、retryable、用户可执行建议；
- 已脱敏 message 和 correlation id。

禁止公开：credential、完整 environment、原始 exception payload、带 secret query 的 URL、任意 worker stdout、
用户文件内容和未裁剪的第三方响应。

需要可审计的关键动作包括 install、source resolve、digest mismatch、grant/revoke、code trust、enable/disable、
activation、update/rollback、data migration、worker restart、forced shutdown 和 purge。审计记录不能成为 Run
canonical transaction 的单点故障。

## 19. 故障与并发

- 每个 plugin id 的 install/update/remove/enable/disable 使用跨进程锁；
- Store rename 和 config/state 写入都使用现有原子文件策略；
- 全局 ContributionCoordinator 只保护短暂快照交换，不在锁内 import、执行插件代码或做磁盘/网络 I/O；
- observer 使用独立有界队列；满队列产生可诊断 drop/coalesce，不阻塞 durable event；
- semantic Hook、tool、command、worker call 均有 event-specific timeout、cancel 和 output limit；
- 后续 worker supervisor 使用有上限 backoff/circuit breaker；当前 worker crash 直接进入 failed，不自动重启；
- 同一插件的并发 update 请求按 expected digest 做 compare-and-swap；过期请求返回 conflict；
- Host 崩溃后根据 persistent desired state 重建，不相信上次的 transient `active` 标记。

## 20. 模块边界

目标实现建议保持以下职责，不要求一次建立空目录：

```text
ouro/plugins/
  manifest.py          # schema only
  models.py            # install/provenance/grant/generation records
  sources/             # local/git/archive materialization adapters
  store.py             # immutable package/env/data references
  authorization.py     # fingerprints and grant evaluation
  contributions.py     # prepared set, owner transaction, lease
  api/                  # stable in-process Plugin SDK adapter
  worker/               # protocol and supervised process adapter
  compatibility/       # Claude/Codex/MCP importers
  runtime.py            # instance/generation lifecycle

ouro/application/
  plugins.py            # PluginService/use cases
  plugin_models.py      # public DTO

ouro/cli/
  plugin_commands.py    # facade-only Host mapping
```

依赖方向：

```text
Host -> application.plugins -> plugin domain/ports
                              -> source/store/runtime adapters
plugin runtime -> stable PluginContext ports -> application services
core <- typed tools/hooks/context records only; never imports plugin package/runtime
```

`LocalApplication` 拥有 PluginRuntimeResource，并与 MCP 一起在 runtime 开始接受 Run 前启动，在 runtime 和
Supervisor shutdown 期间按 owner/依赖顺序关闭。

## 21. 现有实现迁移

1. 保留当前 `PluginLoader.register_contributions` 测试作为安全基线，不立即自动 import。
2. 收缩 `PluginManifest`：移除安全无效的 `source_type` 决策作用，拒绝尚未支持的 placeholder capabilities。
3. 新增 schema v1 `.ouro-plugin/plugin.json`；root `plugin.json` 通过显式 legacy parser 转换。
4. 将 `PluginPermissions(read_write, network)` 迁移为结构化 permission request/grant；legacy 值只转换为保守
   broad request，不自动授权。
   legacy `ai_generated` 只保留为 creation metadata，按 unverified code 处理，不参与排序或放宽权限。
5. 为 Tool/Hook/command/compaction registry 增加 owner 和原子 replace/unregister；在 application 层增加
   `ContributionCoordinator`。
6. 用 `NativePluginAdapter` 将现有 `PluginContributions` 接入 `prepare -> validate -> commit`，删除逐类隐式
   部分发布。
7. `LocalApplication` 增加 plugin owner，但默认没有授权的第三方插件时行为与当前完全一致。
8. 增加 run-level `ExtensionCatalogSnapshot` 和 durable contribution identity，再开放 update/rollback。
9. 完成 in-process 生命周期后实现 worker protocol；两者共享 manifest、授权、contribution 和 DTO。
10. 最后接入 community importer、依赖 resolver 和 catalog source；这些不能阻塞本地目录/Git 的基础闭环。

迁移期间不得把 legacy manifest 的 `source_type=official` 转为 trusted provenance，也不得因当前测试使用该字段
而保留自报信任行为。

## 22. 分阶段路线图

### M1：包与审计，不执行代码

- schema v1、legacy parser、path/digest/compat validation；
- local/Git/archive source、immutable store、provenance；
- list/show/validate/install/remove、DTO 和 CLI；
- 未授权安装始终 disabled；
- 安装中断、并发和 orphan GC 测试。

### M2：授权与 owned contributions

- structured permissions、code trust、配置/credential refs；
- owner-aware registries、ContributionCoordinator、原子撤销；
- manifest/contribution 对账和 namespace 冲突；
- capability 扩大和 digest 变化重新授权测试。

### M3：In-process lifecycle

- stable SDK、NativePluginAdapter、PluginContext；
- instantiate/prepare/start/stop、LocalApplication ownership；
- background task/connection cleanup、failure isolation；
- enable/disable/restart/check 完整闭环。

### M4：Durable generation 与更新

- Run extension snapshot、generation lease、pending tool identity；
- side-by-side update、drain、rollback、data/config migration；
- crash recovery 和 exact digest 缺失行为。

### M5：Worker 与跨语言兼容

- worker protocol、独立环境、supervision、cancel/restart；
- Agent Skills/MCP 和 Claude/Codex import report；
- Pi/OpenCode porting report；
- worker 与 in-process 的行为 contract suite。

### M6：依赖与 Catalog

- plugin dependency resolver/lock graph；
- catalog source、publisher/signature policy；
- orphan dependency/cache GC；
- 组织 policy 只在出现真实多设备/分发需求时进入。

## 23. 验收矩阵

完整插件系统至少由以下证据证明：

### Package 与供应链

- manifest schema、unknown field、SemVer/API compatibility；
- path traversal、absolute/UNC、symlink/reparse、archive bomb、文件/总大小限制；
- 相同 version 不同 digest、Git ref 固定、来源脱敏；
- install/update 中断和 record 写失败，旧版本仍可用；
- 无安装/构建脚本执行。

### Trust 与权限

- 未授权前通过 import side effect 证明 entrypoint 从未执行；
- manifest 自报 official/verified 无效；
- permission/capability/runtime/source 扩大使授权失效；
- third-party tool 不能自动执行，Hook 不能提升权限；
- credential/config/exception/worker stderr 全链路脱敏。

### Contribution 与生命周期

- 任一必需 contribution 冲突时零发布；
- disable/revoke/remove 撤销 owner 的全部 contribution；
- plugin A 失败不改变 plugin B/builtin；
- timeout、cancel、start failure、partial startup、forced shutdown 无 task/process/connection 泄漏；
- stop/unregister/lease close 重复执行安全。

### Durable Run

- update 期间已有 Run 保持旧 generation，新 Run 使用新 generation；
- pending approval 恢复只调用 exact digest + contract hash；
- exact generation 缺失时进入稳定 recovery error，不调用同名新工具；
- compaction/context/Hook snapshot 不在单 Run 内跨版本混用。

### Compatibility

- Agent Skill round trip；
- Claude/Codex MCP 可映射子集；
- 不支持 Hook/LSP/UI 有明确 CompatibilityReport；
- native/worker 对同一 tool/hook contract 通过共享测试；
- API minor negotiation 和 major incompatibility 行为稳定。

### Host 与架构

- CLI/TUI/Desktop 只依赖 facade/DTO；
- `core` 不依赖 manifest/store/import/worker；
- stdout JSON 模式无插件日志污染；
- 全量 pytest、ruff、mypy、compileall、coverage 和 dependency tests 通过。

## 24. 当前实现边界与设计调整

截至 2026-07-16：M1、M2、M3 已完成；M4 完成 generation snapshot、lease、精确恢复校验和失败回滚，
不含 data migration；M5 完成基础 worker、Skills/MCP 和 compatibility report，不含独立环境、自动重启、
熔断、反向 Host service 与通用 feature negotiation；M6 只完成已安装 digest 的依赖图，不含 catalog、签名、
publisher policy 和自动第三方依赖求解。

下列能力明确不属于当前已实现面：通用 mutation dry-run、request-id 幂等账本、data migration、worker 独立
依赖环境与 restart supervisor、OS sandbox、worker 反向 Host service、catalog/marketplace、签名验证和
publisher trust root。它们只有在真实需求和完整不变量可以一起交付时才进入实现，不预建空抽象。

实现保留第 3 节不变量，但基于 OURO 当前共享 registry/context 的真实底层，对原方案做以下收口：

1. **目录变更采用全局 generation barrier。** enable、disable、upgrade、rollback、配置替换和撤权会先阻止
   新 Run 获取 extension snapshot，并等待现有 snapshot 释放后再更换全局 registry。当前不在同一进程中让
   两套 in-process generation 面向不同 Run 并行服务；这避免 Python module cache 和全局工具目录产生混用。
2. **安全撤权优先。** revoke 在等待旧 lease drain 前先撤销 owner contribution，使旧 Run 的后续调用失败，
   而不是继续使用已撤销能力。普通 disable/upgrade 仍执行有序 drain。
3. **恢复只接受精确 generation。** snapshot 持久化 plugin id/version/digest/generation/contract hash；进程重启
   后若不能重建原 generation，恢复显式失败。首版不把“相同 digest 的新进程实例”冒充原 generation。
4. **依赖解析不隐式下载。** resolver 只使用已安装且显式选中的 digest，检查 SemVer、冲突和环，并按拓扑
   顺序启停。缺少依赖返回 incompatible；安装器不访问 registry 或执行第三方依赖安装脚本。
5. **配置按 digest 隔离。** 候选版本的 schema/config 可在切换前独立验证，不能覆盖当前 generation 的配置；
   凭据状态只保存 `env:` 引用，SDK/worker/MCP 在授权后的激活阶段解析值。
6. **MCP 动态发现冻结到 generation。** auto-start MCP 在隔离 ToolRegistry 中完成连接和工具发现，再把该次
   发现结果作为 owner contribution 原子发布；服务端后续 tools-list 变化只在下一 generation 生效。
7. **社区兼容先报告、后迁移。** `import-report` 识别 Agent Skills、MCP 与不可移植 Hook/LSP/theme/app，输出
   类型化 supported/converted/ignored/incompatible 结果。首版不自动生成或执行转换后的社区代码。
8. **不建立虚假的供应链能力。** 当前来源为 local、archive 和 Git；catalog、签名验证、publisher trust root、
   自动环境创建和第三方依赖求解不属于已实现面。当前 provenance 不暴露无生产者的 publisher/signature 字段，
   code trust 只绑定精确 digest；真实 source verifier 与状态迁移一起落地后再扩展 schema。
9. **worker 是协议隔离，不是沙箱。** worker 使用受限环境、JSON-RPC 消息上限、超时/cancel、stderr 脱敏与
   幂等 shutdown；没有 OS sandbox 时不声称可以安全运行恶意代码。
10. **部署 home 是首版 scope 边界。** state 中保留 user/workspace/managed scope 与授权绑定，但首版不在单一
    进程内叠加多个 home 的同 id overlay。需要并行不同 digest 时使用独立部署 home 或 worker 进程。
11. **选择 digest 是依赖锁定事务。** 已启用插件切换 digest 前原子校验完整 desired dependency graph；存在
    active dependent 时拒绝禁用依赖项。更新仍只做 side-by-side 安装，授权、配置和选择候选版本是显式步骤。
12. **共享数据与 owner 生命周期分离。** 同一 plugin id 的不同 digest 共用私有数据目录，因此只有移除最后一个
    安装 digest 时才能 purge。Host 关闭先让 Runtime 释放 generation snapshot，再按 owner 逆序 drain 插件资源；
    超过关闭 deadline 时记录 forced-shutdown 诊断并继续幂等清理。
13. **durable generation 引用优先于管理便利。** 重复 `enable` 只确认 desired state，不隐式重启。queued、running、
    waiting、paused 或 recoverable Run 仍引用当前 generation 时，普通 disable、restart、digest selection 和活动配置
    替换被拒绝；安全撤权仍可优先撤销能力并让旧 Run 显式失败。
14. **秘密按解析出的真实值全链路脱敏。** manifest slot 同时约束 SDK、worker 和 MCP；in-process logger 的格式化
    参数、structured diagnostic、启动异常和后台 task 异常，以及 worker stderr/JSON-RPC error 都使用动态精确值
    脱敏，不能只依赖 `token=` 等字符串模式。

对应实现集中在 `ouro/plugins/`、`ouro/sdk/plugins/v1.py`、`ouro/application/plugins.py`、
`ouro/cli/plugin_commands.py` 与 `LocalApplication`；legacy `PluginLoader` 仅作为一个兼容周期的无自动 import
桥接层，新原生插件必须走 installer/runtime generation 路径。

# OURO Agent 开发架构

本文是 OURO 当前代码结构的权威说明。新代码必须先确定职责、依赖方向和持久化边界，再选择目录；
目录位置本身不能替代架构判断。

各领域的详细数据契约、失败语义和验收证据见 [规格索引](../dev/specs/README.md)。

## 分层与依赖方向

```text
CLI / TUI / Desktop hosts
        |
        v
application orchestration
   |        |             |
   v        v             v
 core    config    session/resource/tool ports
   ^                        ^
   |                        |
provider adapters    file / SQLite / local implementations
```

- `ouro.core` 定义稳定领域记录、Provider/Tool 契约、上下文构建、取消信号和单次 active run 的状态机。
  它只能依赖 `ouro.core`。
- `ouro.application` 编排用例、状态转换和持久化事务。它可以依赖 core、配置契约和基础设施端口，
  不能依赖 CLI 或具体 provider 实现。
- `ouro.local_application` 是本地部署组合根，统一拥有 Runtime、MCP 和关闭生命周期；`ouro.cli` 只是界面
  适配器，负责解析、调用 application service 和格式化输出。
- `ouro.providers`、`ouro.sessions`、`ouro.tools` 是外部能力的具体适配与实现。
- `ouro.config`、`ouro.skills`、`ouro.plugins`、`ouro.mcp` 维护各自契约与策略，不反向依赖
  application 或 CLI。

架构测试位于 `tests/architecture/test_boundaries.py`，用于阻止低层反向 import。

## 包职责

| 包 | 负责 | 不负责 |
|---|---|---|
| `core` | 领域记录、协议、纯规则、inner loop | 文件、网络、CLI、具体厂商 SDK |
| `application` | 会话与运行用例、事务编排、Host DTO | 参数解析、终端渲染、HTTP 细节 |
| `config` | 集中配置模型、校验、持久化和运行参数合并 | 模型请求和 UI 查询用例 |
| `sessions` | 后端中立 SessionRepository/Catalog port、文件与 SQLite 后端、Session 资源引用 | 会话命令与 Host DTO |
| `resources` | ResourceStore port、不可变 blob 与 blob 元数据 | Session 引用、Host DTO 与运行调度 |
| `providers` | Provider 实现、transport、codec、adapter factory | 会话模型选择和运行状态 |
| `tools` | 工具注册、schema 校验、权限执行和批调度 | 模型轮次状态机 |
| `skills` | 标准 Skills 发现、解析、内容加载和安装管理 | 提示词运行编排 |
| `plugins` / `mcp` | 插件注册；MCP transport、协议适配、工具投影和连接生命周期 | 核心业务状态 |
| `cli` | 命令定义、组合、输入输出 | 领域规则和持久化事务 |

## 核心运行链

```text
CLI / TUI / Desktop Host
  -> LocalApplication async lifecycle
    -> MCPRuntimeResource.from_config(home, runtime.tools)
      -> start auto-start servers + atomic ToolRegistry projection
    -> RuntimeService
    -> session lock + recovery
    -> RunLifecycle.start
    -> InnerAgentLoop
       -> ContextPreparationService
       -> RuntimeAdapter -> Provider
       -> ToolCallCoordinator -> ToolBatchExecutor
       -> RunLifecycle complete / fail / pause
    -> queued input drain
```

默认 CLI 的单次 Run、Interactive 和审批恢复均通过 `LocalApplication` 启动并关闭 Runtime、插件与 MCP；Runtime
自身又关闭模型 adapter、事件 worker 和专用压缩 adapter。TUI/Desktop 必须复用同一 async lifecycle，
不得从 Session adapter 路径推导部署配置，也不得只构建 RuntimeService 后绕过扩展资源装配。

`RuntimeService` 拥有跨协作者的运行事务：session lock、abort signal、恢复、人工交互恢复和排队输入
串行化。`InnerAgentLoop` 只拥有一个 active run 内的模型轮次状态机；它通过 `InnerLoopPorts` 显式访问
上下文、事件、工具和持久化副作用。`RunLifecycle` 集中承载模型循环中的 run/session 状态转换。

运行事务读取 `RuntimeSessionSnapshot`，其中只包含 Session、messages、runs 和 compactions；包含完整
events 的 `SessionSnapshot` 只服务查询、回放、导出和活动投影重建。正常 Run 在取得锁前后复用同一份运行态
快照，并用轻量 SessionRecord 重读检测竞争更新；上下文准备和 catalog 刷新都不得反向加载 replay events。

`RuntimeSupervisor` 位于 RuntimeService 之外，只拥有跨 Session 的进程内 FIFO 调度、并发上限、Host
订阅和 shutdown 生命周期。它不写 durable Run 状态机；submit/abort 最终调用 RuntimeService，重启恢复
仍从 SessionStore 读取。`RuntimeActivitySnapshot` 把 durable Run/ToolBatch/ToolCall 与模型、压缩事件投影为
Host 可直接消费的细粒度活动状态；它不是第二套状态机，并可从 Session 事件重建。订阅可选择接收活动变化，
且在创建时取得同一边界的 initial activity。每个订阅使用有界队列，慢 Host 不反向阻塞 Run；发生 lag 后
Host 通过 activity 快照和 durable event cursor 重新同步。

`RuntimeEventRecorder` 先在线程外持久化事件，再把 Host sink 与观察型 Hook 投递到彼此独立的有界 worker；
慢观察者不会持有跨 Session 全局锁，也不会回滚 durable event。每个 sink/hook 都有超时和脱敏诊断，关闭
生命周期会 drain 已接收事件。Host 收到的是类型化 `RuntimeEventView`，不需要重复解释任意 payload。

排队输入的提交时机是应用层契约：`follow_up` 在当前 Run 完整结束后进入新的 Run，`steer` 在当前
model turn 完整提交后进入同一 Run 的下一次模型请求。工具请求与结果是不可拆分的 turn 边界，因此
steer 不会插入二者之间。消费批量由 `queued_inputs.consumption_mode` 配置决定；`one_by_one` 每个边界
只取一条，`all_at_once` 在同一边界取出全部符合时机的消息，二者都保持符合条件消息的入队顺序。审批或
用户输入请求恢复完成后，application 必须先释放原 Run 的 session run lock，才能为 follow-up 获取新锁并启动
后续 Run；run lock 保持非重入，不能用重入掩盖事务边界错误。

队列来源是类型化的 `user | host | plugin`：用户输入写为 `role=user`；OURO Host/Application 输入必须携带稳定
`host_source_id`；插件输入必须携带完整 Generation/contract attribution。后两者都写为带来源元数据的
`role=system_synthetic`，不能携带用户资源引用，也不能在消费时退化为用户消息。`RuntimeService.submit_host_input`
与 `submit_plugin_input` 共享同一入队、锁、幂等消费和 canonical persistence 边界。

工具调用以一次完整模型响应为批次边界，但 Provider 不截断流式观测。流式 Provider 把供应商 chunk 逐个
规范化为 `tool_call_delta`；其中 `index` 是响应内从零开始的工具调用序号，不得泄漏供应商原生 content-block
坐标。`InnerAgentLoop` 同步产生 `model_tool_call_delta` 事件，并在响应 `done` 边界
聚合、解析和校验整批 tool calls。无法提供分片的非流式 Provider 可以产生完整 `tool_call` 事件；`done`
只表达响应结束和 usage。随后按配置选择顺序或并行执行。assistant tool-call 消息必须先持久化，tool result
按 provider 原始顺序提交。

会话批准模式保存在 canonical `SessionRecord` 中，取值为 `inherit`、`auto`、`approval` 或 `deny`。工具定义
通过 `permission_control` 明确最终权限的控制权，避免用两个可产生非法组合的布尔开关表达同一概念：

- `host` 是默认值。工具定义权限先由用户 `config permissions` 覆盖，再由 Core 的单一策略解析边界计算最终
  权限：`inherit` 沿用配置权限，`auto` 放行，`approval` 要求审批，`deny` 拒绝；限制性插件
  `tool_policies` 最后只能提高限制。
- `tool` 表示工具定义拥有最终控制权，只允许 `permission_mode="auto"` 或 `"deny"`。用户覆盖、会话批准模式和
  Host permission override 不参与决策；限制性 `tool_policies` 仍对这类工具评估。它只免受 Host 审批模式干预，
  不能借此逃逸其他编排扩展的工具策略。

`ToolCallCoordinator` 在预检时把会话模式交给 `ToolExecutor`，最终权限和控制权写入 durable
`ToolInvocation`，因此恢复不依赖当前进程的配置快照。内置、插件和 MCP 工具都通过这一边界。新增会话策略
只需扩展 Core 策略类型和解析函数，执行与入口层不承载策略分支。每个 Tool contract 还显式携带
`effect = read_only | state_changing | unknown` 和 `batch_behavior = compatible | exclusive`。builtin 与插件
在定义处声明 effect；MCP 依次采用用户 override、协议 `readOnlyHint` 或 `unknown`，不按名称和描述猜测。
归一化后的 effect、batch behavior 和 resumable 能力都进入 execution contract hash。工具策略只接收 canonical
identity、owner、这些归一化语义、schema 校验后的参数及 Session/Run 上下文；包含 exclusive 工具的混合批次
必须在任何 handler 执行前整批拒绝。
工具 handler 可以返回
`ToolInputRequest`，以独立于审批的用户输入协议挂起 Run；其中的请求和回答都是 Core 不解释的 JSON 值。
需要根据回答继续执行的工具必须声明 resumable 并提供 resume handler；恢复仍绑定原 Catalog 与精确工具 contract，
允许再次请求输入或产生最终工具结果。非 resumable 工具保留直接把回答作为结果的单阶段协议。`AskUser` 只是该
协议的内置生产者，不被会话批准模式转换成普通工具批准。`ToolExecutionContext` 集中限制内置文件路径和 Bash cwd
只能落在 workspace 或 Host 管理的 `OURO_HOME/artifacts` 制品根目录，并在 executor 边界统一限制结果长度；它不会
放开 `OURO_HOME` 的配置、会话或插件私有数据，Bash cwd 限制也不等同于操作系统沙箱。

## 模型路由与 Provider 边界

持久化模型路由只有一种格式：`provider_id/endpoint_id`。

- `application/models.py` 负责列出、解析和持久化会话模型路由。
- `config` 负责 provider/endpoint 配置模型与运行参数合并。
- `providers` 负责通过构造器注册表把已解析参数构建为具体协议 adapter。
- Provider 保存供应商级连接默认值；Endpoint 保存模型 id、采样参数、能力和可选连接覆盖。

模型路由不接受 model id 或 endpoint id 短名。这样持久化值、CLI 输入和运行时解析使用同一契约，
不会因多个 provider 出现同名模型而产生歧义。
同一份已解析 endpoint 配置复用 adapter 与 HTTP client；配置指纹变化时重建 adapter，Supervisor shutdown
统一关闭其拥有的连接。调用方注入的 HTTP client 不由框架关闭。
当前协议类型包括 Faux、OpenAI Chat Completions 和 Anthropic Messages。配置域的协议档案集中声明默认 API
path 与连接要求，产品档案声明允许/默认协议；厂商模板只组合产品类型、协议和 Endpoint 数据，不进入 adapter
factory 分支。OpenAI 与 Anthropic 协议实现共享 provider-neutral HTTP JSON/SSE transport 和重试执行器：只在
尚未发出模型流事件时重试明确分类为 transient 的失败，开始输出后禁止重放。结构化 `response_format` 受
Endpoint capability gate 约束，重试计划通过 durable runtime event 暴露给 Host。

模型思考使用四段式边界：core 的 `ReasoningRequest` 表达统一意图，Endpoint 的 `ReasoningCapability` 表达
真实能力，config 的 `ReasoningProfileConfig` 声明解析后 variant 对应的原生请求片段，provider encoder 只负责编码。
解析严格拒绝不支持的开关和 effort，不在厂商等级之间近似。可展示 reasoning 与工具续接状态分开建模；
续接状态使用路由绑定的 `ProviderContinuationArtifact`，OpenAI-compatible 只回放声明过的
`reasoning_content` 文本，Anthropic 只在工具续接时原样回放同 provider/protocol/model 的签名 thinking blocks。

## 消息、工具与上下文

`MessageRecord` 使用类型化字段表达工具关系：

- assistant 工具请求写入 `tool_calls`；
- tool result 关联写入 `tool_result`；
- 可展示思考写入 `reasoning_content`，协议私有续接状态写入 `provider_continuation`；
- `metadata` 只保存非结构化附加信息，不保存工具请求、关联身份或思考续接状态。

一个 assistant 工具请求及其结果共享 `turn_id`。压缩以该模型回合为原子帧，不能把请求和结果拆开。
没有模型回合身份的普通消息按用户请求分组；不完整工具交换及其后续恢复上下文作为受保护后缀。

`ContextPreparationService` 负责把 canonical messages 投影为模型上下文，应用 Skills 指令、压缩摘要、
snapshot-bound Context Provider 和 token 预算。Context Provider 在模型请求构建前返回带 owner 的类型化 fragment；
fragment 默认只参与本次模型请求；声明 `persistence="message"` 时，Application 必须先通过 `RunLifecycle` 将它以
`system_synthetic` 写入 canonical history，并记录 plugin、provider、generation、model round 和幂等 delivery key，
成功后才能调用模型。同一 Run/model round 的重试不能重复落盘。optional provider 失败时省略自身，required
provider 失败时阻断本轮，不能借用观察 Hook 修改语义。canonical history 永不被压缩覆盖；`CompactionRecord`
通过 message id 前缀和 digest 验证摘要来源。

模型产生无工具的候选完成后，`InnerAgentLoop` 在完成 Run 前评估同一 Catalog 的 Turn Policy。策略只能返回
`allow`，或返回带 feedback 的 `continue`；后者把候选回复和带 provenance 的 synthetic feedback 持久化到同一
Run，再进入下一模型轮次。策略异常、超时或非法输出 fail-closed 并暂停 Run。跨 Run 的 Continuation Policy 与此
保持分离：前者约束当前 Run 的轮次收束，后者只消费终态 `RunOutcome`。

用户附件采用同一投影原则：canonical message 只保存 `resource_refs`。ResourceService 在 Run/Message 提交前
校验 Session 归属、digest、size、媒体类型和 Endpoint 能力，再把受支持文本或图片仅注入本次模型请求；附件
内容参与上下文预算与压缩，但不回写历史。

`MessageRecord` 到统一 Provider 的投影以 `ModelMessage` 为硬边界。后者只包含 role、模型可见 content、工具调用/
结果关联和路由绑定的 continuation，不携带 message/session/run identity、metadata、resource references 或仅供 Host
展示的 reasoning。这里的“真实 Provider 上下文”特指 `RuntimeAdapter` 交给统一 `Provider.stream` 的
`system_prompt`、`messages` 和 `tools` 三项，不是任何厂商 HTTP endpoint 的最终 payload；厂商 codec 在该边界之后
继续负责协议编码。

每次上下文构建只计算一次类型化 `ContextMetrics`：发送前的 input token 估算、固定部分、模型窗口、预留输出、
可用 input budget、剩余量和占用率。估算用于预算与实时状态；Provider `done` 后单独补记供应商报告的 input token，
两者不互相覆盖。内置 estimator 对文本、工具 schema 和 continuation 计数；图片使用有界保守值，不把 data URI 的
base64 字节错误当作文本 token。

请求审计采用 write-through latest snapshot。上下文准备完成后，在模型请求前原子替换 Session 的最新
`ContextSnapshotRecord`；记录保存 ephemeral 注入、压缩/恢复后的 source messages、工具、指标、路由和实际
Provider 内容 digest。资源 blob 不复制进快照，审计内容查询通过不可变 resource reference 还原后校验 digest。
`SessionService.context_status/context` 只读取这一最近请求事实，供运行活动、Provider usage 和诊断使用。

面向用户的当前上下文查询属于另一应用用例。`RuntimeService.current_context` 使用与请求准备、压缩完成相同的纯投影
边界，从 canonical messages、active compaction、当前模型、Catalog 工具、Skills 和资源构造 `ContextView`；
`/context` 与 `session context` 读取 metrics，full/content 读取同一视图的统一 `ProviderContext`。查询不执行
Context Provider、Hook 或自动压缩，也不写入 Session。请求级 ephemeral 注入只属于具体 Run，不能伪装成空闲
Session 的当前事实。该查询需要显式 O(context bytes) 投影，以保证手动压缩后不会继续展示旧请求快照。

## Provider Usage 与全局费用账本

Provider adapter 只在 `done` 边界产生类型化 `Usage`，数据源必须是供应商响应。`core/context.py` 的 token
estimator 属于上下文预算域，只负责溢出判断和压缩触发；它与费用账本没有回退、同步或推导关系。

`UsageService` 把普通模型回合和压缩模型请求统一写入 `UsageLedger`。每条有效记录绑定 provider、endpoint、
model、Session、Run、用途、请求身份和接收时间；Run 通过 `usage_entry_ids` 保持幂等聚合。压缩在收到
Provider `done` 时立即记账，因此即使后续摘要格式校验失败或路由回退，已经发生的供应商调用也不会漏记。

本地实现使用 `OURO_HOME/billing/usage.jsonl` 追加事件和独立跨进程写锁。`record` 保存供应商事实，
`replace` 保存带原因的人工修订，`reset` 在当时的有效视图上选择性失效匹配记录；查询重放事件得到当前
有效视图，历史审计事件始终保留。入口层只解析过滤条件和格式化输出，日期/路由过滤、汇总与修订规则位于
应用层，文件和锁细节留在基础设施层。

## 本地持久化

`SessionRepository` 是 canonical 后端契约，`SessionCatalogStore` 是可丢弃查询投影契约；二者都不暴露
Path、初始化、文件索引或 ORM。默认 `LocalSessionStore` 在 `OURO_HOME` 下保存可读文件：

```text
OURO_HOME/
  layout.json
  config/config.json
  skills/
  locks/sessions/
  locks/session-mutations/
  locks/billing-mutations.lock
  billing/usage.jsonl
  artifacts/
    <plugin-id>/
  sessions/
    index.json
    <session_id>/
      session.json
      messages.jsonl
      events.jsonl
      compactions.jsonl
      context.json
      runs/
      resources/index.json
  resources/
    records/<resource_id>.json
    blobs/sha256/<digest>
    index.json
    staging/
  trash/sessions/
```

`SQLiteSessionStore` 是同一契约的第二实现，默认数据库位置为 `OURO_HOME/sessions.sqlite3`。它使用标准
`sqlite3`、WAL、外键和显式事务，事件 after-sequence 查询由索引支持；表结构只存在于 adapter 内。
`LocalApplication.build(..., session_backend="sqlite")` 可选择该实现。Host 的选择规则集中在
`ouro.deployment.resolve_session_backend`：显式参数优先于 `OURO_SESSION_BACKEND`，默认使用 `files`。CLI 的
交互、run、session、resource 与审批入口都接受 `--session-backend files|sqlite`，命令层只透传选择值，不包含
后端分支或存储实现。该部署参数不写入 `config.json`，避免在尚未选定存储时反向依赖存储内配置。

选择后端不等于迁移数据；文件和 SQLite 数据集保持隔离。`import_sessions_to_sqlite` 在一个目标事务中复制、
逐 Session 验证并在失败时整体回滚；文件后端继续作为可读实现和导出来源。

`session.json`、messages、events、runs 和 compactions 是 canonical history；`context.json` 是可替换的最新
Provider 请求快照，SQLite 使用对应的 `latest_contexts` 单行记录；`sessions/index.json` 是可重建
查询投影。索引失败不得破坏 canonical 写入，失败通过 dirty marker 和 diagnostic 暴露。
`billing/usage.jsonl` 是独立的全局 canonical 账本，不随 Session 归档或进入回收站而删除。
messages/events JSONL 同时提供稳定 byte cursor 分页端口；增量读取不依赖完整 `SessionRecord` 装载，append 后
已有 cursor 仍可继续使用。

Provider text/reasoning delta 只追加到文件后端的 `events.jsonl`，由下一个非 delta 事件执行组提交；状态边界
事件仍同步落盘。快照只在消息提交和生命周期状态转换时写入。SQLite 在同一语义契约下使用数据库事务。
所有 Store 读取均无副作用；`SessionRecoveryService` 是显式 application command，从 durable messages、run
状态和最后模型轮次事件重建未完成输出，不重放结果未知的外部副作用。

`layout.json` 的 `data_layout_version` 标识整套本地数据契约。遇到不兼容版本时初始化必须抛出
`MigrationRequiredError`，不能在加载过程中猜测字段或隐式改写数据。

每个 session 同时只允许一个 active run。`RunRecord` 是执行状态事实，`SessionRecord.status` 与
`active_run_id` 是可恢复的查询投影；显式恢复命令会校准悬空引用、跨存储写入中断和不可继续的人机等待状态。
长生命周期 session OS lock 表示 active run 的进程所有权；独立的短生命周期 mutation lock 只覆盖
单次 canonical 读改写、JSONL 追加和事件序号分配，两者不能互相替代。锁文件仅用于诊断。

全局 resource record/blob 是 ResourceStore 的 canonical 数据；Session 到 resource 的引用只由
SessionRepository 拥有，文件实现保存 Session 子记录，SQLite 实现保存引用表。Resource adapter 不读取或
推断 Session adapter 的目录/表结构。`resources/index.json` 只是 blob 元数据的可重建投影。
资源导入先 staging 并计算 SHA-256，再原子发布。分支的消息与资源引用在同一 Session staging 目录发布，
避免先出现带悬空 resource id 的分支。分支读取、资源引用校验和目标发布持有源 Session mutation lock，
因此源会话删除或资源移除必须在线性化点之后执行，垃圾回收不会删除正在复制的最后一份可达内容。

## 配置契约

配置集中保存在 `config/config.json`，由 `ouro.config` 的类型化 manager 读写。各分区只接受当前权威字段：

- 系统提示词：`prompts.system`；
- Provider：`providers.default` 与 `providers.items`；
- Endpoint：每个 provider 的 `endpoints`；
- 工具执行：`tool_execution`；
- 运行预算：`run_budget`；
- 排队输入：`queued_inputs`；
- 压缩：`compaction`；
- Skills：`skills`。
- MCP server：`mcp.servers`。

Provider 配置同时持久化实例 `provider_id`、产品配置 `provider_type` 和线协议 `protocol`。产品档案负责约束
允许/默认协议，adapter factory 只按 `protocol` 分派。无效结构必须抛出 `ConfigError`，不能静默降级、推导
缺失协议或猜测另一种格式。凭据通过 `CredentialRef` 进入 provider factory，业务代码不直接读取环境变量。
Endpoint 的 `reasoning.capability/profile` 是同一权威配置的一部分；profile 与 `request_options` 的字段所有权
冲突、协议无法保存的 continuation 以及运行时不支持的 effort 均在 HTTP 前失败。
CLI 与未来 Host 通过 `ConfigurationFacade` 的类型化 DTO/query/command 访问这些分区，包括 MCP；Facade
只暴露凭据 configured/missing 状态，并报告配置变更的生效范围。所有 JSON 配置 command 在同一跨进程
文件锁内完成 read-validate-write，避免 CLI/TUI/Desktop 并发修改不同 section 时丢失更新。

## CLI 契约

CLI 命令使用资源层级表达职责：

- `provider add/update/replace` 分别表达 Provider 创建、同类型更新和完整替换；
- `provider endpoint set` 只管理 endpoint；
- `provider endpoint set --reasoning-preset/--reasoning-config` 为内置或自定义模型声明思考契约；
- `provider templates` 查询类型化的只读厂商模板目录；
- `provider add <id> --type <type>` 统一创建 Provider；内置厂商类型原子装载模板参数与 Endpoint，compatible
  类型创建空 Provider 并由 endpoint 命令补充模型；同 id 冲突提示显式使用 `update` 或 `replace`；
- 内置厂商的 `provider update` 刷新模板并保留用户扩展，`provider replace` 按新类型完整替换；
- 内部 Faux Provider 不进入用户侧目录、默认展示或连接检查；
- `provider check` 负责连接检查；
- `mcp list/show/set/remove` 管理 MCP server 配置，`mcp check` 执行一次临时连接检查；
- `session compact` 负责手动压缩；
- `session context` 投影当前上下文长度，`session context --content` 显式读取同一当前统一 Provider 内容；
- `session messages/events` 通过 cursor 和 limit 增量读取历史；
- `resource import/list/show/export/remove` 管理 Session 资源；
- `run --resource` 将已导入资源附加到本次输入；
- `skills remove` 负责移除 OURO 管理的 Skill。

命令入口不得实现运行规则。跨 Host 可复用的查询和命令进入完整、内聚的 application service
（SessionService、ResourceService、UsageService、RuntimeService、ConfigurationFacade）；不再提供运行期才
暴露“缺少某项能力”的大转发 façade。MCP CLI 只适配参数和格式化输出；原子更新、transport 字段重置、
完整校验和脱敏公共视图都由 `ConfigurationFacade` 编排。
Interactive 的动态终端效果同样只属于 CLI adapter：`TerminalStatusLine` 管理 MCP/压缩等单一状态行，
`TerminalStatusRegion` 管理按 tool call identity 固定的工具行，`InteractiveOutput` 把既有 Run 事件投影为
`mcp_connection`、`compaction`、`tool_input`、`tool_execution` 语义。Core/application 不感知 spinner 帧；
真实 TTY 对工具参数生成、执行与结果进行原位重绘，非 TTY 则让每个工具只输出最终稳定行。工具参数 delta 保持
durable 事件与完整累积；参数摘要仅在等待审批时进入工具行和审批提示，批准、拒绝或开始执行后隐藏，避免把
不可操作的信息持续留在终端中。

循环终端在提交用户输入前启动本地单调时钟；`run_started` 表示请求与上下文准备阶段，
`model_request_started` 表示等待 Provider 阶段，首个 reasoning/text/tool-call delta 结束等待。状态行动态显示
当前阶段秒数和本轮上下文估算，并在响应开始时稳定输出总等待及分阶段耗时；自动压缩作为独立阶段计时，手动压缩不伪装成
模型等待。

## 扩展边界

- Hook 失败由 `RuntimeEventRecorder` 隔离并转为脱敏事件，不允许破坏 durable run；semantic 与
  observational hook 使用不同超时上限。
- 插件只能通过 owner-aware candidate Catalog 贡献工具、限制性 tool policy、观察 Hook、Host-neutral command、
  上下文 provider、turn policy、压缩策略、Skills、MCP、Run continuation policy 或受监督后台服务。manifest 的
  `official` 自报不构成信任；有效 provenance 由安装来源传入，capability/permission 必须覆盖实际贡献，
  未授权插件保持 `needs_authorization`。用户信任并授权插件后，其工具权限与内置工具使用相同语义，不按来源
  降级；manifest 工具权限是授权契约，运行时 SDK 贡献不得偏离，相关字段变化会改变 contribution fingerprint。
- 插件命令使用 `<plugin_id>:<name>` 稳定 identity 和类型化 request/result；CLI/TUI 可映射为 slash command，
  Desktop 可映射为 action，但 core 不定义 UI extension DSL。当前 Interactive 中 builtin 优先，canonical
  `/<plugin_id>:<name>` 始终可用，唯一 local name 才获得 `/<name>` 短别名。
- 插件不得取得 `RuntimeService` 或 `SessionStore`。获得 `sessions.write` 与 `background.run` 后，command 可以
  返回类型化 `SessionInput`，continuation policy 可以在终态 Agent Run 后返回
  `continue/complete/pause/fail/ignore`。`RunContinuationService` 负责冻结 generation、冲突处理、幂等入队和
  durable checkpoint；普通 `after_run` Hook 仍是 fail-open observer。
- 插件工具可选接收 Host-owned、request-scoped `ToolExecutionContext`，复用 workspace/Host artifact 路径边界与
  协作式取消语义；
  worker 得到等价 workspace 字段并通过协议 `cancel` 中止调用。`tool_policies` 与观察型 `before_tool_call`
  分离，对 host-controlled 和 tool-controlled 工具统一评估，只能把最终权限提升为审批或拒绝，失败时拒绝，且
  审批修改后的最终参数必须重新评估。`permission_control="tool"` 只隔离 Host permission 维度，不隔离插件策略维度。
- 插件输入通过 canonical queue 进入下一 Run，保存为带 plugin/contribution/generation/decision provenance 的
  `system_synthetic`，绝不伪装为 `role=user`。旧 Run 的续跑决策若遇到用户已排队输入或更新 Run，会被标记为
  superseded；目标插件在下一 Run 重新评估。
- 插件是 OURO 系统级扩展模块，来源只分 `builtin` 与 `user`。workspace/project 只作为一次请求的执行上下文，
  不提供项目插件、scope overlay 或按项目安装的插件运行时；plugin state、grant、provenance、CLI 与 DTO 均不再
  携带 activation scope。
- Builtin 由 application composition root 的显式 definition registry 装配，不扫描磁盘、不写入 user immutable
  store，也不伪造 install/provenance。definition registry 是 builtin identity、默认启停、依赖 contract 和公开名称
  保留的唯一事实来源；user installer、dependency resolver、application facade 与 runtime 都消费该 registry 的派生视图。
- Builtin 与 user 的差异止于供应、授权和管理操作。启用后两者进入同一个 candidate reconciliation，拥有正常
  Generation、gate、owner、Catalog projection 和 durable execution contract；可关闭 builtin 不能放入 Host baseline，
  也不新增 `builtin` runtime kind 或第二套插件 runtime。
- `PluginPackageStore.state.json` 保留 user install/activation，并另存稀疏 builtin enablement override；builtin code 随
  OURO distribution 发布，私有 data 继续使用 `OURO_HOME/plugins/data/<plugin-id>/`。没有 override 时使用 definition 的
  product default，显式用户选择跨重启保留。需要交给模型内置文件工具编辑的用户制品使用独立的
  `OURO_HOME/artifacts/<plugin-id>/`，不混入插件私有 data 或 canonical Session 存储。
- 插件系统将 package、install/provenance、authorization 和 runtime generation 分层。当前代码已经形成首版
  管理与运行闭环；[插件系统规格](../dev/specs/plugin-system.md)描述权威目标与剩余契约，现状差距以
  [插件系统架构审查](OURO_插件系统扩展性架构与Pi对照_2026-07-17.md)为准。同进程插件为 full trust，worker
  提供进程与协议边界，但不宣称 OS 沙箱。
- `PluginPackageStore` 分离 immutable code、mutable state 与 plugin data；installer 从真实 local/Git/archive
  来源生成 provenance。安装阶段只校验包、路径、schema、MCP 描述符和兼容范围，不 import 或启动插件代码；
  激活前重新验证存储树和 digest，避免被修改的“不可变”包进入执行边界。
- plugin state 内容不满足当前 schema/编码契约时，启动读取在 mutation lock 内隔离原文件并以空 Catalog 降级；
  文件系统 I/O 错误保持为类型化 persistence failure，不能触发隔离。`LocalApplication.startup_report` 将插件与 MCP
  启动诊断统一投影给 Host，普通输出、JSON、Interactive、TUI 与 Desktop 不得静默忽略系统扩展降级。
- code trust 精确绑定 digest，capability grant 绑定 permission/contribution fingerprint；撤权先关闭 generation
  gate 并发布不含受影响 generation closure 的新 Catalog，再异步 drain owner。配置使用包含 credential binding
  reference 的内容寻址 immutable revision，selected digest 通过 active revision id 引用；配置/选版与候选 Catalog
  在同一成功边界提交，失败不再依靠补偿恢复。
- 启用依赖图只使用显式选择的已安装 digest；digest/configuration 改变会按拓扑重建传递 desired dependant closure，
  active dependent 存在时不能禁用依赖项。不同 digest 共用 plugin data，候选 Host API 写入先进入隔离 staging，
  发布成功才提交；只有最后一个安装被移除时才允许 purge。
- `ContributionCoordinator` 只负责把 generation contribution 校验、归一化为 owner set；`ExtensionCatalog` builder
  从稳定 Host baseline 与 active generation set 构建结构冻结的 Tool、tool policy、观察 Hook、context provider、
  turn policy、command、compaction 与 continuation registry。候选失败不会改写 baseline 或 current Catalog。
- `ouro.plugins.contribution_types` 是 Host-owned 封闭清单，把每种 Manifest capability 的 SDK、worker、prepare、
  optional、contract、resource 与 Catalog projection facet 绑定在同一记录。主流程遍历清单且不接受用户动态注册；
  Skill/MCP 是 generation-owned resource facet，只有准备成功的资源进入 contract。Background Service 唯一属于
  `activation-singleton`，其 projection 明确为空；其他 contribution 属于 Run `snapshot-bound`。
- Skill metadata 也是 Catalog 的 snapshot-bound 目录：Runtime 装配时发现的 user/workspace Skill 作为 Host baseline，
  Plugin Skill 作为 generation resource 加入同一名称空间。候选按名称拒绝冲突；每轮上下文从 Run lease 生成提示词，
  不通过 Hook 二次注入。disable 只改变新 Catalog，旧 lease 保留不可变包内的 `SKILL.md` 路径。
- Python in-process 的 canonical owned module 为 `<namespace>_plugin`（已带后缀时保持不变）；激活检查 canonical
  冲突，实际加载键为 `_ouro_plugin_<generation_id>`。插件内依赖必须放在 owned package 并使用相对 import；关闭
  generation 时删除其完整私有 module tree。需要独立第三方环境时使用 worker。
- Runtime 在 Run 创建时取得 `ExtensionCatalogLease`，并把同一 Catalog 贯穿 context、model tool list、tool
  execution、Hook、turn completion、compaction、continuation 和异步 `on_event`。current pointer 仅在短同步临界区交换；旧 Catalog
  由自身 lease 自然 drain，插件 import、worker/MCP 启停、回调和 I/O 均不在 pointer lock 内。
- 插件生命周期 mutation 由单一 runtime lock 串行化；候选 generation set、required MCP 与 Catalog 全部成功后，
  Store 以 expected state 比较提交，并在无 `await` 的发布段打开新 gate、交换 pointer。Background Service 不进入
  Run Catalog，发布后按“旧代停止、新代启动”串行转交。
- Catalog registry 的结构在构建后冻结；运行期只允许 Hook 的 per-Run isolation 状态变化。builtin/Host 与 user
  contribution 都有 canonical identity、owner 和 contract hash；插件激活不会修改进程级 Host registry。
- `LocalApplication` shutdown 先关闭 Runtime 并释放其 generation snapshot，再关闭插件 owner 与全局 MCP；插件
  runtime 等待 lease drain 到 deadline，逾期记录诊断后继续清理，避免资源 owner 先于消费者失效。
- 本地 store、credential resolver、registry 和 runtime 的具体装配只属于 `ouro.local_application`；
  `ouro.application.plugins.PluginService` 保持 Host-neutral，不提供隐藏的本地构造入口。
- pending Tool/Approval、Continuation 和 Run Catalog 持久化 `execution_contract_hash`；摘要覆盖 release identity（user
  package digest 或 builtin contract hash）、有效
  配置修订、传递依赖锁、Plugin API/worker protocol 和全部 contribution contract；Tool 的归一化 effect、batch
  behavior 与 resumable 能力属于该 contract。恢复忽略随机 `generation_id`，但会
  复核所有稳定事实与 ToolExecutor 契约，绝不按同名工具猜测替代。
- 插件更新后，若 durable Run 的旧 digest、配置修订和依赖仍保留，runtime 可重建一个非 current generation 与精确
  Catalog；该 Catalog 只由恢复 Run 持有，不修改 selected/desired state，不启动 activation-singleton Background
  Service，lease 释放后按 owner 回收。缺包、禁用或重建 contract 不一致时恢复显式失败；durable reference 释放前
  application facade 阻止移除旧 digest。
- `PluginService.scaffold/reload` 是 Host-neutral 开发用例：scaffold 原子发布经验证的最小 user package；reload 先
  并排安装 immutable candidate，只有配置、能力授权与 exact-digest code trust 完整时才复用 candidate activation
  切换 current。CLI 只负责参数与 DTO 映射。
- Git source 在 checkout 前以 tree metadata 校验 portable path、文件数和字节上限；shallow/blob-filtered fetch、
  subdirectory sparse checkout 和禁用 LFS smudge 共同避免先物化范围外内容。所有 source 最终仍进入统一 package
  validation。社区 compatibility inspection 只读资源并输出四类 outcome 与显式 OURO target，不执行未知代码。
- `ouro.mcp.sdk` 是官方 MCP Python SDK 的唯一适配边界；协议对象不得进入 core、application 或 tools。
- 每个 MCP server 由单一 owner task 打开、监督并关闭 transport context；Manager 的 start/stop 只通过事件
  协调 owner，避免跨任务退出 AnyIO 取消域。
- MCP 工具以 `<server_id>__<remote_name>` 形成稳定 identity，通过 ToolRegistry 原子替换 projection，并继续
  经过 OURO schema、effect 归一化、权限/策略、批次和恢复边界。用户可以按 server 内的 remote tool name 显式
  覆盖 effect；override 改变会产生不同 contract hash。
- Plugin MCP 由 generation resource owner 启停：required 连接失败中止候选，optional 失败投影 degraded；只有
  connected server 生成 contract，已发现 Tool 冻结进 Catalog，server 的后续变化只在下一 generation 可见。
- `MCPRuntimeResource` 把 Manager 适配到 RuntimeSupervisor 的通用受管资源协议；shutdown 关闭 session、HTTP
  client、stdio 管道和子进程。
- 外部 Agent Skills frontmatter 在 skills parser 边界校验，核心运行只接收类型化 `SkillMetadata` 和指令文本。

## 未来演进触发条件

只有出现独立变化轴时才新增层级：

- 第二种 Supervisor 后端或跨进程调度出现后，再拆分当前进程内调度实现；
- Desktop 配置界面直接扩展现有 `ConfigurationFacade` 用例，不另建 UI 专用 manager；
- TUI 与 Desktop 直接消费 application DTO、`RuntimeEventView`、activity snapshot 和增量 cursor，不解析后端；
- 第二种压缩后端独立演进后，拆分策略执行与上下文准备；
- 第二种资源后端出现后，再扩展 ResourceStore，当前只支持本地 immutable blob；
- 第二种 MCP SDK 或自定义 transport 出现后，再抽取 SDK 之外的 transport provider 层；当前 client port 已足以
  隔离官方 SDK 和测试实现。

这些边界应由真实用例驱动，不能为了缩短文件或统一命名而增加空转 service、回调层或通用 manager。

## 验收要求

每次架构改动至少验证：

```bash
python -m pytest --cov=ouro --cov-report=term-missing
python -m ruff check ouro tests
python -m ruff format --check ouro tests
python -m mypy ouro
python -m compileall -q ouro
git diff --check
```

同时检查依赖方向、持久化失败路径、并发锁、取消、公开数据结构和文档是否仍与实现一致。

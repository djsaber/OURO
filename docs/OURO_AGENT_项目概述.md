# OURO Agent 项目概述

## 项目定位

OURO 是本地优先的个人智能体运行时。它把会话历史、运行状态、工具副作用、人工决策和上下文维护
视为可检查、可恢复的数据，而不是一次模型请求的临时附属物。

OURO 的核心目标是提供稳定的智能体运行基础：同一套应用能力可被 CLI、未来桌面端和其他本地宿主
复用；模型、工具、Skills、插件和 MCP 通过明确边界接入；用户始终能够查看和控制本地数据。

## 设计原则

- 本地优先：会话、配置和运行记录默认保存在用户指定的本地目录。
- 可恢复：关键状态先持久化，再执行不可逆或结果未知的外部操作。
- 明确边界：core 不依赖 UI、文件系统或具体模型供应商。
- 单一契约：持久化格式、命令输入和运行时解析使用同一组权威字段；独立 record 必须携带显式版本。
- 用户控制：敏感工具执行、AskUser、终止和会话维护都有显式入口。
- 渐进扩展：只有真实变化方向清楚时才引入新抽象或基础设施。
- 可观察：重要状态变化、模型流、工具调用、失败和恢复均形成事件。

## 当前产品范围

### 智能体运行

OURO 支持流式、多轮的模型/工具循环。文本、reasoning 和工具调用参数分片均作为模型流事件逐段进入核心
循环；工具调用只在完整响应边界形成可执行批次。一次 Run 从用户输入开始，可经历多个模型回合和工具批次，
最终进入 completed、waiting、paused、aborted、failed 或 recoverable 状态。运行软预算可在模型回合边界
按轮数、累计 token 或耗时暂停运行。
流式与非流式 Provider 都在 adapter 边界输出同一种 `tool_call_delta`；Core 不维护完整调用事件的第二条路径。

### 会话与恢复

Session 是长期容器，保存消息、运行、事件、模型路由、压缩状态和目录元数据。当前支持：

- 创建、继续、重命名、归档、置顶、搜索和筛选；
- 在合法消息边界创建分支；
- 只读事件回放和数据导出；
- 删除到本地回收站；
- 进程锁、损坏索引重建和中断运行恢复；
- active run 期间来自用户、OURO Host/Application 或插件的 durable 排队输入、来源归属、去重，以及 run 级
  follow-up / turn 级 steer 交付；
- 消息和事件按稳定 cursor 增量读取，事件订阅也支持 after-sequence 追赶，长历史不要求 Host 全量加载；
- Session Catalog 只读取有界 latest/count 投影：文件后端读取有限尾部，SQLite 使用聚合查询，不加载完整历史；
- Goal、Plan、Todo 等扩展状态由 SessionRepository 按 owner/version/branch policy 保存；canonical 状态、
  scaffold projection、branch、export 与 trash 共享同一生命周期契约；
- 后端中立 SessionRepository 下的本地可读文件与 SQLite 两种实现，以及原子、验证、失败回滚的
  文件到 SQLite 导入；
- CLI Host 可通过 `--session-backend files|sqlite` 或 `OURO_SESSION_BACKEND` 选择后端；选择只决定当前数据集，
  不触发隐式迁移。

### 模型与上下文

Provider 表示供应商级连接，并独立保存用户实例 id、产品配置类型和线协议；Endpoint 表示具体模型目标。
会话保存完整 `provider/endpoint` 路由，模型参数和能力属于 Endpoint。运行时 adapter 只按协议分派，当前提供
Faux、OpenAI Chat Completions 和 Anthropic Messages 协议适配，内置
OpenAI、Anthropic、Gemini、DeepSeek、Qwen、KIMI、GLM、Grok、OpenRouter、Mistral 多 Endpoint 配置模板。HTTP
协议调用共享仅在首个
输出事件前发生的有界重试，并将重试计划记录为运行事件；结构化输出只有在 Endpoint 显式声明能力时才会
进入请求。

思考配置使用统一意图、Endpoint 真实能力和声明式原生参数 profile 三层契约。用户可对内置模板或任意
OpenAI-compatible/Anthropic-compatible 自定义模型选择开启、关闭和精确 effort；协议不支持的意图会在网络请求前
明确失败，不会被近似成相邻等级。可展示 reasoning 与工具续接状态独立保存，签名或私有 continuation 只在
同 provider/protocol/model 的工具回合中重放。

上下文由 canonical messages 投影生成。系统提示词、分层 `AGENTS.md` 指令、Skills 指令、压缩摘要和最近原子模型回合在请求前
组合；超过硬预算时明确失败或暂停，不静默丢弃 canonical history。摘要压缩使用独立记录保存覆盖范围、
来源 digest、策略和 usage。

默认启用的 Agent Context builtin 在请求时把用户级共享规则、`OURO_HOME/AGENTS.md` 和 Git 根到当前
workspace 的项目 `AGENTS.md` 从宽到窄追加到 system prompt，并继续在尾部提供原有运行环境事实。该路径不
加载普通项目文档、产品私有规则名、Memory 或 Skills；规则内容不写入 canonical history，编辑后在下一次
真实模型请求生效。

统一 Provider 只接收纯模型语义 `ModelMessage`，不会看到 Session 持久化 identity、metadata 或资源引用。
OURO 在每次模型请求前维护请求快照和类型化 token 指标；Provider 完成后在该审计事实上补记供应商报告的 input
token。面向用户的上下文查询读取最近一次真实 Provider 请求快照：长度与完整内容查询共享同一个请求事实，完整
查询返回 Provider 边界实际接受的 system prompt、messages 和 tools，因此包含请求级动态 Context Provider 注入。
查询不会重跑插件或从 canonical history 猜测 prospective context；手动压缩后的摘要会在下一次真实请求中出现。

### 工具与人机协同

内置工具包括 Read、Write、Edit、Bash 和 AskUser。所有工具通过统一 schema 校验、权限判断、执行上下文
和结果结构。工具也可返回携带任意 JSON 值的输入请求；Host 自行解释并呈现该值，回答作为同一次工具调用的
结果回填给模型。模型一次响应中的多个工具调用组成一个批次，可按配置顺序或并行执行；结果仍按原始调用顺序
提交。输入请求与工具审批使用不同状态转换，二者都可持久化并恢复。

工具权限支持 `auto`、`approval` 和 `deny`。默认由 Host 控制，用户配置可覆盖工具定义，会话批准策略支持
`inherit`、`auto`、`approval` 和 `deny`。Harness 工具可改为工具自控，此时只允许 `auto/deny`，且用户覆盖、
会话策略和限制性工具策略都不能改变其定义权限。该统一模型适用于内置、受信插件和 MCP 工具。内置文件路径与
Bash 工作目录默认限制在 Session workspace，工具输出由统一边界限长并标记截断。Bash 的工作目录约束不是
操作系统命令沙箱。

### 子智能体

OURO 提供一等、可恢复的单层子智能体能力。系统始终包含不可编辑/删除的 builtin `general`，用户可通过
AgentDefinitionService、CLI 和 TUI Actions 管理具有 name、description、system prompt、model 和 all/selected tools 的
自定义 definition；model 为空时继承 Session model。TUI Actions 提供可导航的统一目录列表与全局派遣策略配置，builtin
在管理入口中可见但不可编辑；agent/model allowlist 使用当前目录多选，不要求手输标识。Application API 完整表达
AgentSpawnPolicy，具体 Host 只展示其需要的字段。
每个 parent Run 冻结 Agent 目录；模型 `SpawnAgent` schema 与执行解析使用同一快照，active Run 不受后续配置编辑
影响。标准 LocalApplication 默认装配 Agent control tools；allowlist 无可用 definition 时只省略 SpawnAgent，不生成
无效工具 schema。

每次派遣创建 durable `DelegationRecord`、独立 subagent Session 和普通 subagent Run，继续复用现有模型循环、
工具、审批、AskUser、资源、预算和恢复。sync parent 以 `waiting_child` park 并释放 Supervisor permit；async
结果通过稳定 delivery identity 在 next-boundary 自动进行 exact-once 交付。root AgentTree、interaction inbox 和
subagent event stream 都是 application 投影，Host refresh/detach 不改变 subagent 生命周期。
循环交互 Host 在主终端等待输入时持续消费该 interaction inbox，因此 sync 与 async subagent 的工具审批和
AskUser 都可在主窗口处理。终端只有一个行读取所有者；interaction 到达时将当前提示重定向为明确的回答提示，
不会启动第二个 stdin reader，AskUser 空行也不会被提交。所有决议都按 subagent session/run/tool call/request
identity 精确路由，并覆盖决议提交与 canonical inbox 投影收敛之间的短暂重复通知。终端仅渲染主智能体行动轨迹
和 subagent HITL 提示，不展开 subagent 回复或工具调用。

运行契约固定 `max_depth=1` 和 bound lifecycle，但不设置 subagent 专用 active/queued 配额。每个合法派遣都提交
现有 RuntimeSupervisor，实际并发与排队由其通用并发上限和 FIFO 决定。工具 effect 与权限不参与并发调度；
多个 write-capable subagent 可同时运行，工具冲突通过普通失败结果回到各自模型。subagent 能力只能从 parent 的
工具/workspace/network/MCP/审批/预算上限收紧；nested 与 detached 明确拒绝。进程重启会把中断 Run
转 recoverable 并对账半提交 relation/result/delivery，但不会自动重放未知外部副作用。

### Skills 与扩展框架

OURO 发现标准 `SKILL.md`，只在目录阶段读取元数据，在使用时按需加载完整内容和资源。当前支持本地发现、
集中启停、校验、安装、更新和安全移除。固定目录源使用 Host 与作用域组成的稳定 id，并可独立关闭，例如
`ouro.user`、`ouro.workspace`、`codex.user`、`codex.workspace`；默认全部启用。插件 Skill 仍随所属插件的
Generation 原子发布和撤销，不进入目录来源开关。

Interactive 终端支持 `/skills` 查询当前扩展目录，以及 `/skill <name> [task]` 对一次 Run 显式激活技能。
CLI 只解析命令；Runtime 在该 Run 的冻结目录中完成可调用性校验和精确加载，并把技能正文固化为用户角色的
类型化消息内容。无参数调用、忙碌 Session 的 follow-up 排队和进程恢复使用同一语义；排队后技能文件或插件
generation 变化不会替换已接受的正文。显式激活不扩大工具权限，也不形成跨 turn 的 Session 模式。

MCP 客户端通过官方 Python SDK 处理协议与 session，并支持 stdio、Streamable HTTP 和 legacy SSE；其中
legacy SSE 数据通道由 OURO 适配为 SDK session stream，使读取和 POST 失败统一进入连接重建。客户端覆盖握手、
能力协商、工具分页发现与变化通知、调用、超时取消、健康检查、重连、凭据注入、隔离诊断和关闭。远程工具
固定映射为带 server 身份的 OURO Tool，并继续经过参数校验和权限执行边界。
默认 CLI 的单次运行、交互循环和审批恢复都会从 `mcp.servers` 装配这些工具；Host 退出后连接与动态工具
投影一并释放。单个 server 启动失败不会阻止不依赖它的普通 Run。`mcp list/show/set/remove/check` 提供
原子配置管理、脱敏查询和一次性连接诊断，用户不需要直接编辑共享 JSON 配置。

插件系统包括严格 manifest、local/Git/archive 安装、不可变 digest store、代码信任与 capability
grant、declarative/Python in-process/stdio worker runtime、不可变 owner Catalog、generation lease、依赖解析、
更新/回滚、Skills/MCP/background service、最小 scaffold/开发 reload、四类兼容报告和五类可运行示例。插件是 OURO
系统级模块，正式来源只分 builtin 与 user；
workspace/project 只影响一次执行的上下文，不存在项目插件或 workspace plugin scope。in-process 是用户明确授予的
同进程代码信任；worker 提供进程与协议隔离，但不是 OS sandbox。

插件可贡献 Host-neutral command 和 Run continuation policy。command 经 `sessions.write` 与 `background.run`
授权后可请求会话输入；continuation policy 在每个 Agent Run 终态后返回继续、完成、暂停或失败。application
持久化 generation-frozen checkpoint、处理冲突并用稳定 queue id 幂等入队。插件输入保存完整来源归因，并显式
携带 role、scaffold 和可选 semantic tag；这些维度不会相互推导，role 也不会使其变成直接用户来源或获得用户权限。
Interactive 支持 canonical/唯一短别名 slash command，并在插件启动的执行链运行时继续接收修改、引导、follow-up、
steer、暂停或中止输入。

插件工具可选接收 Host-owned workspace/path/cancel 上下文；限制性 tool policy 可实现 plan mode、危险命令确认、
受保护路径和合规门禁。策略只能提升审批或拒绝，不能批准、放宽 Host policy 或修改调用参数，失败时默认拒绝；
用户在审批中修改参数后会以最终参数重新评估。Runtime 为每个 Run 固定不可变 Extension Catalog，贯穿模型工具列表、
执行、Observer、上下文、压缩、continuation 与 policy；current pointer 切换不改变已经冻结的 Run，插件 contribution
归属于 Catalog，不写入 Host 全局 registry。每代 callback 经过 process-local gate，disable/revoke 后不能开始新调用。
enable/configure/select/restart 共享候选 activation 事务：失败不提交 state/Catalog，依赖变化重建传递 closure；候选
数据写入隔离暂存，后台服务按停旧启新顺序串行转交。依赖 observed state、封闭 contribution adapter 清单、严格
Worker 协议、代际私有 in-process module、统一 Run-local Skill Catalog 与 Plugin MCP 生命周期属于同一 generation
边界。Observer 使用独立有界
mailbox，不能阻塞语义路径；Prompt Policy 在直接用户提示持久化前 fail-closed，并把同一 Catalog contract 转交
prospective Run。完整审查与
取舍见[插件系统架构审查](OURO_插件系统扩展性架构与Pi对照_2026-07-17.md)。

### 资源与后台运行

ResourceService 把本地附件导入为内容寻址的不可变资源。消息只保存 resource id；相同内容去重，Session
引用只由 SessionRepository 持有，因而文件/SQLite Session 都能与本地 BlobStore 组合。分支原子复制引用
关系，删除按活动 Session 可达性清理共享内容。资源 metadata 和 Session export 不暴露
原始绝对路径。用户可把已导入资源附加到一次 Run；Interactive 的 Session-scoped attachment draft 通过
`/attach`、`/attachments` 和 `/detach` 把本地资源组合到下一条用户消息。Runtime 在提交前验证归属与完整性，只在请求内将受支持
的 UTF-8 文本或 vision 图片投影给模型；canonical message 仍只保存引用。PDF 等格式在没有专用 adapter 时
明确拒绝。

RuntimeSupervisor 在进程内为多个 Session 提供 FIFO 调度、全局并发限制、状态快照和有界事件订阅。Host
可读取细粒度 activity，区分模型准备、等待响应、各类增量流、工具预检/执行/结果提交、人工交互和压缩。
Host detach 不会隐式终止 Run；explicit abort 与进程 shutdown 使用不同的显式协议。重启后的事实仍来自
SessionStore，而不是内存 registry。subagent 使用同一 Supervisor 队列；parent `waiting_child` 不占 execution
permit，也没有第二个 Agent worker pool。

Host 事件由 `RuntimeEventView` 提供稳定类别和常用字段；原始扩展数据统一脱敏。慢 Host sink 与每个 Observer
在彼此独立的有界 mailbox 中运行，不能阻塞 durable event、其他 Observer 或其他 Session。`LocalApplication` 为 CLI 和未来 TUI/
Desktop 统一拥有 Runtime、MCP、Provider 与后台 worker 的启动和关闭。

### 运行界面

- Interactive：持续 stdin/stdout 会话、slash commands、下一消息图文资源组合、流式展示和人工交互；工具名称及参数分片在模型
  生成期间动态预览，完整响应后按调用顺序定稿；sync/async 子智能体的审批和提问通过 root interaction
  inbox 在同一非阻塞输入循环中处理，终端只展开主智能体行动轨迹。
- Print：stdout 输出最终文本，reasoning delta 以终端样式显示。
- JSON：stdout 输出稳定机器可读结果，并使用分类退出码。
- Agent：definition CRUD/tool compatibility，以及 list/show/spawn/wait/send/interrupt/close/tree/interactions；
  运行命令与未来 Desktop 使用同一 AgentService。

## 核心概念

### Session

持久化对话和工作状态的长期容器。同一 Session 只允许一个 active Run，但可以接收排队输入。follow-up
在 Run 结束后消费，steer 在完整 model turn 结束后消费；消费策略可配置为逐条或一次性消费。

### Run

一次可恢复执行。Run 保存来源、状态、当前阶段、模型、usage、待处理人工交互、活动工具批次，以及可选的
generation-frozen continuation checkpoint。

### Provider usage ledger

跨 Session 的供应商请求级 token/费用账本。usage 只采集 Provider 响应，按日期、模型和路由等维度查询；
上下文 token 估算不进入账本。人工修订和重置保留追加式审计历史；公开读取返回深拷贝，调用方不能修改
账本内存缓存中的权威状态。

### Model turn

一次完整 Provider 响应及其工具 continuation 边界。assistant 工具请求和对应结果共享 `turn_id`，用于恢复和
上下文压缩的原子性判断。

### Message

会话的 canonical 对话记录。工具请求使用 `tool_calls`，工具结果关联使用 `tool_result`；两者不隐藏在
metadata 中。可展示思考使用 `reasoning_content`，协议私有的工具续接状态使用路由绑定的
`provider_continuation`。消息使用封闭的 `system | user | assistant | tool_result` role；`scaffold` 独立说明是否由
Harness 有意注入，`semantic_tag` 独立提供可选模型语义包络。role、scaffold、tag 与 producer provenance 彼此正交，
全部由消息提供方定义；框架不推断或补全。直接用户输入显式使用 `user/scaffold=false`，子智能体初始 task 显式使用
`user/scaffold=true` 并保留 `delegation_id`。

### Event

按顺序追加的运行事实，用于流式渲染、诊断和恢复。文本、reasoning 与工具调用增量保留 Provider 的到达
顺序；事件不是 canonical message 的替代物，也不表示未完成的工具调用已经可以执行。

### Tool batch

一次模型响应中全部工具调用的 durable 状态集合，保存调用顺序、审批、执行结果和未知副作用状态。

### Compaction

对 canonical message 前缀生成的持久化摘要投影。压缩记录不修改或删除原消息。

### Delegation

root-scoped 的 durable 父子关系与冻结派遣契约。child Run 是执行状态唯一事实源；Delegation 只保存关系、能力上限、
result/delivery identity 和 revision，不镜像 running/waiting/terminal 状态。

## 数据与安全边界

- 所有路径写入受 `OURO_HOME` 或工作区约束；内置工具统一使用 execution context 校验路径和权限。
- 凭据通过环境变量引用或显式本地配置进入 Provider，不写入事件和公开输出。
- 外部工具结果可能包含不可信内容，只作为数据回灌模型，不改变系统权限。
- in-process 插件是同进程可信扩展；worker 是协议隔离。permission 是 Host-service 授权门，两者都不宣称
  OS 安全沙箱。
- 删除会话先发布到回收站；canonical 写入与可重建索引分离。
- 本地原子 I/O 与跨进程文件锁由共享基础设施拥有，Session、Resource、Config、Billing adapter 不互相借用实现。
- `ouro/**` 的依赖方向由递归 allow-edge matrix 检查，新增 package 必须显式登记其允许依赖。
- 配置 command 在跨进程锁内完成完整 read-validate-write；公共 DTO、事件和第三方错误统一脱敏。

## 未来设计范围

以下能力处于目标状态，不属于当前产品契约：

- TUI Host：复用 application service/DTO、typed event、activity 与 cursor 的终端原生界面。
- Desktop Host：复用相同 application service/DTO 的会话导航、运行视图、diff、审批和设置界面。
- 插件扩展：只有真实 user plugin 证明现有 13 类 extension point 无法表达 Agent、provider、resource 或 UI 能力时
  才增加类型。Marketplace、签名、publisher policy、OS sandbox、项目插件与历史兼容层不在当前范围。
- 长期记忆：来源可追踪、可查看、可编辑、可删除的记忆提取与检索。
- Durable Task/Workflow：在 builtin Goal 之外表达多任务依赖、跨 Run 调度、重试和用户可见任务图；它可以
  关联 Delegation，但不复制 Goal 或子智能体状态机。
- SQLite 产品化：Desktop 后端选择、迁移 CLI/UI、备份恢复、版本升级和真实规模性能基准。

未来能力必须复用现有 core/application 契约，不能让 Desktop、插件或协议对象直接接管运行状态。

## 非目标

- 云端多租户 Agent 平台。
- 默认远程同步用户会话或密钥。
- 用无界自治替代人工权限控制。
- 为尚未出现的使用场景预建通用工作流引擎、分布式队列或复杂依赖注入框架。

## 文档关系

- 当前代码边界以 [开发架构](OURO_AGENT_开发架构.md) 为准。
- 未实现能力的推进顺序以 [开发路线图](OURO_AGENT_开发路线图.md) 为准。
- 领域契约与验收证据见 [规格索引](../dev/specs/README.md)。
- 未来能力的任务拆分见 [执行计划索引](../dev/plans/README.md)。
- Goal builtin 的当前契约见 [OURO Goal 插件设计](OURO_Goal插件设计_2026-08-16.md)。
- 所有实现和评审遵循 [中文编码准则](OURO_AGENT_编码准则_ZH.md)。

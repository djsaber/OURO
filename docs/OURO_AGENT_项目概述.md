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
- 单一契约：持久化格式、命令输入和运行时解析使用同一组权威字段。
- 用户控制：敏感工具执行、AskUser、终止和会话维护都有显式入口。
- 渐进扩展：只有真实变化方向清楚时才引入新抽象或基础设施。
- 可观察：重要状态变化、模型流、工具调用、失败和恢复均形成事件。

## 当前产品范围

### 智能体运行

OURO 支持流式、多轮的模型/工具循环。文本、reasoning 和工具调用参数分片均作为模型流事件逐段进入核心
循环；工具调用只在完整响应边界形成可执行批次。一次 Run 从用户输入开始，可经历多个模型回合和工具批次，
最终进入 completed、waiting、paused、aborted、failed 或 recoverable 状态。运行软预算可在模型回合边界
按轮数、累计 token 或耗时暂停运行。

### 会话与恢复

Session 是长期容器，保存消息、运行、事件、模型路由、压缩状态和目录元数据。当前支持：

- 创建、继续、重命名、归档、置顶、搜索和筛选；
- 在合法消息边界创建分支；
- 只读事件回放和数据导出；
- 删除到本地回收站；
- 进程锁、损坏索引重建和中断运行恢复；
- active run 期间的 durable 排队输入、去重，以及 run 级 follow-up / turn 级 steer 交付。

### 模型与上下文

Provider 表示供应商级连接，Endpoint 表示具体模型目标。会话保存完整 `provider/endpoint` 路由，
模型参数和能力属于 Endpoint。当前提供 FauxProvider 和 OpenAI-compatible Provider。

上下文由 canonical messages 投影生成。系统提示词、Skills 指令、压缩摘要和最近原子模型回合在请求前
组合；超过硬预算时明确失败或暂停，不静默丢弃 canonical history。摘要压缩使用独立记录保存覆盖范围、
来源 digest、策略和 usage。

### 工具与人机协同

内置工具包括 Read、Write、Edit、Bash 和 AskUser。所有工具通过统一 schema 校验、权限判断、执行上下文
和结果结构。模型一次响应中的多个工具调用组成一个批次，可按配置顺序或并行执行；结果仍按原始调用顺序
提交。需要人工确认时，Run 进入 waiting 状态并可通过交互界面或 CLI 恢复。

### Skills 与扩展框架

OURO 发现标准 `SKILL.md`，只在目录阶段读取元数据，在使用时按需加载完整内容和资源。当前支持本地发现、
集中启停、校验、安装、更新和安全移除。

MCP 客户端通过官方 Python SDK 支持 stdio、Streamable HTTP 和 legacy SSE，覆盖握手、能力协商、工具分页
发现与变化通知、调用、超时取消、健康检查、重连、凭据注入、隔离诊断和关闭。远程工具固定映射为带 server
身份的 OURO Tool，并继续经过参数校验和权限执行边界。
默认 CLI 的单次运行、交互循环和审批恢复都会从 `mcp.servers` 装配这些工具；Host 退出后连接与动态工具
投影一并释放。单个 server 启动失败不会阻止不依赖它的普通 Run。`mcp list/show/set/remove/check` 提供
原子配置管理、脱敏查询和一次性连接诊断，用户不需要直接编辑共享 JSON 配置。

Hook 和插件 contribution registry 已具备代码边界；完整的插件分发、信任管理和 entrypoint 生命周期尚未完成。

### 资源与后台运行

ResourceService 把本地附件导入为内容寻址的不可变资源。消息只保存 resource id；相同内容去重，Session
分支原子复制引用关系，删除按活动 Session 可达性清理共享内容。资源 metadata 和 Session export 不暴露
原始绝对路径。

RuntimeSupervisor 在进程内为多个 Session 提供 FIFO 调度、全局并发限制、状态快照和有界事件订阅。
Host detach 不会隐式终止 Run；explicit abort 与进程 shutdown 使用不同的显式协议。重启后的事实仍来自
SessionStore，而不是内存 registry。

### 运行界面

- Interactive：持续 stdin/stdout 会话、slash commands、流式展示和人工交互。
- Print：stdout 输出最终文本，reasoning delta 以终端样式显示。
- JSON：stdout 输出稳定机器可读结果，并使用分类退出码。

## 核心概念

### Session

持久化对话和工作状态的长期容器。同一 Session 只允许一个 active Run，但可以接收排队输入。follow-up
在 Run 结束后消费，steer 在完整 model turn 结束后消费；消费策略可配置为逐条或一次性消费。

### Run

一次可恢复执行。Run 保存来源、状态、当前阶段、模型、usage、待处理人工交互和活动工具批次。

### Model turn

一次完整 Provider 响应及其工具 continuation 边界。assistant 工具请求和对应结果共享 `turn_id`，用于恢复和
上下文压缩的原子性判断。

### Message

会话的 canonical 对话记录。工具请求使用 `tool_calls`，工具结果关联使用 `tool_result`；两者不隐藏在
metadata 中。

### Event

按顺序追加的运行事实，用于流式渲染、诊断和恢复。文本、reasoning 与工具调用增量保留 Provider 的到达
顺序；事件不是 canonical message 的替代物，也不表示未完成的工具调用已经可以执行。

### Tool batch

一次模型响应中全部工具调用的 durable 状态集合，保存调用顺序、审批、执行结果和未知副作用状态。

### Compaction

对 canonical message 前缀生成的持久化摘要投影。压缩记录不修改或删除原消息。

## 数据与安全边界

- 所有路径写入受 `OURO_HOME`、工作区或工具参数约束；工具仍必须自行校验输入和权限。
- 凭据通过环境变量引用或显式本地配置进入 Provider，不写入事件和公开输出。
- 外部工具结果可能包含不可信内容，只作为数据回灌模型，不改变系统权限。
- 插件是同进程可信扩展，当前不提供安全沙箱。
- 删除会话先发布到回收站；canonical 写入与可重建索引分离。

## 未来设计范围

以下能力属于明确的未来方向，当前实现不应假装已经提供：

- Desktop Host：基于 application facade 的会话导航、运行视图、diff、审批和设置界面。
- 完整插件系统：安装来源、信任声明、权限确认、entrypoint 加载、升级与隔离诊断。
- 长期记忆：来源可追踪、可查看、可编辑、可删除的记忆提取与检索。
- 计划与子智能体：显式任务树、预算、取消、结果归并和用户可见进度。
- 可替换持久化后端：在本地文件规模成为实际瓶颈后评估 SQLite 等实现。

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
- 所有实现和评审遵循 [中文编码准则](OURO_AGENT_编码准则_ZH.md)。

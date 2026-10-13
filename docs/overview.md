# OURO 项目概述

> 文档类型：产品与能力说明
> 事实来源：当前源代码、CLI 解析器与行为测试；精确契约见领域规格。

## 定位与原则

OURO 是本地优先的个人智能体运行时。会话历史、运行状态、工具副作用、人工决策和上下文维护均有可检查的持久记录。CLI 与 TUI 复用同一应用层，模型和扩展通过明确协议接入。

设计以行为正确、资源可关闭、状态可恢复和用户可控制为基础。权威数据与可重建索引、查询投影、UI 状态分开；新抽象由实际变化方向和可验证收益驱动。

## 当前能力

| 领域 | 已实现范围 | 精确契约 |
|---|---|---|
| 运行 | 默认与可替换 Run Driver、共享模型 / 工具 / Agent 执行端口、顺序或并行批次、取消、预算、恢复、follow-up / steer 排队输入 | [运行时](specs/runtime.md) |
| 会话 | 创建、继续、重命名、归档、置顶、搜索、分支、事件回放、回收站、游标查询和消息撤销 | [持久化](specs/session-persistence.md)、[撤销](specs/session-undo.md) |
| 存储 | 文件与 SQLite 会话后端，共用契约；显式校验导入、原子提交和恢复 | [会话持久化](specs/session-persistence.md) |
| 模型 | Faux、OpenAI Chat Completions、OpenAI Responses、Anthropic Messages；Provider / Endpoint、模板与自定义连接、推理能力配置 | [Provider 配置](specs/provider-configuration.md)、[协议](specs/provider-protocols.md) |
| 认证 | 静态引用、受管理凭据、浏览器订阅授权、刷新、注销、请求认证与跨进程协调 | [认证架构](architecture/authentication.md) |
| 上下文 | 原子模型帧、分层 AGENTS.md、Skills、动态 Context Provider、真实请求快照、摘要压缩和硬预算 | [上下文与压缩](specs/context-and-compaction.md) |
| 工具与交互 | Read、Write、Edit、Glob、Grep、TodoList、宿主 Shell、AskUser；权限、审批、结构化输入和 AFK | [工具与交互](specs/tools-and-interactions.md)、[AFK](specs/afk.md) |
| 子智能体 | 内置 general 与用户定义、冻结派遣目录、单层 sync / async 子会话、结果交付、根交互路由和恢复对账 | [子智能体](specs/subagents.md) |
| 扩展 | 插件安装与版本选择、不可变包、精确依赖租约、15 类原生贡献、具名服务与动态工具、双向 worker、受管理进程及后台服务；Goal / Plan、Skills 与 MCP | [插件](specs/plugins.md)、[Skills](specs/skills.md)、[MCP](specs/mcp.md) |
| 插件配置 | 自包含 JSON Schema、独立凭据引用、静态预检、带编辑基线的完整提交、不可变快照；CLI 与 TUI 复用统一应用管线 | [插件配置](specs/plugin-configuration.md) |
| 资源 | 会话独立附件、原路径引用、完整性校验、复制与导出，以及有界文本 / 原生图片输入 | [资源与监督](specs/resources-and-supervision.md) |
| 自动任务 | 时区和日历计划、原子领取、会话任务与插件 handler、审批挂起、执行历史与恢复；文件 / SQLite 任务后端 | [自动任务](specs/automated-tasks.md) |
| 用量 | 请求级 token / 费用账本、计费归属、过滤汇总、审计修改和选择性重置 | [用量与计费](specs/usage-and-billing.md) |
| 数据传输 | 各配置域、原生 Skills、完整会话树和附件的验证导入导出及跨会话后端传输 | [数据传输](specs/data-transfer.md) |
| 数据升级 | 按数据契约版本在原 home 升级有限历史核心格式、备份与中断恢复；TUI 自动升级和需危险确认的初始化兜底 | [数据升级](specs/data-upgrade.md) |
| Host | Textual 全屏 TUI、CLI 单次文本 / JSON 输出、配置与维护命令、类型化事件观察 | [CLI](specs/cli-and-hosts.md)、[TUI](specs/tui.md)、[会话观察](specs/session-observation.md) |

模型目录声明、OURO adapter 能力和真实服务验收是不同事实。浏览器订阅与预制模型的验证范围见[订阅指南](guides/subscriptions.md#证据范围)和[预制配置](specs/provider-presets.md)，不能从模板存在推断账户可用性或全部参数已验收。

## 核心概念

| 概念 | 职责 |
|---|---|
| Session | 长期会话容器，关联历史、路由、Run、压缩、扩展状态与资源引用 |
| Run | 一次有界执行；拥有运行、工具批次、等待、取消和终结事实 |
| Model turn | 一次模型响应及对应工具处理边界，保证工具请求 / 结果成帧 |
| Provider / Endpoint | 供应商连接与具体模型目标；实例 ID、产品类型和线协议彼此独立 |
| Resource | 会话拥有的不可变文件及元数据；模型请求通过可读路径访问 |
| ExtensionCatalogLease | 一次决策及调用持有的当前扩展目录租约 |
| Compaction | 覆盖一段历史前缀的持久摘要，不改写原始消息 |
| Delegation | 父子会话关系、能力上限和结果交付身份；child Run 保持执行状态的唯一来源 |
| Goal | 主会话内一个用户拥有的目标槽位，跨有界 Run 继续；不充当通用工作流图 |

## 数据与安全边界

- 主数据保存在所选 home。文件与 SQLite 数据集独立，切换选择不执行隐式迁移；格式不受支持时明确拒绝。
- 模型调用将请求内容发送至所配置服务；MCP、Shell 和插件可能访问外部资源。工作区文件约束不是 OS 沙箱。
- `deny` 是硬禁用；人工审批与用户输入分别有持久化协议。AFK 不伪造用户回答，也不突破硬禁用和能力上限。
- 进程内插件是受信代码；worker 提供进程和协议隔离，未提供 OS 安全隔离。
- 凭据可来自环境变量引用或独立托管存储，公开 DTO 和输出脱敏；内联配置和导出归档仍可能包含私密数据。
- 权威写入和恢复由所属应用 / 存储边界负责；索引与 Host 投影可重建，不建立第二套运行状态机。

详见[安全策略](../SECURITY.md)和[架构说明](architecture/README.md)。

## 未来范围与非目标

Desktop Host、长期记忆、通用依赖任务图以及进一步的真实终端和长历史产品化见[路线图](plans/roadmap.md)。这些目标不作为当前可用功能宣称。

OURO 不以云端多租户、默认远程同步会话或凭据、无界自治替代人工控制为目标。新的扩展点和基础设施需由真实业务需求证明必要。

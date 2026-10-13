# OURO 领域规格（Specs）

本目录保存 OURO 独立维护的正式规格（Spec），按业务领域定义实现与回归验收共同遵循的模型、状态、不变量、失败语义和权限边界。操作步骤见[使用指南](../README.md#入门与使用)，模块职责与设计取舍见[架构](../architecture/README.md)。

规格与实现不一致时，结合调用链、可重复测试和既定行为约束判断应修正文档还是实现；不能仅因代码目前如此就覆盖已承诺的契约。目标规格与局部未验收能力必须明确标注，不能作为已发布能力说明；实施任务与验收安排统一放在[规划](../plans/README.md)。

## 运行、会话与调度

| 规格 | 关注点 | 主要实现 |
|---|---|---|
| [核心运行时](runtime.md) | Session/Run/turn、inner loop、状态转换、取消与排队输入 | `ouro/core`、`ouro/application/runtime.py` |
| [会话与持久化](session-persistence.md) | SessionRepository、文件/SQLite、索引、锁、恢复、分支与回收站 | `ouro/sessions`、`ouro/application/sessions.py` |
| [会话撤销、Harness 状态与插件交互](session-undo.md) | 原生状态版本、精确撤销、插件调用/提交、队列竞争、恢复与验收证据 | `ouro/core/harness.py`、`ouro/sessions`、`ouro/application`、`ouro/plugins/harness_calls.py` |
| [上下文与压缩](context-and-compaction.md) | ContextView、原子帧、预算、摘要、投影与 lineage | `ouro/core/context.py`、`ouro/application/compaction.py` |
| [会话观察](session-observation.md) | Host-neutral snapshot/live 衔接、父子关系与只读 child 导航 | `ouro/application`、`ouro/sessions`、`ouro/tui` |
| [资源与运行监督](resources-and-supervision.md) | 会话资源归属、完整性、RuntimeSupervisor 调度与订阅 | `ouro/resources`、`ouro/application/resources.py`、`ouro/application/supervisor.py` |
| [附件输入与 TUI 交互](attachments.md) | 附件元素、批量导入、@ 目录选择、排队撤回和模型投影 | `ouro/core`、`ouro/application`、`ouro/tui` |
| [子智能体](subagents.md) | AgentDefinition、Delegation、控制工具、HITL、权限、交付与恢复 | `ouro/core/agents.py`、`ouro/application/agents.py`、`ouro/sessions` |
| [自动任务](automated-tasks.md) | 计划与时区、原子领取、任务/执行状态、后台会话、恢复与 TUI 管理 | `ouro/automation`、`ouro/application/tasks.py`、`ouro/application/task_execution.py`、`ouro/tui/task_workflows.py` |

## 模型、认证与用量

| 规格 | 关注点 | 主要实现 |
|---|---|---|
| [Provider 与配置](provider-configuration.md) | Provider/Endpoint、静态及受管理认证引用、模板绑定、运行参数与覆盖规则 | `ouro/config`、`ouro/providers` |
| [Provider 类型与协议](provider-protocols.md) | 实例 id、产品配置类型、线协议与冲突策略 | `ouro/config`、`ouro/providers`、`ouro/cli` |
| [Provider 模板](provider-templates.md) | 本地模板、版本、修改感知、内容更新与严格版本边界 | `ouro/config/provider_template_store.py`、`ouro/config/provider_templates.py` |
| [预置供应商配置](provider-presets.md) | 供应商模型目录、连接参数、能力声明与官方依据 | `ouro/config/provider_templates.py` |
| [受管理凭据核心](credentials.md) | 受限文件存储、revision、刷新/注销/取消与跨进程互斥；内建浏览器订阅产品复用 | `ouro/auth` |
| [认证驱动与单次授权](authorization.md) | 受管理驱动端口、单次授权与资源清理、内建 OpenAI 浏览器订阅驱动；CLI/TUI 通过通用用例接入 | `ouro/auth/driver_contracts.py`、`ouro/auth/authorization.py`、`ouro/auth/drivers` |
| [Provider 认证用例](provider-authentication.md) | 通用登录、安全状态、凭据管理与配置条件提交；轻量 CLI、通用 TUI 与内建浏览器驱动已接线 | `ouro/application/provider_auth*.py` |
| [模型请求认证](request-authentication.md) | 逐次认证快照、最终目标、统一重试及错误码；三种协议、共享 Runtime、路由重绑定与内建浏览器驱动已接线 | `ouro/auth/request_auth.py`、`ouro/auth/managed_requests.py`、`ouro/providers` |
| [模型订阅认证](subscription-authentication.md) | 认证组合、浏览器产品、Responses 与验收矩阵；设备码和其他未验证能力在章节中标注 | `ouro/auth`、`ouro/providers`、`ouro/local_application.py` |
| [Responses 编解码](responses-codec.md) | 协议 adapter/注册、同次认证、SSE 投影与完整 scope/envelope；浏览器订阅的请求与续接 | `ouro/providers/openai_responses.py`、`openai_responses_*.py` |
| [用量与计费](usage-and-billing.md) | 请求时计费归属、实际 token、未知/部分金额、账本审计与 CLI/TUI 呈现 | `ouro/core/billing.py`、`ouro/application/usage.py`、`ouro/billing` |

## 工具与扩展

| 规格 | 关注点 | 主要实现 |
|---|---|---|
| [工具与人机协同](tools-and-interactions.md) | 工具契约、权限、批执行、审批、AskUser、AFK 与取消 | `ouro/tools`、`ouro/application/tool_calls.py` |
| [Skills 与扩展](skills.md) | Skills、显式激活、扩展边界与来源策略 | `ouro/skills`、`ouro/application/skill_activations.py` |
| [MCP 客户端](mcp.md) | transport、握手、工具发现/调用、重连、诊断与关闭 | `ouro/mcp`、`ouro/tools/registry.py` |
| [插件系统](plugins.md) | 包、安装、Catalog/generation、15 类 contribution、策略与恢复 | `ouro/plugins`、`ouro/sdk/plugins/v3.py` |
| [插件配置](plugin-configuration.md) | 通用 Schema、凭据槽、统一配置管线、Host 自动交互与修订生效 | `ouro/plugins/configuration*.py`、`ouro/application/plugin_configuration*.py`、CLI/TUI |
| [Goal 插件](goal-plugin.md) | 目标状态、命令、预算、自动继续及恢复契约 | `ouro/builtin_plugins/goal` |
| [AFK](afk.md) | 无人值守审批、输入 resolution 与 Plan 组合 | `ouro/core`、`ouro/application` |

## Host、数据传输与性能

| 规格 | 关注点 | 主要实现 |
|---|---|---|
| [CLI 与 Host](cli-and-hosts.md) | 命令面、输出模式、退出码和 Host/Application 边界 | `ouro/cli`、`ouro/local_application.py` |
| [TUI](tui.md) | 页面、命令、键位、认证管理、子智能体视图、渲染与恢复 | `ouro/tui` |
| [配置与数据传输](data-transfer.md) | 配置分域、统一导入导出、会话树与附件、技能目录、未知依赖与失败边界 | `ouro/application/data_transfer.py`、`ouro/transfer`、`ouro/sessions/portable.py` |
| [数据升级](data-upgrade.md) | 指定 home 的有限核心格式升级、备份、启动保护与失败恢复 | `ouro_upgrade`、`ouro/deployment.py` |
| [性能热路径](performance.md) | Provider 流、持久化、工具边界、TUI 帧与响应性预算 | `ouro/core`、`ouro/application`、`ouro/tui` |

## 维护与验收

同一精确契约由一个领域规格维护，其余文档通过链接引用。强制约束应有类型模型、单元 / 集成测试、架构边界或恢复测试支持。真实账户、终端与平台验证单独说明，不能由离线 fixture 推断。

未来平台的目标契约见[平台规划](../plans/platform.md)，文档组织与验证规则见[文档规范](../development/documentation.md)。

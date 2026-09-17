# OURO 规格索引

> 索引基线：2026-09-16

本目录保存当前代码契约和未来规格。规格直接描述行为、失败语义、数据边界与验收证据；
相关使用示例和操作入口见 docs 中的对应指南，规格之间通过链接引用共同契约。

## 阅读规则

- “现行规格”由当前代码与测试实现；行为冲突时先以代码和可重复测试校准事实，再修正文档或实现。
- “未来规格”描述尚未实现的契约，不能作为可用能力对外宣称。
- 同一文件含未来增量时，必须在对应章节显式标注，不使用含糊的“部分完成”。
- 产品范围见[项目概述](../../docs/OURO_项目概述_2026-09-02.md)，模块归属见
  [开发架构](../../docs/OURO_开发架构_2026-09-02.md)，实施顺序见
  [开发路线图](../../docs/OURO_开发路线图_2026-09-02.md)。

## 现行规格

| 规格 | 关注点 | 主要实现 |
|---|---|---|
| [核心运行时](OURO_核心运行时规格_2026-09-02.md) | Session/Run/turn、inner loop、状态转换、取消与排队输入 | `ouro/core`、`ouro/application/runtime.py` |
| [会话与持久化](OURO_会话持久化规格_2026-09-02.md) | SessionRepository、文件/SQLite、索引、锁、恢复、分支与回收站 | `ouro/sessions`、`ouro/application/sessions.py` |
| [会话撤销、Harness 状态与插件交互](OURO_会话撤销与Harness状态规格_2026-09-08.md) | 原生状态版本、精确撤销、插件调用/提交、队列竞争、恢复与验收证据 | `ouro/core/harness.py`、`ouro/sessions`、`ouro/application`、`ouro/plugins/harness_calls.py` |
| [Provider 与配置](OURO_Provider配置规格_2026-09-02.md) | Provider/Endpoint、静态及受管理认证引用、模板绑定、运行参数与覆盖规则 | `ouro/config`、`ouro/providers` |
| [受管理凭据核心](OURO_凭据核心规格_2026-09-16.md) | 受限文件存储、revision、刷新/注销/取消与跨进程互斥；尚未接入订阅产品 | `ouro/auth` |
| [Provider 认证用例](OURO_Provider认证用例规格_2026-09-16.md) | 通用登录、安全状态、凭据管理与配置条件提交；轻量 CLI 与通用 TUI 已接线，生产驱动待验证 | `ouro/application/provider_auth*.py` |
| [认证驱动与单次授权](OURO_认证驱动与授权规格_2026-09-16.md) | 受管理驱动端口、封闭 challenge、单次授权与资源清理；尚无生产驱动；CLI/TUI 通过通用用例接入 | `ouro/auth/driver_contracts.py`、`ouro/auth/authorization.py` |
| [模型请求认证](OURO_请求认证规格_2026-09-17.md) | 逐次认证快照、最终目标、统一重试及错误码；现有两种协议、共享 Runtime 和路由重绑定已接线，生产驱动待实施 | `ouro/auth/request_auth.py`、`ouro/auth/managed_requests.py`、`ouro/providers` |
| [Provider 模板](OURO_Provider模板规格_2026-09-02.md) | 本地模板、版本、修改感知、内容更新与严格版本边界 | `ouro/config/provider_template_store.py`、`ouro/config/provider_templates.py` |
| [Provider 类型与协议](OURO_Provider类型与协议规格_2026-09-02.md) | 实例 id、产品配置类型、线协议与冲突策略 | `ouro/config`、`ouro/providers`、`ouro/cli` |
| [工具与人机协同](OURO_工具与人机协同规格_2026-09-02.md) | 工具契约、权限、批执行、审批、AskUser、AFK 与取消 | `ouro/tools`、`ouro/application/tool_calls.py` |
| [上下文与压缩](OURO_上下文与压缩规格_2026-09-02.md) | ContextView、原子帧、预算、摘要、投影与 lineage | `ouro/core/context.py`、`ouro/application/compaction.py` |
| [Skills 与扩展](OURO_Skills与扩展规格_2026-09-02.md) | Skills、显式激活、扩展边界与来源策略 | `ouro/skills`、`ouro/application/skill_activations.py` |
| [MCP 客户端](OURO_MCP客户端规格_2026-09-02.md) | transport、握手、工具发现/调用、重连、诊断与关闭 | `ouro/mcp`、`ouro/tools/registry.py` |
| [插件系统](OURO_插件系统规格_2026-09-02.md) | 包、安装、Catalog/generation、13 类 contribution、策略与恢复 | `ouro/plugins`、`ouro/sdk/plugins/v2.py` |
| [资源与运行监督](OURO_资源与运行监督规格_2026-09-02.md) | 内容寻址资源、引用可达性、RuntimeSupervisor 调度与订阅 | `ouro/resources`、`ouro/application/resources.py`、`ouro/application/supervisor.py` |
| [自动任务](OURO_自动任务规格_2026-09-06.md) | 计划与时区、原子领取、任务/执行状态、后台会话、恢复与 TUI 管理 | `ouro/automation`、`ouro/application/tasks.py`、`ouro/application/task_execution.py`、`ouro/tui/task_workflows.py` |
| [子智能体](OURO_子智能体规格_2026-09-02.md) | AgentDefinition、Delegation、控制工具、HITL、权限、交付与恢复 | `ouro/core/agents.py`、`ouro/application/agents.py`、`ouro/sessions` |
| [会话观察](OURO_会话观察规格_2026-09-02.md) | Host-neutral snapshot/live 衔接、父子关系与只读 child 导航 | `ouro/application`、`ouro/sessions`、`ouro/tui` |
| [CLI 与 Host](OURO_CLI与Host规格_2026-09-02.md) | 命令面、输出模式、退出码和 Host/Application 边界 | `ouro/cli`、`ouro/local_application.py` |
| [TUI](OURO_TUI规格_2026-09-02.md) | 页面、命令、键位、认证管理、子智能体视图、渲染与恢复 | `ouro/tui` |
| [性能热路径](OURO_性能热路径规格_2026-09-02.md) | Provider 流、持久化、工具边界、TUI 帧与响应性预算 | `ouro/core`、`ouro/application`、`ouro/tui` |

## 未来规格

| 规格 | 目标能力 |
|---|---|
| [模型订阅认证](OURO_模型订阅认证规格_2026-09-16.md) | 多厂商认证端口、登录契约、Responses、OpenAI 订阅和验收矩阵；完整接入待实施，凭据核心另见现行规格 |
| [个人智能体平台](OURO_未来平台规格_2026-09-02.md) | Desktop、长期记忆、Durable Task/Workflow 与产品化边界 |

## 规格完成标准

每条强制约束至少由一种可重复证据保护：类型化模型或 Protocol、单元测试、Application/adapter 集成测试、
架构依赖测试、CLI/TUI 行为测试，或本地数据布局与恢复测试。目标规格和未来规格进入实现前，必须补齐依赖、迁移、
失败路径、并发/恢复矩阵和可验收退出条件。

# OURO 规格索引

本目录保存 OURO 的规范性规格。规格描述当前契约或明确的目标契约，不记录方案演变、修订过程、
完成历史或版本对比。

## 文档规则

- `状态：当前` 表示规格由当前代码和测试实现。
- `状态：目标` 表示规格是未来实现必须满足的契约，不能据此宣称功能已经可用。
- 当前行为与规格冲突时，以代码和可重复测试作为事实证据，并立即校正规格或实现。
- 产品范围以 [`docs/OURO_AGENT_项目概述.md`](../../docs/OURO_AGENT_项目概述.md) 为准。
- 模块归属以 [`docs/OURO_AGENT_开发架构.md`](../../docs/OURO_AGENT_开发架构.md) 为准。
- 未实现工作的执行顺序以 [`dev/plans/README.md`](../plans/README.md) 为准。

## 当前规格

| 规格 | 关注点 | 主要实现 |
|---|---|---|
| [核心运行时](core-runtime.md) | Session/Run/turn、inner loop、状态转换、取消与排队输入 | `ouro/core`、`ouro/application/runtime.py` |
| [会话与持久化](sessions-persistence.md) | SessionStore、本地布局、索引、锁、恢复、分支与回收站 | `ouro/sessions`、`ouro/application/sessions.py` |
| [Provider 与配置](providers-config.md) | Provider/Endpoint、模型路由、凭据、配置结构与覆盖规则 | `ouro/config`、`ouro/providers` |
| [供应商类型与协议类型拆分](provider-type-protocol.md) | 集成厂商/通用配置类型、线协议、实例 id 与冲突策略 | `ouro/config`、`ouro/providers`、`ouro/cli` |
| [工具与人机协同](tools-hitl.md) | 工具契约、权限、批执行、审批、AskUser 和进程取消 | `ouro/tools`、`ouro/application/tool_calls.py` |
| [上下文与压缩](context-compaction.md) | ContextView、原子帧、预算、摘要、投影与 lineage | `ouro/core/context.py`、`ouro/application/compaction.py` |
| [Skills 与扩展](skills-extensions.md) | Skills、Hook、插件 contribution 和 MCP 框架边界 | `ouro/skills`、`ouro/plugins`、`ouro/mcp` |
| [MCP 客户端](mcp-client.md) | transport、握手、工具发现/调用、重连、诊断与资源关闭 | `ouro/mcp`、`ouro/tools/registry.py` |
| [CLI 与 Host](cli-host.md) | 命令面、输出模式、退出码和 Host/application 边界 | `ouro/cli`、`ouro/application/facade.py` |
| [资源与后台运行监督](resources-supervisor.md) | ResourceService、不可变 blob、引用可达性、RuntimeSupervisor 调度与订阅 | `ouro/resources`、`ouro/application/resources.py`、`ouro/application/supervisor.py` |

## 目标规格

| 规格 | 目标能力 |
|---|---|
| [插件系统](plugin-system.md) | 包、来源、信任、授权、双运行时、贡献事务、durable generation、兼容、更新与回滚 |
| [个人智能体平台能力](future-platform.md) | Desktop、记忆与子智能体等跨能力目标 |

## 规格完成标准

当前规格的每条强制约束必须至少由以下一种证据保护：

- 类型化模型或 Protocol；
- 单元测试；
- application/adapter 集成测试；
- 架构依赖测试；
- CLI 行为测试；
- 本地数据布局或恢复测试。

目标规格进入实现前，必须在对应计划中拆出依赖、迁移、失败路径和验收任务。

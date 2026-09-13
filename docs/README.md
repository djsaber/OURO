# OURO 文档索引

> 索引基线：2026-09-13

文档分为现行说明、规范性规格、改进设计与实施计划、未来规划和非规范性调研。阅读当前行为时，以代码、测试和
“现行规格”为准；标为“本次改进目标”的文档约束待实施工作，不能作为已实现能力。调研只提供证据与设计依据。

## 建议阅读顺序

1. [项目概述](OURO_项目概述_2026-09-02.md)：产品范围、核心概念与数据/安全边界。
2. [开发架构](OURO_开发架构_2026-09-02.md)：分层、模块职责、运行链和扩展边界。
3. [规格索引](../dev/specs/README.md)：各领域的精确契约、失败语义和验收证据。
4. [开发路线图](OURO_开发路线图_2026-09-02.md)与[规划索引](../dev/plans/README.md)：未来能力和执行顺序。

## 现行说明与指南

| 文档 | 内容 |
|---|---|
| [项目概述](OURO_项目概述_2026-09-02.md) | 当前能力、核心概念、未来边界与非目标 |
| [开发架构](OURO_开发架构_2026-09-02.md) | 依赖方向、composition root、运行链、持久化和扩展 |
| [开发路线图](OURO_开发路线图_2026-09-02.md) | TUI 产品化、Desktop、长期记忆、Workflow 和 SQLite 产品化 |
| [插件开发指南](OURO_插件开发指南_2026-09-02.md) | 插件形态、Manifest、运行时、开发/安装/更新流程和示例 |
| [Goal 插件](OURO_Goal插件_2026-09-02.md) | Goal 状态、命令、工具、预算、自动继续、恢复和安全不变量 |
| [AFK 模式](OURO_AFK模式_2026-09-02.md) | 无人值守审批、输入 resolution、Plan 组合和持久化语义 |
| [中文编码准则](OURO_编码准则_ZH_2026-09-02.md) | 实现、测试、重构与评审规范 |
| [English development principles](OURO_编码准则_EN_2026-09-02.md) | English version of the development standard |

## 规范与规划

- [规格索引](../dev/specs/README.md)：现行规格、本次改进目标规格与未来规格；覆盖 Core、Session、Provider、Tool/HITL、Context、Skills、MCP、
  Plugin、Resource/Supervisor、自动任务、Subagent、CLI、TUI 与性能。
- [自动任务规格](../dev/specs/OURO_自动任务规格_2026-09-06.md)：配置、计划、状态机、统计、会话运行与 TUI 验收契约。
- [当前规划](../dev/plans/README.md)：Desktop Host、高级智能体能力和性能治理。

规格保留领域模型、状态机、不变量、失败/恢复语义、权限边界、数据布局和验收条件；规划完成后，最终行为迁入
规格，已完成 checklist 不长期保留。

## 设计

- [会话撤销、Harness 状态与插件交互改进设计](OURO_会话撤销与Harness状态设计_2026-09-08.md)：已实施；
  核心设计哲学、框架/插件职责、原生状态和提交边界，配套
  [现行规格与验收证据](../dev/specs/OURO_会话撤销与Harness状态规格_2026-09-08.md)。
- [自动任务](OURO_自动任务_2026-09-06.md)：执行计划、应用分层、统一运行链路、恢复语义及 TUI 管理。

| 设计 | 范围 |
|---|---|
| [TUI 视觉样式与 Markdown 渲染设计](OURO_TUI视觉样式与Markdown渲染设计_2026-09-02.md) | 完整 TuiTheme Token 注入、Markdown/Rich 适配、标识符、动画与验收约束 |

## 调研

| 调研 | 范围 |
|---|---|
| [插件系统](调研_插件系统_2026-07-16.md) | Claude Code、Codex、Pi、OpenCode 的包、信任、运行时、Hook 与生命周期 |
| [计划模式 Harness](调研_计划模式Harness_2026-07-18.md) | 模式上下文、工具约束、退出协议和回注策略 |
| [子智能体设计范式](调研_子智能体设计范式_2026-07-22.md) | Spawn/Join、会话关系、HITL、权限、结果与恢复 |
| [AskUser 工具](调研_AskUser工具_2026-07-28.md) | 结构化提问、Host 呈现和可恢复工具输入 |
| [Agent 系统提示词](调研_Agent系统提示词_2026-07-31.md) | 稳定提示词内核与动态上下文分层 |
| [Agent CLI 图文输入](调研_AgentCLI图文多模态输入_2026-08-01.md) | 附件草稿、资源引用和模型投影 |
| [Agent CLI 技能与 Slash 命令](调研_AgentCLI技能与Slash命令_2026-08-02.md) | 显式技能激活、冻结目录和用户角色投影 |
| [TUI 技术选型](调研_TUI技术选型_2026-08-03.md) | Textual/Rich 选型、视觉与交互设计依据 |
| [全生命周期性能](调研_OURO全生命周期性能_2026-08-30.md) | 启动、运行、Provider、工具、持久化与 TUI 的测量证据 |
| [会话与 TUI 性能审计](OURO_会话与TUI性能审计_2026-09-05.md) | 同步 I/O、订阅、压缩与渲染的实际改良、对照测量及剩余限制 |

调研中的外部项目事实固定在调研日期；其中描述的 OURO 旧缺口、候选数据模型或实施阶段不覆盖现行规格。

## 文件命名

- 调研：`调研_<主题>_YYYY-MM-DD.md`；
- OURO 说明、规范和规划：`OURO_<主题>_YYYY-MM-DD.md`；
- `README.md`、`CHANGELOG.md`、`CONTRIBUTING.md`、`SECURITY.md`、`RELEASING.md`、包内 `README.md`
  和技能 `SKILL.md` 保留生态约定名称。

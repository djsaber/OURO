# OURO 文档索引

文档直接描述当前 OURO 的使用方式、架构和契约。阅读相应任务所需的指南与领域规格即可；
尚未实现的能力在目标设计、规划和未来规格中明确标注。实现事实以代码和可重复测试为依据。

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
| [消息撤销与会话继续](OURO_消息撤销与会话继续.md) | 消息撤销、完整帧续跑、模型切换和协议附件边界 |
| [开发路线图](OURO_开发路线图_2026-09-02.md) | TUI 产品化、Desktop、长期记忆、Workflow 和 SQLite 产品化 |
| [插件开发指南](OURO_插件开发指南_2026-09-02.md) | 插件形态、Manifest、运行时、开发/安装/更新流程和示例 |
| [Goal 插件](OURO_Goal插件_2026-09-02.md) | Goal 状态、命令、工具、预算、自动继续、恢复和安全不变量 |
| [AFK 模式](OURO_AFK模式_2026-09-02.md) | 无人值守审批、输入 resolution、Plan 组合和持久化语义 |
| [订阅支持使用指南](OURO_订阅支持使用指南_ZH_2026-09-19.md) | 浏览器订阅登录、持久凭据、调用续接、注销及验收边界 |
| [OpenAI 预制配置](OURO_OpenAI预制配置说明_ZH_2026-09-19.md) | API 与订阅模型清单、官方依据、推理等级及更新边界 |
| [中文编码准则](OURO_编码准则_ZH_2026-09-02.md) | 实现、测试、重构与评审规范 |
| [English development principles](OURO_编码准则_EN_2026-09-02.md) | English version of the development standard |

## 规范与规划

- [规格索引](../dev/specs/README.md)：现行规格与未来规格；覆盖 Core、Session、Provider、Tool/HITL、Context、Skills、MCP、
  Plugin、Resource/Supervisor、自动任务、Subagent、CLI、TUI 与性能。
- [自动任务规格](../dev/specs/OURO_自动任务规格_2026-09-06.md)：配置、计划、状态机、统计、会话运行与 TUI 验收契约。
- [当前规划](../dev/plans/README.md)：模型订阅认证、Desktop Host、高级智能体能力和性能治理。

规格集中定义领域模型、状态机、不变量、失败/恢复语义、权限边界、数据布局和验证要求。
同一契约由一个领域规格负责，其他文档通过链接引用，避免重复加载和表述分歧。

## 设计

- [自动任务](OURO_自动任务_2026-09-06.md)：执行计划、应用分层、统一运行链路、恢复语义及 TUI 管理。

| 设计 | 范围 |
|---|---|
| [模型订阅认证扩展设计](OURO_模型订阅认证扩展设计_2026-09-16.md) | 认证核心、登录、请求认证、Responses 与 Host 的职责；浏览器订阅及未验证扩展的范围 |
| [TUI 视觉样式与 Markdown 渲染设计](OURO_TUI视觉样式与Markdown渲染设计_2026-09-02.md) | 完整 TuiTheme Token 注入、Markdown/Rich 适配、标识符、动画与验收约束 |

## 文件命名

- OURO 说明、规范和规划使用稳定的 `OURO_<主题>.md` 路径；文件名中的日期不表示功能或验证状态。
- `README.md`、`CHANGELOG.md`、`CONTRIBUTING.md`、`SECURITY.md`、`RELEASING.md`、包内 `README.md`
  和技能 `SKILL.md` 保留生态约定名称。

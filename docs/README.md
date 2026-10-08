# OURO 文档中心

文档以当前代码与可重复测试为依据。使用指南讲操作，架构说明讲职责和取舍；领域规格（Spec）在 `docs/specs/` 独立维护，定义实现与验收共同遵循的精确契约。未实现能力与已完成工作分别存放在规划和归档中。

命令示例默认从仓库根目录执行。`bash` 代码块采用 POSIX Shell 语法，`powershell` 代码块采用 PowerShell 语法；尖括号内容需替换为实际值，`text` 代码块可用于表示语法而非可直接执行的命令。

## 入门与使用

先阅读[项目简介与快速启动](../README.md)和[项目概述](overview.md)，再选择对应任务：

| 任务 | 指南 |
|---|---|
| 启动 TUI / CLI、选择 home 和后端 | [运行与存储](guides/running.md) |
| 配置模型、推理与认证 | [Provider 与 Endpoint](guides/providers.md)、[订阅接入](guides/subscriptions.md) |
| 管理会话、运行和人工交互 | [会话控制](guides/sessions.md)、[撤销与继续](guides/session-history.md) |
| 安装插件、使用 Goal 和 Plan | [插件使用](guides/plugins.md) |
| 工具权限、AGENTS.md 与技能 | [工具、指令与 Skills](guides/skills.md) |
| 连接 MCP Server | [MCP](guides/mcp.md) |
| 配置提示词和本地文件 | [配置文件](guides/configuration.md) |
| 调度后台工作 | [自动任务](guides/automated-tasks.md) |
| 查询 token、费用与计费归属 | [用量统计](guides/usage.md) |
| 备份、选择数据和跨后端传输 | [导入导出](guides/data-transfer.md) |

## 架构与契约

- [架构总览](architecture/README.md)：包职责、调用链、状态与资源所有权。
- [会话连续性与动态环境](architecture/session-continuity.md)：可信事实、当前能力、局部操作承诺与环境生效边界。
- [文件输入架构](architecture/file-input.md)：会话附件、原路径引用与模型内容的准备和持久化。
- [认证架构](architecture/authentication.md)：授权、凭据、请求认证和协议边界。
- [TUI 渲染设计](architecture/tui-rendering.md)：主题、Markdown、缓存与渲染生命周期。
- [正式规格（Specs）索引](specs/README.md)：运行时、会话、Provider、扩展、Host 与性能的权威契约。

## 开发与维护

- [贡献指南](../CONTRIBUTING.md)：环境和质量门禁。
- [中文编码准则](development/coding-guidelines.zh-CN.md) / [English guidelines](development/coding-guidelines.en.md)：实现、测试和重构原则。
- [插件开发指南](development/plugin-development.md)与[可运行示例](../examples/plugins/README.md)。
- [文档规范](development/documentation.md)：分类、命名、内容与验证要求。
- [开发工具](../dev/README.md)：基准与协议探针的独立入口。
- [安全策略](../SECURITY.md)、[发布流程](../RELEASING.md)与[变更记录](../CHANGELOG.md)。

## 规划与记录

[路线图](plans/roadmap.md)描述未来方向；[规划索引](plans/README.md)列出当前待办和进入实施的条件。已完成审查保留在[完成记录](plans/archive/README.md)，用于追溯当时的范围和验证结果，不代替现行规格。

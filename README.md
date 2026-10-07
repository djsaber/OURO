# OURO Agent

OURO 是本地优先的个人智能体运行时，以可恢复会话为中心，提供模型调用、工具执行、人工审批、上下文压缩、插件、Skills、MCP、子智能体和自动任务。CLI 与全屏 TUI 共用应用层和持久化契约，未来 Host 可复用同一边界。

## 开始使用

要求 Python 3.14 或更高版本，以及 `uv`。在仓库根目录执行：

```bash
uv sync --frozen --all-groups
uv run --frozen ouro --home ./.ouro-dev
```

默认启动 TUI。尚未配置真实模型时，内置 Faux Provider 只用于本地演示和测试。
通过 [Provider 配置指南](docs/guides/providers.md)添加模型；浏览器订阅接入见[订阅指南](docs/guides/subscriptions.md)。

单次运行和机器可读输出：

```bash
uv run --frozen ouro run --print --home ./.ouro-dev "hello"
uv run --frozen ouro run --json --home ./.ouro-dev "hello"
uv run --frozen ouro --help
```

`--home` 优先于 `OURO_HOME`，均未设置时使用 `~/.ouro`。会话默认使用文件后端，也可显式选择 SQLite；切换后端不迁移或混读数据。完整说明见[运行与存储选择](docs/guides/running.md)。

## 能力与操作入口

| 任务 | 文档 |
|---|---|
| 选择模型、推理等级与连接认证 | [Provider 与 Endpoint](docs/guides/providers.md)、[订阅接入](docs/guides/subscriptions.md) |
| 运行、审批、继续、分支、撤销与恢复 | [会话控制](docs/guides/sessions.md)、[历史撤销](docs/guides/session-history.md) |
| 工具权限、Agent 指令与 Skills | [工具、指令与 Skills](docs/guides/skills.md) |
| 安装扩展与使用 Goal / Plan | [插件使用](docs/guides/plugins.md)、[插件开发](docs/development/plugin-development.md) |
| 连接外部工具 | [MCP](docs/guides/mcp.md) |
| 创建计划任务 | [自动任务](docs/guides/automated-tasks.md) |
| 查看 token 与费用 | [用量统计](docs/guides/usage.md) |
| 个性化提示词、备份与传输 | [配置文件](docs/guides/configuration.md)、[导入导出](docs/guides/data-transfer.md) |

完整范围、核心概念与边界见[项目概述](docs/overview.md)。Desktop、长期记忆和通用 Workflow 等未来能力见[路线图](docs/plans/roadmap.md)。

## 数据与权限

会话、配置和运行记录保存在本地 home；模型调用和外部工具按配置访问相应服务。环境变量引用与托管凭据各有生命周期，导出的配置和会话可能含私密数据。

工具的 `deny`、审批与工作区路径约束不能替代操作系统沙箱。进程内插件是受信代码，worker 也不提供 OS 隔离。启用扩展前应核对来源和权限，详见[安全策略](SECURITY.md)。

## 开发与文档

- [文档中心](docs/README.md)：按使用任务和文档职责导航。
- [架构说明](docs/architecture/README.md)与[领域规格](docs/specs/README.md)：分层、状态、失败和恢复契约。
- [贡献指南](CONTRIBUTING.md)：开发环境、测试与完整质量门禁。
- [发布流程](RELEASING.md)与[变更记录](CHANGELOG.md)：版本交付要求。
- [开发工具](dev/README.md)：本地基准和显式联网探针。

软件包版本以 `pyproject.toml` 为准。仓库尚未声明分发许可证，发布前需由维护者明确许可条款。

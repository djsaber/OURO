# OURO 执行计划索引

本目录保存当前与已完成工作的执行计划。计划以依赖、任务、验收和退出条件组织；完成计划可保留最终状态与
验证证据，但不作为逐日执行日志。

## 计划关系

```text
architecture-foundation-refactor -> desktop-host
architecture-foundation-refactor -> advanced-agent-capabilities
personal-workflow-foundation -> desktop-host
personal-workflow-foundation + connectivity-extensions -> advanced-agent-capabilities
connectivity-extensions -> plugin-observers-prompt-policy
```

| 计划 | 目标 | 前置条件 |
|---|---|---|
| [架构底层重构（已完成）](architecture-foundation-refactor.md) | 持久化版本、Session 扩展状态、资源上界、Run 状态权威与编排收敛 | 当前 Runtime/Session/Plugin/Tool 契约 |
| [个人工作流基础](personal-workflow-foundation.md) | ResourceService、RuntimeSupervisor、配置门面 | 当前 Session/Runtime/Config 契约 |
| [连接与扩展（已完成）](connectivity-extensions.md) | 插件底层架构收敛与 MCP 生命周期 | 配置门面、MCP/Supervisor 边界 |
| [插件观察器与提示策略（已完成）](plugin-observers-prompt-policy.md) | 非阻塞 typed Observer 与 fail-closed Prompt Policy | 连接与扩展 B1-B8、不可变 ExtensionCatalog |
| [高级智能体能力](advanced-agent-capabilities.md) | 长期记忆与 durable Task/Workflow；工作流 C 子智能体已完成 | ResourceService、Supervisor 和稳定 Host 控制面 |
| [子智能体（已完成）](subagents.md) | 工作流 C 的实现记录：AgentDefinition、durable Delegation、控制工具、事件树、HITL、权限和恢复 | Session/Run、Tool/HITL、config、ExtensionCatalog、ResourceService、RuntimeSupervisor |
| [TUI 子智能体会话视图（已完成）](tui-subagent-session-views.md) | Host-neutral 会话观察契约与 TUI 主/多 child 只读连续切换 | 子智能体、RuntimeSupervisor、TUI 实施基线 |
| [桌面 Host](desktop-host.md) | 基于 application DTO 的本地桌面端 | 配置门面、Supervisor；资源视图依赖 ResourceService |
| [运行热路径性能改进（已完成）](runtime-performance-hot-paths.md) | 流式降频、阻塞事务 worker 边界、取消快路径、TUI 有界帧消费与批提交 | 当前 Runtime/Session/TUI/Plugin 契约与性能审查证据 |

## 使用规则

- 开始任务前读取对应规格和编码准则。
- 一次实现只推进一个可独立验证的里程碑。
- 结构重构和功能行为尽量分开提交。
- 每个里程碑先补失败/边界测试，再实现，再更新权威规格。
- 发现依赖条件未成立时调整计划，不在当前模块中临时模拟缺失服务。
- 完成计划的标准是所有退出条件有当前代码和测试证据，不是 checklist 被手工勾选。

## 全局质量门槛

```bash
python -m pytest
python -m ruff check ouro tests
python -m ruff format --check ouro tests
python -m mypy ouro
python -m compileall -q ouro
git diff --check
```

所有计划还必须通过架构依赖测试、敏感信息审查、取消/资源释放检查和文档链接检查。

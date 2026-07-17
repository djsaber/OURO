# OURO 执行计划索引

本目录保存待实施工作的执行计划。计划以依赖、任务、验收和退出条件组织，不保存执行日志、日期化进度
快照或方案修订记录。

## 计划关系

```text
personal-workflow-foundation
   |             |
   v             v
desktop-host   connectivity-extensions
   \             /
    v           v
 advanced-agent-capabilities
```

| 计划 | 目标 | 前置条件 |
|---|---|---|
| [个人工作流基础](personal-workflow-foundation.md) | ResourceService、RuntimeSupervisor、配置门面 | 当前 Session/Runtime/Config 契约 |
| [连接与扩展](connectivity-extensions.md) | 插件底层架构收敛（MCP 与首版闭环已完成） | 配置门面、现有 MCP/Supervisor 边界 |
| [桌面 Host](desktop-host.md) | 基于 application DTO 的本地桌面端 | 配置门面、Supervisor；资源视图依赖 ResourceService |
| [高级智能体能力](advanced-agent-capabilities.md) | 长期记忆、计划与子智能体 | ResourceService、Supervisor 和稳定 Host 控制面 |

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

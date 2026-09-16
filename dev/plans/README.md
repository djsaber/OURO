# OURO 规划索引

> 索引基线：2026-09-16

本目录只保存待实施或验证的规划，定义目标、前置条件和验收要求。当前行为以
[规格索引](../specs/README.md)为准；版本能力摘要见 [CHANGELOG.md](../../CHANGELOG.md)。

## 当前规划

| 规划 | 范围 | 关键前置 |
|---|---|---|
| [模型订阅认证实施计划](OURO_模型订阅认证实施计划_2026-09-16.md) | 接入验证、认证基础、协议适配、Host 闭环与发布验收 | [目标设计](../../docs/OURO_模型订阅认证扩展设计_2026-09-16.md)、[未来规格](../specs/OURO_模型订阅认证规格_2026-09-16.md) |
| [桌面 Host](OURO_桌面Host规划_2026-09-02.md) | 本地桌面会话、运行、资源、diff、设置与发布 | Application services/DTO、RuntimeSupervisor、ResourceService |
| [高级智能体能力](OURO_高级智能体能力规划_2026-09-02.md) | 长期记忆、Durable Task/Workflow 及其与 Goal/Delegation 的组合 | 当前 Session/Run、Context、资源、子智能体和 Host 控制面 |
| [性能优化](OURO_性能优化规划_2026-09-02.md) | 真实终端验收、长历史分页、Context/Compaction 与资源投影 | 当前性能规格与可重复测量证据 |

Desktop Host 等长期规划的总体顺序和跨能力边界见[开发路线图](../../docs/OURO_开发路线图_2026-09-02.md)，目标契约见
[未来平台规格](../specs/OURO_未来平台规格_2026-09-02.md)。

## 使用规则

- 开始实现前读取对应现行规格、目标规格与编码准则。
- 一次只推进一个可独立验证的里程碑；先确定责任层和失败语义，再写 Host 或 adapter。
- 新行为包含正常、错误、取消、恢复、重复执行和并发边界测试。
- 发现前置条件不成立时调整规划，不在当前模块模拟缺失服务。
- 完成后把最终行为写入现行规格并从规划删除，不追加长期维护的实施日志。

## 全局质量门槛

```bash
uv run --frozen pytest --cov=ouro --cov-report=term-missing
uv run --frozen ruff check ouro tests
uv run --frozen ruff format --check ouro tests
uv run --frozen mypy ouro
uv run --frozen python -m compileall -q ouro
git diff --check
```

所有规划还必须通过架构依赖测试、敏感信息审查、取消/资源释放检查和文档链接检查。

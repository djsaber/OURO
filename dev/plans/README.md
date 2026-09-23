# OURO 规划索引

本目录保存任务范围、待办目标、前置条件和验收要求。当前行为以
[规格索引](../specs/README.md)为准；能力摘要见 [CHANGELOG.md](../../CHANGELOG.md)。

## 当前规划

| 规划 | 范围 | 关键前置 |
|---|---|---|
| [轻量化重构优化](OURO_轻量化重构优化计划_2026-09-24.md) | 框架、TUI、性能、复杂度与工程验收 | 当前代码、架构与性能证据 |
| [模型订阅认证验证与扩展](OURO_模型订阅认证实施计划_2026-09-16.md) | 设备码、模型能力与跨平台验收 | [设计](../../docs/OURO_模型订阅认证扩展设计_2026-09-16.md)、[规格](../specs/OURO_模型订阅认证规格_2026-09-16.md) |
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
- 现行规格独立描述有效行为与验收依据；长期规划只保留待办和扩展条件，不保存实施日志或版本对比。
- 任务计划可标记验收状态，但不能把尚未验证的能力标记为完成。

## 全局质量门槛

阶段推进先执行新增行为的针对性测试、受影响模块与架构边界回归；测试范围依据调用链和依赖变化确定。
全量测试及全仓覆盖率留给里程碑整合、发布验收或影响范围无法可靠界定的变更，不要求每一步重复执行。
提交与交付说明实际测试范围及未执行项，不能把定向测试描述为全量通过。

阶段检查：

```bash
uv run --frozen pytest <targeted-tests-and-affected-modules>
uv run --frozen ruff check ouro tests
uv run --frozen ruff format --check <changed-python-files>
uv run --frozen mypy ouro
uv run --frozen python -m compileall -q ouro
git diff --check
```

整合与发布检查：

```bash
uv run --frozen pytest --cov=ouro --cov-report=term-missing
uv run --frozen ruff check ouro tests
uv run --frozen ruff format --check ouro tests
uv run --frozen mypy ouro
uv run --frozen python -m compileall -q ouro
git diff --check
```

所有规划还必须通过架构依赖测试、敏感信息审查、取消/资源释放检查和文档链接检查。

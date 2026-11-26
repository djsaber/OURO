# OURO 开发路线图

## 近期：Host 产品面

### TUI 产品化增强

TUI 的待办集中在真实终端验收、持续负载下的响应性和发布级交互体验。响应门槛见[性能规格](../specs/performance.md#io-所有权与响应性验证)。

范围：

- 在两种后端重复验证近期窗口冷进入、自动补页和缓存重进的成本与资源界限；
- 在跨平台重复终端场景中验证草稿、焦点、队列和会话切换；
- 在 Windows Terminal、常见 POSIX 终端和窄屏环境建立首反馈、持续流、长代码块和长历史响应性基线；
- 验证持续写入与慢消费者情况下的视口锚定、持久追赶和有界内存；
- 补齐发布级键盘兼容、颜色降级、复制与链接体验。

验收记录应注明终端、平台与负载，并逐项对照响应门槛。TUI 负责渲染和输入映射，会话事实读取和状态转换由 Application 与存储层负责。

### Desktop Host

目标：为现有 application 能力提供本地桌面界面。

范围：

- session catalog、搜索、分支和回放；
- 流式运行、工具批次、审批、AskUser 和 abort；
- 文件 diff、资源预览和设置；
- 通过 RuntimeSupervisor 订阅后台运行。

完成条件：Desktop 只依赖内聚 application service、DTO 与通知，不复制运行规则或直接解析本地文件/SQLite。

## 后续：高级个人智能体能力

### 长期记忆

- 记忆候选提取、用户确认和来源追踪；
- 可查看、编辑、禁用和删除；
- 检索结果与上下文预算协同；
- 防止把工具输出或外部提示自动提升为可信记忆。

### Durable Task/Workflow

- 复用 Goal 的显式创建、Run continuation、状态、预算和控制权仲裁；
- 自动任务负责日历/间隔触发；依赖任务图有独立需求时，再定义 durable workflow 记录与状态；
- ready task 选择、依赖、重试与跨 Run 调度；
- 与当前 durable Delegation 建立显式关联，不复制 Agent 状态机；
- 用户可见的计划进度、预算与控制入口。

多任务/工作流复用子智能体和单 Goal 生命周期，不把 AgentTree 或 Goal state
冒充通用任务图。

### SQLite 产品化

文件与 SQLite 实现同一 SessionStore contract；SQLite 使用 adapter 内部事务和索引，文件到 SQLite 的
导入 API 会验证并整体回滚。CLI 通过命令参数与环境变量选择后端，不隐式迁移。产品化目标包括：

- 提供 Desktop 后端选择，以及 CLI/Desktop 迁移预检、进度和备份体验；
- 在真实长会话数据上建立查询、写入、WAL 维护和恢复基准；
- 完善损坏诊断，复用[数据升级与备份恢复流程](../guides/data-upgrade.md)；
- 保留文件后端作为可读实现和显式导出来源。

不得为两种实现再增加通用 ORM、UoW 或第二套领域恢复规则。

## 跨阶段质量门槛

每项能力必须满足：

- 行为、错误、取消、恢复和并发语义有测试；
- 不新增 core 对外层的依赖；
- 快速 `tests/architecture + tests/core` gate 通过，完整跨平台 suite 与 branch coverage 门槛通过；
- 敏感数据不进入日志、事件或公开 DTO；
- 资源有明确关闭路径；
- CLI、TUI、Desktop 和协议适配器不复制 application 规则；
- README、项目概述和开发架构与代码保持一致。

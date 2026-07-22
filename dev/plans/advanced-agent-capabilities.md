# 高级智能体能力计划

状态：未开始

## 目标

在资源身份、跨 Run 生命周期和稳定 Host 控制面就绪后，实现可审计的长期记忆、durable 计划和子智能体。
这些能力必须增加用户控制，而不是扩大不可见自治。

## 前置条件

- ResourceService 能保存结果引用和可达性。
- RuntimeSupervisor 能管理并发、订阅、取消和关闭。
- Desktop 或等价 Host 能展示候选、进度和控制项。
- ContextBuilder 提供明确的外部上下文贡献边界。

## 工作流 A：长期记忆

### A1. 领域与存储

- [ ] 定义 MemoryRecord：identity、content、source refs、status、confidence、timestamps、revision。
- [ ] 定义 candidate/confirmed/disabled/deleted 状态。
- [ ] 建立 canonical store 与可重建检索索引。
- [ ] 删除源 Session 时保持引用一致性或明确脱链策略。

### A2. 候选提取

- [ ] 仅从明确用户事实、偏好和长期工作上下文生成候选。
- [ ] 工具输出、外部文档和 system/tool prompt 不自动成为可信记忆。
- [ ] 记录提取模型、source messages 和理由。
- [ ] 去重和冲突只生成 diagnostic/候选，不静默覆盖。

### A3. 用户控制

- [ ] list/show/confirm/edit/disable/delete。
- [ ] Desktop 展示来源和修改历史。
- [ ] 高影响类别默认要求确认。

### A4. 检索与上下文

- [ ] 检索 query、limit、score 和过滤契约。
- [ ] 检索结果占用 ContextView token budget。
- [ ] 注入内容明确标识为 memory context，不伪装用户消息。
- [ ] 记录每次使用的 memory ids 以支持解释和删除影响分析。

## 工作流 B：Durable plan/goal

### B1. 任务模型

- [ ] Goal、Task、dependency、status、owner、budget、result refs。
- [ ] 状态转换和循环依赖校验。
- [ ] 计划更新具备乐观版本或事务冲突语义。

### B2. 执行编排

- [ ] Task 到 Run 的显式关联。
- [ ] ready task 选择、公平调度和全局/goal 并发上限。
- [ ] 完成、失败、取消和重试传播规则。
- [ ] 重启后从 durable task/run 状态恢复，不依赖内存队列。

### B3. Host 控制

- [ ] 展示 task tree、状态、预算、来源和结果。
- [ ] 用户可暂停、恢复、取消和调整优先级。
- [ ] 自动计划变更形成 diff 并可审计。

## 工作流 C：子智能体

规范性契约见[子智能体规格](../specs/subagents.md)，可执行里程碑、故障矩阵与退出条件见
[子智能体实施计划](subagents.md)。本节只保留高级能力之间的总体集成门槛。

### C1. 能力与权限

- [ ] 子智能体 profile 明确 model、tools、workspace、permissions 和预算。
- [ ] 权限只能等于或小于父级授权，不能自行提升。
- [ ] 每个子任务有独立 Session/Run identity 和 DelegationRecord；不复用 branch lineage 表达委派。

### C2. 并发与结果

- [ ] Supervisor 管理子 Run，不递归持有 RuntimeService 内部状态。
- [ ] 结果通过 typed DelegationResult、message/resource refs 和 exact-once delivery 归并。
- [ ] partial、failed、aborted 子任务对父任务有明确策略。
- [ ] 取消向整个子树传播并有限等待。
- [ ] Host 可订阅 child 消息流与根 Agent tree，并准确路由 child HITL。

### C3. 上下文隔离

- [ ] 子智能体只获得任务所需上下文和资源引用。
- [ ] 不复制完整父 Session history 作为默认行为。
- [ ] 返回内容视为不可信协作者输出，仍经过父级验证。

## 测试矩阵

- Memory 提取污染、来源删除、冲突、编辑和禁用；
- 检索预算、删除后不再注入、敏感内容过滤；
- Task dependency、并发冲突、重复调度、重启恢复和取消传播；
- 子智能体权限越权、预算耗尽、partial result 和树取消；
- Host 断开/重连后进度一致；
- 所有结果和资源无悬空 identity。

## 风险与控制

- 记忆污染：候选与确认分离，来源全程可追踪。
- 自治失控：预算、权限、并发和用户控制都是强制契约。
- 任务系统过度设计：只支持当前计划/子任务用例，不预建通用工作流 DSL。
- 上下文爆炸：资源引用和检索替代完整历史复制。
- 状态组合复杂：先完成 durable plan，再接子智能体执行。

## 退出条件

- 用户能解释、修改和删除每条记忆。
- 每个 Task/子 Run 有 durable identity、预算和结果。
- 重启、取消和失败不会产生重复执行或孤儿任务。
- 权限和 workspace 隔离有负向测试。
- 项目概述、架构和目标规格更新为实际状态后才宣称能力可用。

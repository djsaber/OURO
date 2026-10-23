# 高级智能体能力计划

> 文档类型：未来规划
> 范围：长期记忆产品与依赖工作流插件；原生任务调度、插件端口和子智能体的有效契约由领域规格定义。

## 目标

以独立插件实现可审计的长期记忆和依赖工作流，并通过公开模型、工具、Agent、调度及服务端口组合原生能力。
这些能力必须增加用户控制，而不是扩大不可见自治。

## 前置条件

- ResourceService 能保存会话拥有的结果文件与引用。
- RuntimeSupervisor 能管理并发、订阅、取消和关闭。
- Desktop 或等价 Host 能展示候选、进度和控制项。
- ContextBuilder 提供明确的外部上下文贡献边界。
- 插件拥有私有业务存储，通过具名服务导出共享能力，不访问其他插件私有目录。
- Task 的计划、领取、执行与结果事实由[自动任务规格](../specs/automated-tasks.md)定义；插件不得复制该账本。
- Driver 与 checkpoint、等待恢复、取消和预算使用[运行时规格](../specs/runtime.md)的公开契约。

## 工作流 A：长期记忆

### A1. 领域与存储

- [ ] 定义 MemoryRecord：identity、content、来源 Session/Message、status、confidence、提取时间、revision 和用户修改记录。
- [ ] 定义 candidate/confirmed/disabled/deleted 状态。
- [ ] 在记忆插件私有目录建立 canonical store 与可重建检索索引。
- [ ] 删除源 Session 时保持引用一致性或明确脱链策略。
- [ ] 通过声明依赖的具名服务共享检索，消费者的过滤器注册随 owner 关闭而撤销。

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

## 工作流 B：依赖工作流插件

### B1. 业务模型

- [ ] Workflow、节点依赖、业务状态、owner、预算及原生 Task/Run/Delegation 引用。
- [ ] 状态转换和循环依赖校验。
- [ ] 工作流更新具备明确的并发冲突语义，不复制原生 Task 修订与执行状态。

### B2. 执行编排

- [ ] 就绪节点通过公开 Agent、Driver 或可调度服务端口执行，保存返回的原生身份。
- [ ] 定义工作流自身的依赖和并发策略，实际派发服从原生共享容量及权限上限。
- [ ] 定义完成、失败、取消和重试传播规则，未知外部结果不自动重放。
- [ ] 重启后按持久业务图与原生执行事实继续，使用 Driver checkpoint 承接原生等待，不新增通用操作日志。
- [ ] 就绪节点、依赖与重试决策持久化，不以递归调用 RuntimeService 或拼接 prompt 代替业务图。

### B3. Host 控制

- [ ] 展示 task tree、状态、预算、来源和结果。
- [ ] 用户可暂停、恢复、取消和调整优先级。
- [ ] 自动计划变更形成 diff 并可审计。

## 与现有子智能体的组合边界

工作流节点可以通过稳定 `delegation_id` 关联 `DelegationRecord`，但不得复制 AgentDefinition、child Run、
AgentTree、权限继承、HITL 或结果唯一交付状态。工作流只判断业务依赖和何时提交派遣；派遣后的执行事实继续
由子智能体 Session/Run 与[子智能体规格](../specs/subagents.md)拥有。日历与间隔触发使用原生 Task，
同一 home 的执行所有权、领取与中断仍由原生调度器处理。

## 测试矩阵

- Memory 提取污染、来源删除、冲突、编辑和禁用；
- 检索预算、删除后不再注入、敏感内容过滤；
- Workflow dependency、并发冲突、重复提交、重启恢复和取消传播；
- 工作流关联子智能体后的权限越权、预算耗尽、partial result、重复交付和取消传播；
- 插件禁用或缺失、显式更新与必要数据维护不影响原生运行事实的读取和管理；
- Host 断开/重连后进度一致；
- 所有结果和资源无悬空 identity。

## 风险与控制

- 记忆污染：候选与确认分离，来源全程可追踪。
- 自治失控：预算、权限、并发和用户控制都是强制契约。
- 任务系统过度设计：只支持当前计划/子任务用例，不预建通用工作流 DSL。
- 上下文爆炸：资源引用和检索替代完整历史复制。
- 状态组合复杂：Workflow、Task、Goal 与 Delegation 保持不同 identity，只增加显式关联，不重写各自状态机。

## 退出条件

- 用户能解释、修改和删除每条记忆。
- 每个工作流节点保存清楚的业务身份、预算及实际使用的 Task/Run/Delegation 引用。
- 重启、取消和失败不会产生重复执行或孤儿任务。
- 权限和 workspace 隔离有负向测试。
- 项目概述、架构和目标规格更新为实际状态后才宣称能力可用。

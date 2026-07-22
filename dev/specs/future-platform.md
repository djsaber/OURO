# 个人智能体平台目标规格

状态：目标

## 目标

在保持本地优先、durable Run 和清晰依赖方向的前提下，为桌面 Host 和长期记忆/任务计划提供共同目标契约。
本规格不表示这些剩余能力已经实现。ResourceService、RuntimeSupervisor、已实现插件和子智能体已经分别转入
[当前监督规格](resources-supervisor.md)、[已实现插件规格](plugin-system.md)和[当前子智能体规格](subagents.md)，
不再属于本目标规格。

## Desktop Host

- 只依赖 application service/DTO 和 RuntimeSupervisor 订阅。
- 覆盖 catalog、搜索、运行流、工具批次、审批、AskUser、diff、资源、配置和诊断。
- UI 关闭不丢失 canonical 状态。
- 大事件流和消息历史使用增量加载，不全量阻塞 UI。

## 长期记忆

- 记忆含来源 Session/Message、提取时间、内容、状态和用户修改记录。
- 自动提取只产生候选；高影响记忆需用户确认。
- 用户可查看、编辑、禁用和删除。
- 检索结果进入 ContextBuilder 的显式边界并占用预算。
- 外部工具输出和提示注入不能自动升级为可信记忆。

## Durable 计划

- durable goal/task tree 保存 owner、状态、依赖、预算和结果引用。
- ready task 选择、依赖、重试和跨 Run 状态必须 durable，不以递归 RuntimeService 或裸 prompt 拼接代替任务模型。
- Goal/Task 可关联当前 `DelegationRecord`，但二者保持不同 identity；不能把已实现的 Agent tree 反向解释成
  尚未实现的通用任务调度器。
- Host 展示可验证进度、预算和用户控制入口。

## 非功能门槛

- 新协议和 Host 不改变 core 的依赖方向。
- 所有长生命周期资源支持关闭、取消和超时。
- canonical write、索引、缓存和 UI projection 继续分离。
- 敏感数据不进入事件、日志、错误和公开 DTO。
- 每项能力包含正常、失败、恢复、重复执行和并发测试。

## 对应计划

- [个人工作流基础计划](../plans/personal-workflow-foundation.md)
- [连接与扩展计划](../plans/connectivity-extensions.md)
- [桌面 Host 计划](../plans/desktop-host.md)
- [高级智能体能力计划](../plans/advanced-agent-capabilities.md)
- [子智能体实施计划](../plans/subagents.md)

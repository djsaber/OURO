# 个人智能体平台目标规格

状态：目标

## 目标

在保持本地优先、durable Run 和清晰依赖方向的前提下，为桌面 Host、完整扩展、长期记忆和子智能体
提供共同目标契约。本规格不表示这些剩余能力已经实现。ResourceService、RuntimeSupervisor 与 Application
配置门面已转入[当前规格](resources-supervisor.md)和[Provider 与配置规格](providers-config.md)，不再属于本目标规格。

## 完整插件

完整契约见[插件系统目标规格](plugin-system.md)。本平台规格只保留以下总体约束：插件生命周期不得改变
core 依赖方向和 durable Run 语义；同进程插件明确标注 full trust，worker 只承诺崩溃/依赖隔离而不冒充
OS 沙箱；任意 Host 都通过 application facade/DTO 管理插件。

## Desktop Host

- 只依赖 application facade/DTO 和 RuntimeSupervisor 订阅。
- 覆盖 catalog、搜索、运行流、工具批次、审批、AskUser、diff、资源、配置和诊断。
- UI 关闭不丢失 canonical 状态。
- 大事件流和消息历史使用增量加载，不全量阻塞 UI。

## 长期记忆

- 记忆含来源 Session/Message、提取时间、内容、状态和用户修改记录。
- 自动提取只产生候选；高影响记忆需用户确认。
- 用户可查看、编辑、禁用和删除。
- 检索结果进入 ContextBuilder 的显式边界并占用预算。
- 外部工具输出和提示注入不能自动升级为可信记忆。

## 计划与子智能体

- durable goal/task tree 保存 owner、状态、依赖、预算和结果引用。
- 子智能体继承明确的工具权限、workspace 和模型预算。
- 并发、取消、失败传播和结果归并由 application/Supervisor 编排。
- Host 展示可验证进度和控制入口。
- 不以递归 RuntimeService 调用或裸 prompt 拼接代替任务模型。

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

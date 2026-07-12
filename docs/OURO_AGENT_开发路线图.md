# OURO Agent 开发路线图

本文只描述尚未实现的能力和进入实现阶段的条件。当前能力以项目概述和开发架构为准。
具体任务、依赖和退出条件见 [执行计划索引](../dev/plans/README.md)。

## 推进原则

- 每项能力先定义 application 用例、领域记录和失败语义，再实现 Host 或具体协议。
- canonical 数据与可重建投影分离；任何新索引都不能成为核心写事务的单点故障。
- 新外部依赖必须停留在适配层，并具备超时、取消、资源释放和可诊断错误。
- 不以“未来可能需要”为理由引入空接口、通用 manager 或深层依赖注入。
- 每一项交付必须包含正常、错误、恢复和并发边界测试，并同步更新权威文档。

## 近期：完成个人工作流基础

### ResourceService

目标：让附件和工具生成文件拥有稳定资源身份，而不是在消息中传播裸路径。

范围：

- ResourceRecord、内容摘要、媒体类型和来源；
- 导入、引用、预览、导出和删除；
- session branch 的资源引用复制；
- canonical 资源记录与可重建索引；
- 路径逃逸、符号链接、重复导入和清理失败测试。

完成条件：消息只保存 resource id；分支、删除和恢复不会制造悬空引用。

### RuntimeSupervisor

目标：在不改变单 session 单 active run 约束的前提下管理跨会话后台运行。

范围：

- 进程内 run registry、订阅和生命周期；
- Host 断线后的运行继续或取消策略；
- 跨 session 并发上限和公平调度；
- 关闭时的协作式取消和资源释放。

完成条件：CLI 与未来 Desktop 可观察同一 durable run，Host 生命周期不再隐式等于 Run 生命周期。

### Application 配置门面

目标：让非 CLI Host 通过稳定 DTO 管理 provider、endpoint、权限、压缩和 Skills 配置。

范围：

- 查询与命令 DTO；
- 凭据安全输出；
- 原子校验和错误诊断；
- 配置变更后的 runtime 生效规则。

完成条件：Desktop 不直接依赖 `ProviderConfigManager` 等基础设施 manager。

## 中期：连接与扩展

### MCP 客户端

目标：把远程或本地 MCP server 的工具适配到 OURO Tool 契约。

范围：

- stdio 与 HTTP transport；
- 握手、能力协商、认证和工具发现；
- 超时、取消、重连、日志和 server 状态；
- schema 转换与错误隔离。

完成条件：MCP 协议对象只存在于 `ouro.mcp`，core 和 application 只看到 OURO Tool/Result。

### 完整插件生命周期

目标：提供可审计的插件发现、加载、启停和升级。

范围：

- manifest 与 entrypoint 加载；
- 来源、信任和权限确认；
- contribution 冲突、排序和失败隔离；
- 安装、升级、回滚和诊断。

完成条件：插件不能绕过工具权限、持久化事务或 Hook 失败边界。

### Desktop Host

目标：为现有 application 能力提供本地桌面界面。

范围：

- session catalog、搜索、分支和回放；
- 流式运行、工具批次、审批、AskUser 和 abort；
- 文件 diff、资源预览和设置；
- 通过 RuntimeSupervisor 订阅后台运行。

完成条件：Desktop 只依赖 application facade/DTO，不复制运行规则或直接解析本地文件。

## 后续：高级个人智能体能力

### 长期记忆

- 记忆候选提取、用户确认和来源追踪；
- 可查看、编辑、禁用和删除；
- 检索结果与上下文预算协同；
- 防止把工具输出或外部提示自动提升为可信记忆。

### 计划与子智能体

- durable task/goal 记录、状态和预算；
- 子任务并发、取消、结果归并和失败传播；
- 工具与模型配额继承；
- 用户可见的进度与控制入口。

只有 RuntimeSupervisor 和资源身份稳定后才进入该能力，避免用临时进程和裸文本拼接模拟任务系统。

### 持久化规模化

继续使用本地文件后端，直到真实数据证明查询、事务或维护成本成为瓶颈。若引入 SQLite：

- `SessionStore` 契约保持稳定；
- 提供显式导入、验证和回滚工具；
- 不让 ORM 或表结构进入 core；
- 文件后端仍作为可读导出格式或受支持实现。

## 跨阶段质量门槛

每项能力必须满足：

- 行为、错误、取消、恢复和并发语义有测试；
- 不新增 core 对外层的依赖；
- 敏感数据不进入日志、事件或公开 DTO；
- 资源有明确关闭路径；
- CLI、Desktop 和协议适配器不复制 application 规则；
- README、项目概述和开发架构与代码保持一致。

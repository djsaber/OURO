# OURO Agent 开发路线图

本文只描述尚未实现的能力和进入实现阶段的条件。当前能力以项目概述和开发架构为准。
具体任务、依赖和退出条件见 [执行计划索引](../dev/plans/README.md)。

## 推进原则

- 每项能力先定义 application 用例、领域记录和失败语义，再实现 Host 或具体协议。
- canonical 数据与可重建投影分离；任何新索引都不能成为核心写事务的单点故障。
- 新外部依赖必须停留在适配层，并具备超时、取消、资源释放和可诊断错误。
- 不以“未来可能需要”为理由引入空接口、通用 manager 或深层依赖注入。
- 每一项交付必须包含正常、错误、恢复和并发边界测试，并同步更新权威文档。

## 近期：连接与扩展

个人工作流基础（ResourceService、RuntimeSupervisor、Application 配置门面）、后端中立 SessionRepository、
SQLite adapter、类型化 Host 事件、LocalApplication 生命周期，以及插件首版管理闭环、Run continuation、工具执行
上下文和限制性 tool policy 控制面已经完成。

插件系统已完成 B1-B8 八个里程碑：删除 workspace/managed 等插件 scope，只保留 builtin/user；Manifest/state
直接升级并拒绝旧格式；配置改为 immutable revision；权限集合封闭；插件 MCP 随启用启动；Runtime 改为持有
不可变 `ExtensionCatalogLease`，旧/新 Run 可并存，插件不再修改 Host 全局 registry，也不存在全局 Catalog barrier。
enable/configure/select/restart 已统一为“构建候选 generation closure 与 Catalog、expected-state 提交、pointer swap、
旧代独立 drain”，Background Service 串行转交；builtin/user dependency lock、blocked 恢复和统一 observed state 也已
落地。十种 Manifest capability 现由封闭 `ContributionTypeAdapter` 清单统一绑定 SDK/worker、准备、契约、资源与
Catalog 投影。Worker 严格协议/进程树关闭、in-process 代际私有 module、统一 Run-local Skill Catalog 与 Plugin MCP
生命周期均已收口。稳定执行契约现可跨进程重建 waiting/recoverable Run，并在插件更新后从保留的旧 digest、配置和
依赖构建非当前 Catalog。最小 scaffold、显式本地 reload、checkout 前 Git tree 边界、四类社区兼容报告、三种 runtime
可运行示例和[插件开发指南](OURO_插件开发指南.md)也已落地。后续只由真实插件需求驱动新增扩展点，不预建平台能力。

Marketplace、签名/publisher trust root、worker OS sandbox、每插件依赖环境和通用 session-scoped KV/CAS 都不是
当前目标；只有真实插件提出不可由现有边界满足的需求时才重新评估。

### TUI Host

目标：以终端原生布局复用 application 的类型化会话、运行和配置能力。

范围：

- session catalog、分页历史与 activity 面板；
- 流式运行、工具批次、审批、AskUser、steer/follow-up 和 abort；
- 配置与 MCP 诊断；
- 通过 RuntimeSupervisor 的有界订阅和 cursor 重同步处理慢消费者。

完成条件：TUI 只负责渲染和输入映射，不解析 Session 文件/SQLite，不从原始 event payload 重建业务状态。

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

### Durable Goal/Task

- durable task/goal 记录、状态和预算；
- ready task 选择、依赖、重试与跨 Run 调度；
- 与当前 durable Delegation 建立显式关联，不复制 Agent 状态机；
- 用户可见的计划进度、预算与控制入口。

子智能体首版已经完成；本阶段只实现尚未提供的通用 Goal/Task，不把 AgentTree 冒充任务系统。

### SQLite 产品化

文件与 SQLite 已通过同一 SessionStore contract；SQLite 使用 adapter 内部事务和索引，文件到 SQLite 的
导入 API 会验证并整体回滚。CLI Host 已支持命令参数与环境变量选择后端，且不会隐式迁移。产品化仍需：

- 提供 Desktop 后端选择，以及 CLI/Desktop 迁移预检、进度和备份体验；
- 在真实长会话数据上建立查询、写入、WAL 维护和恢复基准；
- 定义版本升级、备份恢复和损坏诊断流程；
- 保留文件后端作为可读实现和显式导出来源。

不得为两种实现再增加通用 ORM、UoW 或第二套领域恢复规则。

## 跨阶段质量门槛

每项能力必须满足：

- 行为、错误、取消、恢复和并发语义有测试；
- 不新增 core 对外层的依赖；
- 敏感数据不进入日志、事件或公开 DTO；
- 资源有明确关闭路径；
- CLI、TUI、Desktop 和协议适配器不复制 application 规则；
- README、项目概述和开发架构与代码保持一致。

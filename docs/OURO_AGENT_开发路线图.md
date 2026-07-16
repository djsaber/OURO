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
SQLite adapter、类型化 Host 事件、LocalApplication 生命周期和插件 contribution 安全边界已完成并转入当前规格。

### 完整插件生命周期

目标：提供可审计的插件发现、加载、启停和升级。

范围：

- 安装器/包管理器与 entrypoint 生命周期；
- 来源证明、用户信任确认和权限变更 UI；
- contribution 冲突、排序和失败隔离；
- 安装、升级、回滚和诊断。

现有 manifest loader 已禁止来源自升、校验 capability/permission、限制 Hook/command 超时并原子注册贡献；
剩余工作不应重新实现这些底层规则。完成条件：安装、升级、禁用和权限撤销都能撤回贡献，插件不能绕过
工具权限、持久化事务、脱敏或 Hook 失败边界。

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

### 计划与子智能体

- durable task/goal 记录、状态和预算；
- 子任务并发、取消、结果归并和失败传播；
- 工具与模型配额继承；
- 用户可见的进度与控制入口。

只有 RuntimeSupervisor 和资源身份稳定后才进入该能力，避免用临时进程和裸文本拼接模拟任务系统。

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

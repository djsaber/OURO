# 个人智能体平台目标规格

状态：目标

## 目标

在保持本地优先、durable Run 和清晰依赖方向的前提下，为多会话后台运行、资源、桌面 Host、完整扩展、
长期记忆和子智能体提供共同目标契约。本规格不表示这些能力已经实现。

## ResourceService

- `ResourceRecord` 使用稳定 id、digest、media type、size、来源和创建时间。
- Message 只保存 resource id，不以裸绝对路径作为跨会话身份。
- 导入执行路径安全检查、内容去重和原子发布。
- Branch 复制引用关系，不复制不受控路径。
- 删除 Session 时资源清理具备引用计数或可重建可达性分析。
- 资源索引失败不破坏 canonical resource record。

## RuntimeSupervisor

- 管理进程内跨 Session active Run registry；单 Session 仍只有一个 active Run。
- 提供 subscribe/unsubscribe、status、abort 和 shutdown。
- Host 断线不隐式终止 durable Run；策略必须显式配置。
- 支持全局并发上限、公平调度和协作式关闭。
- 重启后的事实来自 SessionStore，不依赖内存 registry。

## Application 配置门面

- Provider、Endpoint、权限、压缩、预算和 Skills 暴露类型化 query/command DTO。
- mutation 先完整校验再原子写入。
- credential DTO 只暴露引用和配置状态。
- Desktop、未来 API 和 CLI 可复用同一用例，不直接把 config manager 当 UI service。

## 完整 MCP

- 支持 stdio 和 HTTP transport；SSE 能力按协议要求接入。
- 覆盖握手、版本/能力协商、认证、工具发现、调用、取消、重连和 stop。
- 每个 server 有 durable config、运行状态和隔离日志。
- MCP tool schema/错误转换到 OURO Tool/ToolResult。
- transport 生命周期由专门基础设施对象管理，资源可关闭。

## 完整插件

- manifest 来源、签名/信任信息、权限和配置 schema 可审计。
- entrypoint 加载前完成授权；失败插件不阻止主程序启动。
- install/update/remove 具备 staging、回滚和来源记录。
- contributions 冲突有稳定规则；插件不能覆盖 builtin identity。
- 同进程插件明确标注为可信扩展，不宣称安全沙箱。

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

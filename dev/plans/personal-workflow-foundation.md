# 个人工作流基础计划

状态：未开始

## 目标

建立后续 Desktop、MCP 和高级智能体能力共同依赖的三个应用边界：ResourceService、application 配置门面
和 RuntimeSupervisor。计划不引入云服务、分布式任务系统或通用依赖注入框架。

对应目标规格：[个人智能体平台目标规格](../specs/future-platform.md)。

## 工作流 A：ResourceService

### A1. 领域契约

- [ ] 在 core 定义 `ResourceRecord`、resource id、digest、media type、size 和来源字段。
- [ ] 定义 Message resource ref 的存在性约束，不把本地路径放入 core identity。
- [ ] 定义资源错误：not found、unsafe path、unsupported media、integrity mismatch、in use。
- [ ] 明确 canonical record 与可重建索引的边界。

验收：模型可被纯单元测试构建；core 不 import 文件系统或 MIME 探测库。

### A2. 存储端口与本地实现

- [ ] 扩展或并列定义最小 ResourceStore port。
- [ ] 实现 staging、digest 计算、去重和原子发布。
- [ ] 校验路径逃逸、符号链接、大小限制和重复导入。
- [ ] 实现读取、metadata、导出和引用扫描。
- [ ] 建立资源 index dirty/rebuild 机制。

验收：故障注入证明 partial import 不可见；索引失败不破坏 canonical resource。

### A3. Application 用例

- [ ] 实现 import/list/show/export/remove DTO 与 service。
- [ ] Session branch 复制 resource identity 和引用关系。
- [ ] Session trash/remove 计算资源可达性并安全清理。
- [ ] 工具结果的 `resource_refs` 在提交前验证存在。

验收：分支不再因合法 resource refs 失败；删除不会制造悬空引用或误删共享内容。

### A4. CLI 接入

- [ ] 增加 `resource import/list/show/export/remove`。
- [ ] Session export 包含资源 manifest，但不泄漏工作区外路径。
- [ ] JSON 输出使用 application DTO。

## 工作流 B：Application 配置门面

### B1. DTO 与查询

- [ ] 定义 Provider/Endpoint、权限、工具执行、预算、压缩和 Skills 的公开 DTO。
- [ ] 明确 credential configured/missing 状态和脱敏规则。
- [ ] 提供单一配置 overview query 与分区 query。

### B2. Commands

- [ ] 为每个配置分区封装 validated mutation。
- [ ] 保持 Provider 与 Endpoint 独立事务。
- [ ] 返回 mutation result、diagnostic 和生效范围。
- [ ] 明确运行中 Session 对配置变更的下一请求生效规则。

### B3. CLI 迁移

- [ ] CLI 从直接 manager 调用迁移到配置 facade。
- [ ] 保持命令、JSON shape 和退出码，除非新规格明确变更。
- [ ] 加架构测试阻止 Desktop/CLI 把 config manager 当应用服务。

验收：config manager 只在 application 组合/基础设施路径被访问；CLI 测试继续覆盖原行为。

## 工作流 C：RuntimeSupervisor

### C1. 生命周期协议

- [ ] 定义 run handle、subscription、status snapshot 和 shutdown policy。
- [ ] 保持 RuntimeService 为单 Session Run transaction，不把 durable 逻辑搬到 Supervisor。
- [ ] 明确 Host detach、process shutdown 和 explicit abort 的差异。

### C2. 进程内实现

- [ ] 管理 `session_id -> active task/handle` registry。
- [ ] 提供全局并发限制和 FIFO/fair scheduling。
- [ ] 订阅 durable/stream events，慢订阅者不能阻塞 Run。
- [ ] terminal task 自动清理 registry；异常形成 diagnostic。

### C3. 重启与恢复

- [ ] Supervisor 启动时从 SessionStore 识别 recoverable 状态。
- [ ] 内存 registry 不作为运行事实。
- [ ] shutdown 协作式取消并等待有限时间；未完成状态可恢复。

### C4. CLI 验证 Host

- [ ] 用非交互测试 Host 验证 submit/detach/subscribe/abort。
- [ ] 不急于新增后台 daemon 命令，先稳定 application API。

## 依赖顺序

1. A1-A3 与 B1-B2 可并行设计，但各自保持独立提交。
2. B3 在 DTO 稳定后执行。
3. C1-C3 不依赖资源实现，可在配置门面之后推进。
4. Desktop 开始前必须完成 B 和 C；资源视图开始前必须完成 A。

## 风险与控制

- 资源事务过重：先支持本地 immutable blob，不预建远程对象存储抽象。
- Supervisor 与 RuntimeService 重叠：Supervisor 只拥有跨 Run 生命周期，不复制单 Run 状态机。
- Facade 变成通用 manager：按明确用例暴露方法，不提供任意 config dict 修改。
- 数据布局变化：升级 layout version，并提供显式迁移/导出策略。

## 退出条件

- Resource、配置和 Supervisor 的职责可各用一句话说明且无循环依赖。
- 正常、失败、恢复、重复和并发测试齐全。
- CLI 通过 facade 工作，Desktop 所需接口不依赖 CLI。
- 项目概述、开发架构和相关规格与代码一致。

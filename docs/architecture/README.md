# OURO 架构

> 文档类型：现行架构
> 事实来源：组合根、公开端口、资源所有者与架构依赖测试。

本文定义职责、依赖方向与跨领域不变量。数据字段、状态转换、命令参数及失败矩阵由[领域规格](../specs/README.md)负责。

## 会话事实与动态环境

环境提供当前能力，会话保存可信事实，执行层兑现具体操作的承诺。Agent 根据已有事实和当前能力继续任务；一致性要求作用于请求与调用绑定、身份、授权、事务和副作用状态等实际依赖边界。

每次模型决策取得当前环境，待完成操作依据自身契约收尾，长期等待依靠持久事实保留身份。相关职责、设计理由及维护约束见[会话连续性与动态环境](session-continuity.md)。

## 分层与装配

```text
CLI / TUI                         未来 Host
    |                                |
    +---------- Application ---------+
                   |
             Core / 领域端口
                   ^
       文件、SQLite、Provider、工具等实现

LocalApplication：本地组合与生命周期所有者
```

`core` 不依赖 OURO 外层包。`application` 编排领域规则、端口和基础设施协作者，不依赖 CLI、TUI 或具体 Provider 实现。`local_application` 选择实现并完成装配；Host 解析输入、调用服务和呈现 DTO，不复制事务与恢复规则。

这是职责图，不是通用依赖注入框架。实际允许依赖由[递归依赖矩阵](../../tests/architecture/test_boundaries.py)约束；同一领域内部的协议与实现边界也有独立测试。

## 模块职责

| 模块 | 负责的变化方向 |
|---|---|
| `core` | 领域记录、Provider / Tool / Driver 协议、原子消息帧、上下文与默认循环 |
| `application` | Session / Run 用例、事务与恢复、Agent / Task、查询投影和 Host DTO |
| `local_application` / `local_providers` | 实现选择、扩展及协议装配、共享服务与完整关闭顺序 |
| `sessions` | 后端中立持久化端口、文件 / SQLite、提交、撤销与查询索引 |
| `resources` | 会话文件、元数据、完整性与本地发布 |
| `providers` | HTTP / SSE、协议编解码、适配工厂与可见输出之前的安全重试 |
| `auth` | 授权尝试、凭据材料、revision 条件提交、刷新 / 注销及请求认证 |
| `config` / `prompts` | 配置模型、原子变更、模板、参数合并与提示词事实 |
| `tools` | schema、权限、执行上下文、工具批次与协作取消 |
| `plugins` / `sdk` | 扩展安装和生命周期、Catalog / generation、公开 SDK 与协议边界 |
| `skills` / `mcp` | 技能发现及加载；外部工具连接、调用和健康诊断 |
| `automation` / `billing` | 任务与执行记录；用量账本和审计存储 |
| `transfer` | 有版本的归档格式、路径和大小校验、原生技能文件传输 |
| `cli` / `tui` / `data_commands` | 命令与界面、瞬态交互和共享数据操作参数 |
| `deployment` / `local_io` / `local_locks` / `local_processes` | 本地部署选择、原子 I/O、进程 / 线程协调和受管理进程树 |

Host 需要新增能力时先扩展 Application 用例。其他基础设施不得通过 Session adapter 借用原子写、锁或本地目录解析。

## 一次运行的主链

1. `LocalApplication` 装配存储、配置、Provider 工厂、Runtime、扩展目录、Supervisor、Agent 与任务服务；轻量配置 / 认证命令只装配所需服务。
2. Application 按稳定输入身份持久受理请求；Supervisor 持有长期执行、FIFO 和并发许可。Runtime 在会话执行所有权下
   恢复和验证状态，开始或继续 Run；`RunLifecycle` 维护持久化 Run / Session 转换。Host 等待受理回执或观察结果，切换视图不拥有运行取消权。
3. Run 的 Driver 决定下一步操作。模型请求端口准备上下文，组合有效路由、提示词、压缩、Skills、动态贡献与原子历史帧，记录真实请求快照；`InnerAgentLoop` 是默认算法。
4. `RuntimeAdapter` 调用 Provider；协议适配器把响应投影为统一流事件，Core 不解释供应商的私有认证或续接内容。
5. 完整响应边界形成类型化模型工具调用；Driver 也可显式提交程序化调用。两者共用工具预检、权限、交互和 `ToolBatchExecutor`，结果按调用顺序提交；等待与 Driver checkpoint 使用同一原子提交。
6. 事件持久化后发布；交互预检通知与批次检查点有先后边界，Host 按后续状态事件重新查询权威交互。运行终结、预算、排队输入及 continuation 由各自所属边界裁决。
7. 取消先记录请求并通知 owner，等待正在执行的模型 / 工具资源收尾与审计落盘，再发布 Run 终态。

详细状态与边界见[运行时](../specs/runtime.md)、[上下文](../specs/context-and-compaction.md)和[工具交互](../specs/tools-and-interactions.md)。

工具执行事实、类型化内容与协议呈现的职责划分见[工具结果架构](tool-results.md)。同一结果可在请求边界展开为
原生工具响应及补充上下文，展开不改变 canonical 历史或执行事务。

## 状态与资源所有权

| 所有者 | 权威职责 | 与其他层的关系 |
|---|---|---|
| Runtime / RunLifecycle | 会话运行事务与持久状态转换 | Supervisor 调用它，不直接改写状态 |
| Runtime / RunResources | 单个 Run 的进程内绑定与工具协调器 | 按决策和操作持有绑定；排队输入不持有环境，Observer 为在途回调独立持有租约 |
| RunExecutionOwnership | 进程内执行锁与 AbortSignal | 跨进程 Session execution lock 仍是唯一执行屏障 |
| RuntimeSupervisor | 进程内 FIFO、并发许可、订阅和 shutdown | 不另建持久化调度状态机 |
| SessionRepository / Commit / Harness | 原子变更、检查点、撤销与恢复 | 文件和 SQLite 实现同一领域契约 |
| AgentService | 派遣目录、Delegation、根交互路由与结果交付 | child 是普通 Session / Run，复用同一执行链 |
| Task 应用服务 | 计划领取、执行归属和重启对账 | Agent 目标提交普通会话输入，插件目标调用声明可调度的服务方法；两者使用原生执行记录 |
| 插件生命周期与 Catalog lease | 代次资源、契约快照和引用寿命 | 决策及其调用持有目录；等待释放租约，后续决策使用当前目录 |
| Host | UI 任务、草稿、选择与渲染投影 | 通过快照和事件游标追赶，不成为业务事实来源 |

`run_blocking` 拥有已开始的同步工作及取消收尾。线程重入的配置 mutation 必须在同一工作线程完成，不能持有它跨越 `await`。持久状态、锁、网络授权和 Host 等待各有所有者，不能用额外轮询状态镜像彼此的结果。

`RunResources` 聚合生命周期一致的 Catalog lease、控制器身份、Agent 目录、工具范围及协调器，由 Runtime
统一登记和移除。资源对象先退出 Run 索引，再调用租约释放回调，使释放错误不会保留整组 Run 引用。
持久化 Run 是恢复事实的来源；进程内对象只承载当前执行所需的绑定，精确生命周期见[运行时规格](../specs/runtime.md#run-进程内资源)。

## 跨领域边界

### 框架与插件

框架拥有模型请求、工具执行、权限、Session/Run、任务派发、资源和用量事实。插件提供领域业务、协议实现、
循环算法和扩展处理器，通过独立 SDK 的类型化端口调用原生能力。Goal、Plan 与 Agent Context 使用同样的
公开边界；Host 通过公开交互 DTO 呈现内容，不导入插件业务模块。

Manifest 声明静态贡献与依赖，具名服务承载插件间的业务协作；动态工具整组发布。框架按实际调用保留 owner
及依赖，不向插件暴露跨包对象或内部 Store。进程内与 worker 使用同一业务契约，worker 的双向 RPC 逐项等待
流式结果，并保留父调用的限制。Provider 不继承 Driver 控制端口。

Generation 拥有实例、任务、worker、MCP、私有模块和数据占用。禁用先撤销调用，再结算和关闭资源；升级
发布新代次，在途工作按原绑定完成。等待只保存原生事实与执行契约。卸载保留业务数据和被待完成操作引用的
代码；显式维护在停用及独占数据访问期间执行。详细契约见[插件规格](../specs/plugins.md)。

### 持久化与资源

运行读取不包含回放事件的有界事实集合；查询、回放和导出按各自需要加载历史。文件索引、SQLite 索引、活动投影和目录缓存均可重建。格式版本、提交摘要、fsync / WAL、撤销检查点和恢复日志保护数据边界，不因减少代码而去掉。

资源由会话独立持有，内部 ID、可读文件名与完整性摘要各有职责。导出在复制中校验内容并原子发布；分支、子会话和归档传输保持文件与清单完整。文件输入的来源、内容准备及请求适配见[文件输入架构](file-input.md)。

### 模型、认证与计费

Provider 实例、产品类型与线协议独立；工厂按协议选择 adapter。单次请求取得一致的 headers、目标与续接 scope；401 恢复复用总重试预算，出现可见输出后不自动重发请求。

授权尝试只拥有网络交互和清理，应用用例拥有配置绑定及条件提交，凭据服务拥有材料持久化。跨域提交遵守 credential 锁先于配置锁。请求准备时确定计费归属，完成时记录供应商实际用量，未知费用不投影为零。详见[认证架构](authentication.md)与[计费规格](../specs/usage-and-billing.md)。

### 扩展与子智能体

插件安装只校验和发布不可变包；启用精确选定的内容才执行代码。候选 Catalog 就绪后原子切换，在途调用通过租约保留原目录；会话恢复采用当前能力，待完成调用核对精确执行契约。15 类贡献由明确清单接入，Skill / MCP 资源与代次绑定，后台服务遵循 activation 生命周期。

子智能体目录在父模型决策边界绑定，工具 schema 与实际派遣使用同一快照。子会话复用 Runtime、工具、权限、预算和恢复；sync 等待释放执行许可，async 结果通过持久身份交付，人工交互按完整 child identity 路由。当前单层、bound 生命周期约束见[子智能体规格](../specs/subagents.md)。

### Host 与观察

`SessionObservation` 在 Application 内衔接一致快照、live 事件和分页追赶；Host 成功应用内容后才确认游标。
`SessionContentProjection` 复用通用会话语义，TUI 负责 TranscriptEntry、交互布局与 Strip。状态观察合并失效通知，
消费时查询权威 Agent 树；正文订阅保留顺序并限制批次容量。视图准备结果按目标版本提交，排版独立推进，
不可见会话关闭正文观察。详见[会话观察](../specs/session-observation.md)和[TUI 渲染](tui-rendering.md)。

## 扩展与验证

新增 Host 复用现有服务 / DTO；新的存储、协议或扩展点需要独立业务变化方向。先减少重复扫描、序列化与调用，再考虑缓存；新增缓存须说明 key、失效、容量与所有者。未来条件见[路线图](../plans/roadmap.md)，当前性能边界见[性能规格](../specs/performance.md)。

实现与评审遵循[编码准则](../development/coding-guidelines.zh-CN.md)，验证命令统一见[贡献指南](../../CONTRIBUTING.md)。架构改动需覆盖实际依赖、失败、并发、取消和资源释放；通过测试不替代职责与维护成本审查。

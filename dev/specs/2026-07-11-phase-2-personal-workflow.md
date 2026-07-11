# OURO Agent 第二阶段：完整个人工作流总规格

- Date: 2026-07-11
- Status: Proposed / implementation not started
- Depends on: `2026-07-07-phase-1-core-runtime.md`
- Audit: `../audits/2026-07-11-phase-1-completion-audit.md`
- Sub-specs:
  - `2026-07-11-phase-2-sessions-resources-desktop.md`
  - `2026-07-11-phase-2-extensions-official-capabilities.md`
- Plan: `../plans/2026-07-11-phase-2-implementation-plan.md`

## 1. Motivation

第一阶段已经证明单会话运行闭环：模型流、工具、审批、AskUser、中止、恢复、事件和本地持久化均可
工作。第二阶段的目标不是重写 Agent Loop，而是把该闭环变成适合日常使用的个人工作环境：用户可以
同时管理多段工作、理解后台状态、搜索和分支历史、携带文件、管理模型和扩展，并从 Desktop 使用同一
套 application 能力。

## 2. Goals

- 提供统一 application facade，CLI 与 Desktop 不复制业务规则。
- 按项目概述的 Textual + Rich 基线改善 Interactive，同时保持 Print/JSON 脚本兼容。
- 支持跨会话并发、后台运行、事件订阅和准确的每会话状态。
- 支持标题生成、搜索、筛选、分支、只读回放、可迁移导出和数据维护。
- 正式定义附件、生成文件和外部资源引用的记录、所有权与生命周期。
- 支持项目配置和工作区信任；不信任工作区不能静默获得高影响能力。
- 完成真实 MCP 生命周期和可诊断连接；完善插件、Skills 和模型管理 UX。
- 内置摘要压缩并允许插件贡献替代策略；计划/进度、最小本地记忆和最小浏览器仍以官方插件提供。
- 按项目概述的 PyWebview + FastAPI 基线提供 Desktop 基础应用，复用 Runtime 和 application facade。
- 为未来子智能体保留来源、父子关系和事件归属边界，但不实现子智能体。

## 3. Non-goals

- 不在第二阶段提供稳定 SDK/RPC 公共协议。
- 不实现子智能体编排、探索 Agent 或 Agent 群。
- 不实现通用 RAG、向量数据库或自动长期记忆系统。
- 不提供插件沙箱、容器隔离、企业 RBAC、云同步或多人协作。
- 不实现高级浏览器/桌面自动化；最小浏览器插件之外的自动化进入第三阶段。
- 不引入 SQLite 默认后端；本地可阅读文件仍是权威默认格式。
- 不承诺 Desktop 安装包、自动更新和应用商店发布；第二阶段交付可运行基础应用。

## 4. Design Principles

1. **扩展 application，不扩张 core**

   多会话 supervisor、查询、资源、信任和扩展管理属于 application/service 层。`core` 只在现有
   稳定契约无法表达数据时增加小型、UI 无关的模型或 ports。

2. **命令与查询分离**

   Host 通过 facade 发出命令，通过不可变 DTO 查询状态和订阅事件。Desktop 不读取 session 文件，
   CLI 不直接操作插件/MCP 内部字典。

3. **持久化状态与进程状态分离**

   session、resource、配置和迁移版本是持久化事实；`asyncio.Task`、MCP client、窗口选择和订阅者是
   进程状态，不能序列化进 session。

4. **降级优先，错误可见**

   标题生成、搜索索引、摘要插件、MCP 或 Desktop renderer 失败不得破坏 canonical session。
   降级必须产生诊断记录和可恢复入口，不能静默吞错。

5. **权限只有一个执行入口**

   插件、MCP、浏览器和未来子智能体的工具都进入现有 ToolExecutor；任何扩展不得绕过参数校验、
   auto/approval、事件记录和取消信号。

6. **可迁移而非就地猜测**

   schema 变化使用显式版本、预检、备份和幂等迁移。旧数据不能在首次打开时被无提示破坏。

## 5. Target Architecture

```text
CLI host ───────────────┐
                       ├── ApplicationFacade
Desktop host adapter ─────────┘       │
                               ├── RuntimeSupervisor
                               ├── SessionQueryService
                               ├── ResourceService
                               ├── WorkspaceTrustService
                               ├── ExtensionService
                               └── ModelConfigService
                                        │
              ┌─────────────────────────┼──────────────────────────┐
              │                         │                          │
        RuntimeService             SessionStore              Registries/clients
      (one active run/session)   (canonical local data)   (tools/hooks/plugins/MCP)
              │
        OURO core contracts
```

### 5.1 ApplicationFacade

Facade 是宿主入口，不是远程 API。首批命令至少包括：

- create/open/rename/archive/delete/branch session；
- start/follow-up/approve/reject/answer/abort run；
- attach/remove/export resource；
- trust/revoke workspace；
- configure/check model endpoint；
- enable/disable/check plugin、Skill、MCP server。

查询至少包括 session catalog、session detail、messages、runs、events、resources、active tasks、
pending interactions、model catalog 和 extension health。返回对象不得包含 handler、Task、文件句柄、
client 或明文凭据。

### 5.2 RuntimeSupervisor

Supervisor 管理进程内跨会话运行：

- 同一 session 仍只有一个 active run；不同 session 可以并发。
- 每个 task 记录 `session_id`、`run_id`、phase、started_at 和最后事件序号。
- Host 关闭时先停止接收新命令，再请求取消，等待有限 grace period，最后依赖已有 checkpoint/recovery。
- task 异常必须被收集并转换成运行/诊断状态，不能成为未观察的 asyncio 异常。
- follow-up 仍使用第一阶段持久化队列；Supervisor 不维护第二份内存队列。

### 5.3 EventSubscription

订阅契约包含：

- filter：session、run、event type；
- cursor：最后已消费 sequence；
- snapshot-then-stream：先返回持久化快照，再订阅新事件，避免打开窗口期间丢事件；
- bounded buffer 和显式 overflow 诊断；慢 UI 不得阻塞 run；
- unsubscribe 幂等，subscriber 失败按第一阶段 sink 隔离策略处理。

第二阶段不建立全局连续 sequence；sequence 仍以 session event log 为边界，事件身份使用
`(session_id, sequence)`。

## 6. Cross-cutting Data Contracts

### 6.1 Versioning

- 所有新增持久化 record 保留 `schema_version`。
- 引入 `data_layout_version`，独立于单条 record schema。
- 迁移记录包含 from/to、开始/结束时间、结果、备份位置和错误。
- 读取路径可以兼容上一阶段格式；写入只产生当前权威格式。

### 6.2 Identity and Lineage

Session 增加显式 lineage 字段，不能只塞入任意 metadata：

- `root_session_id`；
- `parent_session_id`；
- `forked_from_message_id`；
- `forked_from_run_id`；
- `title_source`: fallback/generated/user；
- `title_locked`: 用户改名后为 true。

Run 为未来扩展预留：

- `origin`: user/follow_up/replay/plugin；
- `parent_run_id`，第二阶段只记录，不调度子 run；
- `initiator_id`，用于官方插件来源标识。

### 6.3 Diagnostics

跨服务诊断统一至少包含：

- code、severity、component、message；
- session_id/run_id/resource_id/server_id/plugin_id 中适用的关联 id；
- occurred_at、recoverable、suggested_action；
- 可安全展示的 metadata。

诊断不等于用户消息；需要历史追踪的诊断进入事件或专用 health record。

## 7. Workstreams

### 7.1 Sessions, Resources, and Desktop

详细设计见 `2026-07-11-phase-2-sessions-resources-desktop.md`，覆盖：

- session catalog、标题、搜索、筛选和归档；
- branch、replay、export 和数据维护；
- ResourceRecord、文件所有权、缺失检测和导出；
- 多会话后台状态与 Desktop 页面/交互。

### 7.2 Extensions and Official Capabilities

详细设计见 `2026-07-11-phase-2-extensions-official-capabilities.md`，覆盖：

- workspace config/trust；
- Skills、模型和插件管理；
- 真实 MCP 生命周期；
- 摘要、计划、记忆和最小浏览器官方插件；
- 批量工具调用与未来子智能体边界。

## 8. Configuration Precedence

第二阶段统一优先级：

1. 当前命令显式 override；
2. session 持久化 override；
3. 已信任 workspace 的 `.ouro/config.yaml` 或 `.ouro/config.json`；
4. 用户全局 `OURO_HOME/config/config.json`；
5. OURO 默认值。

规则：

- 未信任 workspace 配置只可预览，不能生效。
- workspace 配置只能保存 credential reference，不能保存 inline secret。
- 配置解析必须保留来源信息，UI 能说明最终值来自哪一层。
- 未知字段、错误类型和非法 enum 必须失败并指出路径，不静默忽略。
- 修改配置采用 read-validate-write，保留不相关合法 section。

## 9. Security and Trust

- workspace identity 使用解析后的绝对路径；符号链接/重解析点变化时重新确认。
- 未信任 workspace 不自动加载 workspace Skills、脚本、MCP 或可执行插件贡献。
- 信任是显式用户操作，可撤销；信任记录包含 path、可选 fingerprint、created_at 和 last_seen_at。
- 插件和 MCP 仍在本机用户权限下运行；UI 必须明确“不提供沙箱”。
- 所有外部能力显示 source，涉及写入、命令、网络副作用时遵循工具 approval。
- 日志、events、exports 和 diagnostics 不包含解析后的明文凭据。
- 导入/导出和 resource 路径必须防止目录穿越、覆盖和符号链接逃逸。

## 10. Compatibility and Migration

第二阶段首次启动流程：

1. 只读检测当前 layout 和待迁移对象。
2. 向 Host 返回 migration plan；无变更时直接继续。
3. 需要写迁移时创建时间戳备份和 migration journal。
4. 对每个 session 幂等迁移；失败时停止写入并保留原数据。
5. 重建派生索引；canonical messages/events/runs 不因索引失败丢失。
6. 写完成标记，旧版本进程再次打开时给出版本过高错误。

Phase 1 的扁平 provider 旧格式不重新引入。Phase 2 只迁移当前 endpoint 权威格式和新增字段。

## 11. Observability

新增事件族至少包括：

- `session_title_generated`、`session_branched`、`session_archived`；
- `search_index_rebuilt`、`migration_started/completed/failed`；
- `resource_added/missing/removed/exported`；
- `workspace_trusted/untrusted`；
- `background_run_started/stopped`、`event_subscription_overflow`；
- plugin/MCP/official capability 子规格中的 lifecycle events。

事件 payload 必须使用稳定 id，不嵌入大文件、完整配置或 secret。

## 12. Performance Budgets

在普通个人设备上的验收预算：

- 5,000 个 session 的 catalog 初次加载不超过 2 秒，已有索引时不超过 500 ms；
- 搜索 100,000 条消息的本地派生索引响应目标不超过 1 秒；
- Desktop 打开后首屏不等待 MCP、标题生成或插件网络检查；
- 每个 event subscriber 使用有界内存；慢 subscriber 不增加运行主链延迟；
- 资源字节按流复制和 hash，不把大文件整体读入内存。

这些是工程预算，不是跨硬件 SLA；测试使用确定性数据集和相对回归阈值。

## 13. Acceptance

- CLI 和 Desktop 通过同一 facade 管理 session 和 run，没有第二套 Agent Loop。
- 两个不同 session 可以后台并发；同一 session 仍不会出现两个主 run。
- Host 可在 snapshot 与实时事件之间无丢失切换，慢订阅不会阻塞运行。
- 用户能搜索、筛选、改名、归档、分支、回放和导出 session。
- resource 重新加载、缺失检测、branch 和 export 后仍有稳定归属。
- 未信任 workspace 的项目配置和可执行能力不会自动生效。
- 用户能检查和管理 provider endpoint、Skills、插件和真实 MCP server。
- 摘要使用内置默认策略并可显式替换，失败时不截断历史；其余官方能力可独立禁用和降级。
- Desktop 能显示历史、流式事件、审批/AskUser、资源和后台状态。
- Phase 1 数据可迁移，迁移中断不会破坏原始数据。
- 自动化测试覆盖并发、迁移、损坏、权限、取消、重连、UI facade 和降级路径。

## 14. Resolved Decisions

- Desktop 使用项目概述选定的 PyWebview + FastAPI：FastAPI 只是进程内 facade 的私有 loopback host
  adapter，不作为稳定 RPC；公共 SDK/RPC 仍推迟到第三阶段。
- 本地可阅读文件继续作为 canonical storage；搜索索引和缓存均可删除重建。
- branch 创建新 session 并复制 branch point 之前的消息快照，不修改原 session。
- replay 默认只读，不重新执行工具；“从此处继续”通过 branch 实现。
- 标题生成是非阻塞派生能力；失败回退到第一阶段首条消息截断标题。
- 最小浏览器能力属于第二阶段官方插件；高级浏览器和桌面自动化属于第三阶段。
- 插件隔离是故障隔离和用户授权，不是安全沙箱。
- 第二阶段补齐同一模型响应内的工具调用批次保真；并行只用于全部声明可并行且无需串行审批的调用。

## 15. Open Questions

以下问题不阻塞底层 service 和数据契约，但必须在对应 work package 开始前形成 ADR：

- Desktop 首批支持平台的最低版本和打包方式；
- 搜索派生索引采用纯 JSON 倒排文件还是可选标准库/嵌入式实现；
- HTTP MCP transport 在目标 MCP SDK 版本中的最终命名和认证扩展点；
- 最小浏览器插件采用独立受控进程还是复用 MCP server。

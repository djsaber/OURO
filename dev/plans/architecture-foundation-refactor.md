# OURO 架构底层重构执行计划

状态：已完成（2026-08-01）

设计依据：[`docs/OURO_架构底层重构设计_2026-07-31.md`](../../docs/OURO_架构底层重构设计_2026-07-31.md)

## 目标

在不增加产品能力的前提下，修复持久化版本、Session 扩展状态、资源上界、Run 状态权威和依赖治理，
再按真实变化轴收敛 Runtime/ToolCall 编排。实施必须删除被替代路径，不保留内部兼容双轨。

## 依赖顺序

```text
A0 文档基线
  -> A1 持久化版本边界
  -> A2 Session 扩展状态所有权
  -> A3 Shell 有界输出
  -> A4 Catalog 有界投影
  -> A5 Run 状态内核
  -> A6 Runtime / ToolCall 收敛
  -> A7 依赖、扩展面与 legacy 清理
  -> A8 全量完成审计
```

A3 与 A4 在 A1 后可独立实施；A5 必须先于 Runtime/ToolCall 结构拆分。

## A0. 文档与事实基线

- [x] 记录审查根因、目标不变量、非目标和全局验收标准。
- [x] 建立本执行计划并加入计划索引与路线图。
- [x] 为每个里程碑保存开始前的相关测试、依赖和持久化入口清单。

基线证据分别保存在 A1 的持久化 artifact/decoder 表、A5 的 Run/Session 写入清单、A7 的 package
依赖与 capability 清单，以及各阶段的受影响测试集合中；后续实现均由这些入口迁移，没有以新增并行入口代替。

退出条件：后续实现可以逐项映射到目标不变量，不以“缩短文件”或“通过现有测试”代替设计目标。

## A1. 持久化版本边界

### Artifact 盘点

| 版本域 | 独立持久化根 | 解码边界 |
|---|---|---|
| Core record v1 | Session、Run、Message、Event、Compaction、ContextSnapshot、Delegation | LocalSessionStore / SQLiteSessionStore |
| Core record v1 | ResourceRecord | LocalResourceStore |
| Core record v1 | UsageLedgerEvent | LocalUsageLedger |
| Core 嵌套值 | AgentDefinition、AgentCatalogSnapshot、ApprovalRecord、ProviderUsageEntry | 随 config/Run/ToolBatch/UsageLedgerEvent 根记录验证，不单独读取 |
| Local layout v6 | DataLayoutManifest、Session resource/extension index、Session catalog | LocalSessionStore / disposable index |
| SQLite schema v3 | metadata 与表结构 | SQLiteSessionStore.initialize |
| Plugin schema v3 | manifest、state、audit/contract | PluginPackageStore / plugin loader |

versioned decoder 只处理前三类独立 Core artifact，不接管其他版本域。

### 任务

- [x] 盘点所有独立持久化根记录和实际解码入口，区分嵌套值对象与根 artifact。
- [x] 为当前核心 record 定义封闭版本类型，未知版本不能构造为当前模型。
- [x] 建立一个最小 versioned decoder，要求根 payload 显式携带当前版本。
- [x] Local Session、SQLite、Resource、Billing 等 adapter 在真实读取边界使用同一规则。
- [x] 对缺失、字符串、旧版本、未来版本、非对象 payload 增加失败测试。
- [x] 明确现有 legacy journal/context/tool contract 的迁移或删除归属，不在 decoder 中猜测字段。
- [x] 更新 `sessions-persistence`、`core-runtime` 与数据布局说明。

Legacy 归属决定：本阶段不在 decoder 中迁移字段，也不改变 Core record v1 数字。无 typed turn identity、旧
semantic envelope 和旧 tool execution contract 属于 A7 的内部 legacy 删除/显式版本升级工作；它们必须在 A8
前消失或拥有独立 migrator，不能成为 decoder 的隐式兼容规则。

### 验收

- 新建记录仍自动写入当前版本；
- 独立持久化 payload 缺少版本或版本不等于当前值时产生可诊断 persistence/migration error；
- File 与 SQLite 对相同非法 payload 的语义一致；
- 插件 manifest/state、layout、SQLite schema 的独立版本规则不被错误合并。

## A2. Session-scoped 扩展状态所有权

### 任务

- [x] 盘点 Goal、Plan、Todo 的状态、派生 synthetic messages 和删除路径。
- [x] 定义最小 owner-bound Session state 契约及稳定 owner identity。
- [x] 定义 branch policy：copy、reset 或 reject；不允许隐式继承投影而丢失状态。
- [x] 定义版本化 export section 和 trash tree staging/transaction 参与方式。
- [x] 让 LocalApplication 组合 owner；SessionService 只依赖生命周期端口。
- [x] 迁移 Goal、Plan、Todo，删除各自脱离 Session 生命周期的路径。
- [x] 增加 File/SQLite、branch/export/trash、失败回滚和插件禁用场景测试。
- [x] 更新 Session、插件和 Goal/Plan 规格。

实现决定：`SessionExtensionStateRecord` 是 Session 聚合内按 `owner_id` 唯一的版本化记录；SDK 只暴露绑定
当前 plugin identity 的窄 `PluginSessionStateStore`，不暴露 `SessionStore`，也不扩张为通用 KV。候选
Generation 可读取但在发布前禁止写入 canonical Session。Goal/Plan/Todo 均使用 `reset`；Goal 与 Plan 同时声明
各自 synthetic semantic tag，分支时一并移除状态投影。旧私有 JSON 由显式、幂等 migrator 转入 Session；已无
Session 的孤儿文件进入可恢复 trash，canonical 冲突 fail-closed。

### 验收

- branch 后模型可见 synthetic context 与实际 controller/state 一致；
- export 能完整表达已登记的私有扩展状态；
- trash 不留下不可发现的 session-scoped 插件状态；
- 任一 owner 失败不会产生半个 branch 或半棵 trash tree；
- Observer 不承担 canonical 清理。

## A3. Shell 有界输出

### 任务

- [x] 用测试固化大 stdout/stderr 必须在进程边界按共享预算截断，不得先完整累计。
- [x] 在 ToolExecutionContext 进程边界并发、有界地消费 stdout/stderr。
- [x] 统一 truncated metadata，移除 builtin shell 的事后上限职责。
- [x] 验证 timeout、abort、后代继承 pipe、非 UTF-8 和 PowerShell CLIXML。
- [x] 更新 tools 规格。

实现决定：`run_shell` 使用两个持续 drain task 和一个共享 byte retention budget；预算耗尽只停止保留，不停止
读取。`ToolProcessResult` 是 pipe 截断事实源，Builtin Shell 原样投影每个流的标志，executor 继续负责最终组合
文本的统一字符上限。终止后 EOF 等待有固定 deadline，避免已退出父进程的后代继承 pipe 造成永久挂起。

### 验收

- 大输出测试的内存累计不超过配置上限加固定开销；
- stdout/stderr 不互相阻塞；
- timeout/abort 后进程树和 pipe 均在 deadline 内释放；
- 普通命令的公开结果契约保持稳定。

## A4. Catalog 有界增量投影

### 任务

- [x] 从 SessionIndexEntry 移除完整 transcript `search_text`。
- [x] Catalog 查询只覆盖有界字段，并同步 CLI/Host 文案和规格。
- [x] 为 Store 增加实际用例需要的轻量 latest/count projection，不加载 RuntimeSessionSnapshot。
- [x] Local 与 SQLite 增量维护等价 Catalog；dirty rebuild 同样使用有界查询。
- [x] 增加长会话和大 Catalog 基准/查询计数测试。
- [x] 将未来全文搜索保留为独立目标，不预建通用搜索服务。

实现决定：`SessionCatalogProjection` 只含 latest Run/message 与三个 count。Local rebuild 只解码 Session、由
`last_run_id` 定位的单个 Run、最多 64 KiB 消息尾部，并枚举固定尺寸记录；SQLite 通过一条带 count/latest
子查询的语句生成全部条目。Catalog text query 只覆盖 title、160 字符 preview、workspace、model 与 tags。
layout v6 丢弃旧 transcript-bearing index 后用 bounded projection 重建，不迁移冗余 transcript。

### 验收

- 单次 Run 完成的 Catalog 更新成本不随历史消息总量增长；
- SQLite Catalog rebuild 不执行 per-session full-history load；
- index 不重复保存完整对话；
- File/SQLite 查询结果保持契约一致。

## A5. Run/Session 状态转移内核

### 任务

- [x] 列出所有 `save_run`/`save_session` 调用及其状态不变量。
- [x] 用 contract tests 固化 start/wait/resume/pause/complete/fail/abort/recover/queued 行为。
- [x] 提炼纯 transition plan，不包含 Store、事件、锁或外部调用。
- [x] 迁移 RunLifecycle、Agent、continuation、recovery、session control 和 Runtime 的直接投影。
- [x] 删除重复 `session_status_for_run` 调用和硬编码 Session status。
- [x] 更新 core-runtime 与 subagent 规格。

### 写入清单与边界结论

- `RunLifecycle` 的 start/wait/resume/complete/fail/pause、公共 abort、Agent child queued/follow-up、session
  control cancellation 和 interrupted recovery 是 Run status 迁移，全部先调用纯 transition plan，再按 Run ->
  Session 顺序持久化。
- ToolCall 的 preflight/interaction/execution/commit 通过 `RunLifecycle.save_batch_state` 进入同一规则；continuation
  只更新 `RunContinuationCheckpoint`，不拥有第二套 status 投影。
- Runtime 原先在 compaction fail 后把 Session 从 `failed` 再补写为 `idle`；现由 Run kind 与 status 一次推导，
  该补丁路径已删除。InnerLoop 的内存 waiting 态也由同一 plan 生成。
- usage、model round、continuation settlement、terminal batch audit 和 recovery result reconciliation 只更新 Run
  的非状态字段；queued input、control state、消息游标和 session 配置只更新 Session 自身字段，不属于状态迁移。
- Session recovery 仍负责从全体 Run/Message 修复损坏的持久化投影，但复用同一 `session_state_for_run` 规则，
  不维护恢复专用映射。

### 验收

- Run 是状态事实源，Session 投影由同一规则生成；
- 非法转移在写入前失败；
- 每个 application 用例仍拥有清楚的事务/事件职责；
- 全仓不再存在绕过转移内核拼装 active Session 投影的路径。

## A6. Runtime 与 ToolCall 按变化轴收敛

### 任务

- [x] 以 characterization tests 固化 Runtime run/resume/terminal 关键流程。
- [x] 分离 Run command、execution ownership、interaction resume、terminal settlement。
- [x] 将 InnerLoopPorts 收敛为少量内聚 collaborator 或纯 reducer 边界。
- [x] 按 preflight/interaction/execution/commit 拆分 Tool batch durable phase。
- [x] 删除被替代方法、attach 后装配和重复 Run-local registry 解析。
- [x] 检查 RuntimeService、ToolCallCoordinator、InnerAgentLoop 的公共职责说明。

### 实施边界与结论

- `RunLifecycle` 负责 durable command，`RunExecutionOwnership` 唯一管理 session run lock、AbortSignal 和 durable
  abort 轮询；RuntimeService 只保留必须跨 Store、ToolCall、model loop、queued input 的 interaction resume 与
  terminal transaction。后两者没有拆成转发 façade。
- 原 14 个单方法 InnerLoop Protocol 已删除。`InnerLoopPorts` 只组合 context、run boundary、turn boundary 三组
  内聚依赖，加事件和完整工具批次两个直接边界；Tool registry 以 definitions callable 传入。
- 纯 `tool_batch_suspension` 固化 approval -> child -> user 的暂停优先级和 Run/Batch phase 投影，planning、并发
  checkpoint、execution completion、resume 四条路径共用；preflight 与 result commit 的 durable event 顺序继续由
  `test_tool_call_batches.py` 固化。
- Runtime 按 Run 缓存一个 ToolCallCoordinator，复用 frozen catalog 与动态 controller scope，terminal 时清理；
  approval、AskUser、child wait 不再重复构造 executor 和解析 registry。
- `attach_extension_catalog`、`attach_agent_definitions`、`attach_agent_controls` 已删除。LocalApplication 在解决
  Runtime-Supervisor-AgentService 构造环后调用一次不可重复的 `bind_extensions`，一次绑定完整 Host 组合，避免
  三步半装配；裸 Runtime 明确不绑定扩展。
- characterization 证据覆盖 Runtime 59 项、Tool batch 20 项、InnerLoop 2 项、Agent/Recovery/Continuation 113 项，
  并新增 ownership、suspension 与 per-Run coordinator identity 契约。

### 验收

- 每个模块只有一个主要变化原因；
- 不新增大转发 façade、service locator 或单方法 Protocol 集合；
- approval、AskUser、child wait、abort、continuation 和 queued input 行为保持；
- 恢复与正常执行使用同一状态规则而非双轨。

## A7. 依赖、扩展面与 legacy 清理

### 任务

- [x] 把 local atomic I/O 与 mutation lock 移入共享基础设施，移除 resources 对 sessions 的依赖。
- [x] 用 allowed-edge matrix 递归检查全部 `ouro` package import。
- [x] 盘点 13 种 plugin capability 的 builtin/example/contract 消费证据和稳定性等级。
- [x] 冻结无真实消费者的扩展面，不继续复制适配层；必要时破坏式删除未承诺 API。
- [x] 删除内部 legacy question/tool/context 路径，或为确需保留者建立明确迁移窗口。
- [x] 修复 UsageLedger 可变缓存别名，保持 append-only 权威性。
- [x] 建立快速 architecture/unit gate，并保留跨平台 full suite。

实现证据：原子 I/O 与文件锁现由顶层 `ouro.local_io` / `ouro.local_locks` 拥有，Resource 不再依赖 Session；
架构测试以显式 allowlist 递归解析绝对和相对 import。13 类 capability 的 producer、contract、真实 Host consumer
与稳定级别已写入 plugin spec，清单冻结且没有发现空扩展点。内部 question alias、完整 `tool_call` stream 双轨、
不完整旧工具 contract 与无效 Toolkit 参数均已破坏式删除；schema-1 context digest 读取明确在 schema 2 结束，
外部 MCP SSE 仍由独立 transport adapter 承担。UsageLedger 对输入、entries、events 作深拷贝隔离。
快速 architecture/core gate 共 151 项、约 3 秒；受影响 Provider/Core/Tool、Application、Plugin/CLI 回归分别为
125（另 1 skip）、134、31 和 72 项，全绿。

### 验收

- package graph 与开发架构一致；
- 共享基础设施不归属 sessions adapter；
- 每个稳定 contribution kind 有真实端到端消费者；
- 内部 legacy 无永久双轨；
- ledger 公开读取不能修改缓存中的 canonical state。

## A8. 完成审计

- [x] 按设计文档逐项核对所有不变量和非目标。
- [x] 运行完整测试与 branch coverage，覆盖率不低于 80%。
- [x] Ruff lint、format、Mypy、compile、build、diff check 全绿。
- [x] File/SQLite、Windows/POSIX 关键 contract 有证据。
- [x] 更新项目概述、开发架构、路线图、规格和插件开发指南。
- [x] 确认无临时 compatibility wrapper、未使用抽象或文档声称已完成但代码未实现的行为。

完成证据：

- 当前 Windows/Python 3.14 工作树的全量 branch coverage 门禁为 1301 passed、2 skipped、0 warnings，
  总覆盖率 83.89%；`ResourceWarning` 与 `PytestUnraisableExceptionWarning` 已是项目级失败条件。
- architecture/core 快速门禁 151 passed；Ruff lint、343 文件 format check、Mypy 231 个源文件、compileall、
  sdist/wheel build 与 `git diff --check` 全绿。
- `test_store_contract.py` 对 File/SQLite 执行同一版本、extension state、trash 和 Catalog contract；长历史
  projection 另有双后端测试。当前 Windows 全量实跑，CI 的 Python 3.12 matrix 在 Ubuntu、Windows、macOS
  运行同一套测试，POSIX-only symlink/process contract 不被 Windows skip 取代。
- 新增边界均有生产消费者：persistence decoder 被四类 adapter 使用，Session extension state 被 Store、
  SessionService 与三个 builtin 使用，Run/tool batch 纯 transition 被 application 编排使用，run ownership 被
  Runtime 使用，共享 local I/O/lock 被 config、session、resource、billing/plugin adapter 使用。
- 已删除的内部旧路径没有 wrapper 或双写。仅保留 ContextSnapshot schema-1 与 builtin state 的显式、带版本
  删除窗口 migrator；legacy SSE 是隔离的外部 transport，不是内部 API 兼容层。
- 项目概述、开发架构、路线图、插件指南、core/runtime/session/tool/resource/plugin/MCP 规格及 Changelog
  已与当前行为核对；审计时发现并删除了 Toolkit README 中残留的 `include_dirs` 旧 schema。

只有 A1-A8 的退出条件全部由当前代码、测试和文档证明后，本计划才可标记完成。

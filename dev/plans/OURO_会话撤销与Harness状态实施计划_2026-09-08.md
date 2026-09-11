# OURO 会话撤销、Harness 状态与插件交互实施计划

> 文档类型：本次改进实施计划
> 状态：实施中；M1–M5 已完成阶段验收；M6 未开始
> 计划基线：2026-09-08
> 范围：仅本次状态、插件交互与撤销改进，不包含其他路线图项目

设计取舍见[设计文档](../../docs/OURO_会话撤销与Harness状态设计_2026-09-08.md)，
强制契约及 AC 验收编号见[目标规格](../specs/OURO_会话撤销与Harness状态规格_2026-09-08.md)。
本文安排依赖、改动位置和退出条件，不重复定义另一套业务规则。

## 1. 执行原则

- 先建立原生状态和提交边界，再接插件，再开放撤销行为。不得先移除 extension state 禁止撤销检查。
- 以可验证的纵向链路推进，每个阶段包含失败/恢复证据；最终同时覆盖两种存储后端和两种插件运行形态。
- 允许按状态、调用和提交职责拆分已有大模块；拆分必须消除真实耦合，不能只搬文件或新建无职责抽象。
- core 只依赖 core，plugins 不反向 import application，Host 通过应用入口操作会话；所需能力由组合根注入。
- 每个里程碑开始和退出时对照设计文档的“核心设计哲学”审视方案；实现若需要调整契约，必须同步修正三份
  文档及验收依据，不能靠局部例外绕过边界。
- 不并行维护“旧直接写入”和“新版本化写入”两套可对外执行路径。未完成的阶段只通过内部契约测试验证，
  不把部分能力宣称为已经可用，不添加长期兼容开关。
- 不顺带改插件安装/发现、Provider、任务调度产品行为或其他路线图功能；相关入口只做执行有效性接入。

## 2. 改动面与复杂度

| 工作面 | 主要位置 | 复杂度及原因 |
|---|---|---|
| 原生状态与持久化 | core/session_extensions、session_controls、messages；sessions/base、local_files、sqlite | 高：同一决定覆盖多记录，文件中断恢复和精确消息锚点必须一致 |
| 插件调用与协议 | sdk/plugins、plugins/api、worker、contribution adapters、callbacks | 中高：状态范围、嵌套调用、超时与两种运行形态的语义需要统一 |
| 应用结算与内置插件 | application/session_controls、tool_calls、run_continuations、runtime；builtin_plugins | 高：移除已有直接写与 Run/usage 混存，统一结算和恢复 |
| 撤销、队列和继续 | application/session_editing、sessions、queued_inputs、runtime、agents、supervisor | 高：竞争、旧代次失效和任意消息切口相互关联 |
| Host 与观察 | application/events、observers、会话观察；cli、tui | 中：复用投影和草稿模型，修正订阅与旧流式事件 |
| 迁移与已有生命周期 | sessions 迁移/分支/导出、builtin 迁移、SDK 示例、领域规格 | 中高：只能恢复有依据的数据，不能在迁移时伪造历史 |

工作量集中在提交与恢复语义，不在新增快照字段。按下列退出条件评估完成度，不以文件数量或代码行数衡量。
存储端口、SDK 与内置插件之间存在紧密依赖；不要为了并行开发提前复制临时适配管线。

## 3. 里程碑顺序

```text
M1 原生状态与提交底座
  → M2 调用范围与 SDK/worker，Todo 纵向验证
  → M3 控制/挂起/续跑结算，Plan/Goal 迁移
  → M4 任意节点撤销、队列与执行失效
  → M5 Host 观察、完整输入与现有生命周期
  → M6 集成验收与现行规范收敛
```

迁移设计和旧数据样本在 M1 确定；插件业务迁移随 M3 编写；跨后端和完整生命周期在 M5 验证。
不把数据迁移作为功能完成后的附加工作。

## 4. M1：原生 Harness 与提交底座

前置：阅读现有 SessionRepository、history-cut 恢复、SQLite transaction、消息帧与索引实现，确认
Session/Run/消息写入及查询入口清单。记录本次更改会替换的直接写路径。

- [x] 演进 owner 状态和 controls 为一个 canonical Harness 聚合；定义不可变版本、当前引用、消息锚点、
  history_epoch、有限的提交数据和明确错误语义。类型按职责放置，不创建第二份 Session 状态机。
- [x] 规定操作中多消息/多状态变化的逻辑顺序，并用工具结果、输入批次和无消息命令样例验证锚点。
- [x] 在 SessionRepository 定义短提交和一致读取端口；SQLite 实现事务，File 收敛 history-cut 恢复机制。
- [x] 把消息/状态/挂起或队列意图/结果纳入可恢复决定；提交事件使用既有日志并按操作身份去重。
- [x] 所有目标 Session canonical 读取路径接入未完成决定恢复；Catalog 查询保持既有只读投影边界。
- [x] 建立新旧格式 fixtures、版本升级与迁移基线规则；有活跃 owner 时拒绝并发迁移。保留消息索引与尾部裁剪。
- [x] 注入快照写入、决定持久化、消息更新、状态切换、事件 append 和完成标记各故障点。

退出条件：两后端通过 HS/AN/CM 的底层契约测试；状态与消息不存在可观察的半提交；恢复不调用插件。
AC-01、AC-06、AC-07 的底层部分和 AC-15 基线测试通过。此阶段不开放新的 /undo。

M1 验证入口：`tests/sessions/test_harness_commits.py`、`test_harness_migration.py`、
`test_message_truncation.py`、`test_message_queries.py`、`test_store_contract.py` 和
`tests/core/test_persistence.py`。Windows 上存储回归与记录版本测试共 171 项通过，随后补充的两项 SQLite
迁移锁测试通过；`mypy ouro` 通过。调用方的原子结算、SDK 写权限及完整生命周期仍由 M2–M5 接入，
不能据此宣称应用撤销已完成。M1 没有移除应用层原有撤销保护。

## 5. M2：插件调用范围与 Todo 纵向链路

依赖 M1。保留不可变 Catalog、Generation gate/lease 和现有 contribution 类型。

- [x] 为现有 callback 适配链加入显式调用范围：owner/Session 绑定、快照、只读或暂存写、取消、关闭与有效性。
- [x] 从 PluginContext 移除长期 canonical 写能力；共享最小状态视图和变更封装，不向 SDK 泄露存储端口。
- [x] 明确 readonly contribution 和可变更 contribution 的授予规则；合并决策前不提交任何策略提案。
- [x] worker 随调用传入快照、随结果返回变更，与 in-process 共用验证和结算；显式升级 SDK/协议与执行契约。
- [x] 验证同步 handler、异步 handler、嵌套 callback、保存旧视图的后台 task、worker 超时/迟到的失效行为。
- [x] Todo 完整迁移：读取、修改、工具结果共同提交、失败无写入；维护一个最小 in-process 与 worker 状态示例。
- [x] 实例资源任务继续由现有 task group 管理；会话效果复用现有调度入口，确认不存在后台直接保存的旁路。

退出条件：Todo 验证新机制的正常、冲突、失败和恢复链路；只读写入/越界/关闭后写入明确失败。
AC-03 的 Todo 部分、AC-05/06 及插件形态等价性通过；不为 Plan/Goal 添加临时特殊撤销实现。

M2 验证入口：`tests/core/test_harness_calls.py`、`tests/plugins/test_harness_calls.py`、
`tests/application/test_harness_tools.py`，以及 SDK、Observer、工具批次、插件装配、worker、示例和架构回归。
Windows 集中运行共 238 项通过，2 项仅适用于 File 的故障点在 SQLite 参数下跳过；SQLite 事务故障证据见 M1。
原子提交验证覆盖真实 Todo 与 worker 子进程、File/SQLite、回调异常/非法输出、提交回复丢失、File 决定应用中断、
恢复后同一结果重试和撤销后旧 Run 拒绝写入。`mypy ouro` 与 `ruff check ouro tests` 通过。
SDK 入口为 v2，Plugin API/worker protocol 为 3.0；没有保留实例级 Session 写入接口。
Plan/Goal 已调整为调用内状态接口，其命令/Controller/Continuation 结算和业务数据拆分仍属于 M3，尚未验收。

## 6. M3：控制、交互与续跑统一结算

依赖 M2；这是从“新增状态 API”转为“规范框架管线”的主要工作。

- [x] 重构 SessionControlCoordinator 的 acquire/release/replace：嵌套 callback 共享提案，claim 提交时校验，
  controller 业务关系与 owner 状态共同生效；准备/失败过程进入操作记录，移除半完成业务状态发布路径。
- [x] Tool/Command 输出接入应用提交边界；外部效果的事实归属与 canonical Harness 结算分别表达。
- [x] 把 ToolInputRequest、审批和等待阶段的状态/恢复身份一并提交；恢复使用新调用范围并检查原等待记录。
- [x] Continuation 使用现有 durable settlement plan，纳入状态提案和稳定去重；多策略失败/冲突无部分保存。
- [x] Plan 迁移模式与业务值，去除旧 pending 执行许可混存；审批记录保留原生所有权，计划文件不回滚。
- [x] Goal 从原生 Run 控制快照取得执行绑定；稳定目标身份关联原生 usage，Context 不保存、计费不随快照回退。
- [x] 编写已知 Todo/Plan/Goal schema 的一次性迁移，处理可验证 usage 基线；不确定历史明确拒绝或保留边界。
- [x] 删除被替代的插件存储锁、重复持久化 revision、run_binding 与补偿路径，保留必要业务校验。

退出条件：激活、replace、等待、取消、异常终结、完成、续跑均不产生模式/数据错位；schema 迁移可重入。
AC-04、AC-09 的交互部分、AC-14，以及 AC-03 的 Plan/Goal 部分通过。

M3 当前证据：`test_harness_commands.py`、`test_harness_controls.py`、`test_harness_continuations.py` 与
`test_harness_tools.py` 覆盖两后端的嵌套草稿、失败/冲突丢弃、提交前故障、提交后响应丢失/取消，以及互动和
AFK 等待阶段；不修改 owner 数据的等待请求也使用原生版本前置条件。Plan schema 2 只保存计划路径，模式读取
原生控制关系，审批数据留在原生请求；实际“写计划→等待→重启→接受”及 /plan、/control 流程通过。
Goal schema 4 已只保留稳定 goal_id、目标内容、业务状态、报告、预算与业务时间；移除 Context 写入器、
私有 activation/run_binding、累计 tokens_used/last_accounted_run_id 和重复 revision。控制器仅声明目标
usage_scope；状态查询、预算和续跑读取原生事实。SDK、worker、Continuation 与控制关系中的旧 revision 字段
同步移除，不留运行时双格式分支。replace 显式携带目标 controller id，保留同 owner 权限边界。

`test_builtin_goal.py` 验证两后端的生命周期、旧请求隔离、重复续跑不写快照、未知用量、精确预算、撤销后的
实际消耗，以及真实创建→续跑→完成和冷恢复中断。创建前请求与释放后确认不记入目标，新目标不串账。

`test_builtin_harness_migration.py` 覆盖 Plan 1→2、Goal 3→4、当前 Todo 原样保留、历史可达版本及消息锚点
一致重映射、同一目标的原生 usage 归属、重复执行、归属后未发布重试、File 决定恢复与 SQLite 事务回滚。
迁移生成新不可变版本，保留原快照；无法证明的计数、同 Run 多目标归属、未结算执行和未知 schema 明确拒绝。
旧 Plan/Todo 文件先经有执行所有权的单次导入，提交持久后移除源文件；Plan 保留源 schema 再统一转换。
组合根提供显式 `migrate_builtin_harness_state` 入口；M5 负责接入 Host 的会话维护/导入生命周期，
不在启动、普通读取或 undo 热路径全量扫描和迁移。

旧 activating/deactivating 自动补偿已删除。执行调用对非 active 的旧控制关系明确要求维护，
保留原数据及现有显式 Host force-release 入口；新提交的中断恢复只应用原生 durable decision。
本阶段不开放新的 /undo；M4 仍需完成待办交互的精确 activation/epoch 所有权与所有迟到会话效果的拒绝。

M3 阶段验证（Windows）：集中运行 418 项通过，2 项 File 专属故障点在 SQLite 参数下跳过。
范围含 Harness 命令/控制/工具/续跑/usage、Runtime、本地装配、真实 Goal/Plan、已知迁移、SDK worker、
插件系统、两后端原生提交/迁移和架构边界。后续局部修改分别重跑受影响的 Goal 与迁移用例。
Ruff、mypy（297 个源文件）、编译及改动文件格式检查通过；仓库全量验收仍属于 M6。

集中回归同时修正提交底座的上下文失效范围和文件创建发布边界：普通响应追加保留最近请求上下文，Harness
改变或历史截断才失效；Session 枚举不读取尚未发布的暂存目录。消息准入前的新 Session/Run 使用显式准备值，
执行调用复用实际持有的 Run 请求快照，不通过缺失记录异常兜底。TodoList 清单与实现统一声明 exclusive；
故障注入和流式性能测试覆盖现行提交管线。

## 7. M4：精确撤销、排队输入与执行有效性

依赖 M3。底层契约完整后才替换原撤销限制。

- [x] 原生撤销接受任意消息角色和精确 before_message_id，恢复消息前版本，递增代次，修正消息头与派生状态。
- [x] 从裁剪前置条件中移除工具帧限制；Provider 投影处理不完整交换，保留原切口并提供明确诊断。
- [x] 应用入口按真实来源只接受直接用户输入，支持 message_id/queue_id 和约定的默认选择，返回完整输入。
- [x] 消费与 acknowledge 收敛为原子语义；取消/消费竞争按 queue_id 定位，不出现双消费或误选其他输入。
- [x] 撤销保留其他待消费直接用户输入，不自动 drain；旧 epoch 自动输入和交付失效，恢复消费重新检查策略。
- [x] 将代次检查接到工具结果、状态命令、Continuation、挂起恢复、后台调用和 child delivery 所有会话效果入口。
- [x] /continue 在撤销后基于当前历史创建新执行，不恢复旧 Run checkpoint，不重复添加保留输入或补交撤回结果。
- [x] 保留 parent/child 审计；按既有归属取消相关工作，以父提交检查拒绝迟到投递。
- [x] 用受控同步点覆盖执行所有权、中断结算、命令/工具/撤销竞争；提交后取消按实际 durable 结果处理。

退出条件：AC-01/03/06/08/09/10/12 全部对应场景通过；不存在按插件 id、消息格式或 delegation 存在性
建立的 blanket 撤销禁令。所有新增成功路径都有相应失败与恢复证据。

M4 实现收口（2026-09-10）：原生裁剪恢复 before_harness_version 并递增 history_epoch，旧 Run/事件/usage
保留审计身份。应用 undo 按直接用户来源选择 message_id/queue_id，返回完整输入；Runtime 拥有提交后的租约与
bound child 结算。活动 Run 查询、请求准备、挂起、续跑与恢复均按当前代次判定执行有效性；审批绑定具体
controller activation。没有增加插件专用 undo 或撤销业务回调。

新 Run 启动及运行边界消费统一提交整批输入、消息锚点、Run 与队列移除，删除独立 acknowledge 和旧半消费
修复分支。独立输入保留身份并清除旧许可；Host 接受事实复用原生提交回执；旧 finalizer 和排队 continuation
不能在撤销后驱动消费。父结果在父接受边界检查代次，bound 子工作沿用已有取消结算。子会话的新执行复用
委派准备入口，保留冻结定义、工具范围、约束和预算，并更新当前 child Run 身份。

阶段验证入口及对应边界：

| 验收 | 测试入口与证据 |
|---|---|
| AC-01/10 | test_history_execution、test_harness_commits：任意角色及工具结果内部精确切口；Provider 排除不完整交换并保留诊断；新 Run 不补旧结果、不重复输入 |
| AC-03 | test_harness_tools、test_builtin_goal、test_builtin_plan：真实 Todo/Goal 状态与用量、Plan 连续中断撤销；恢复原版本，再写入使用新身份，计划文件保留 |
| AC-06/08 | test_harness_commands、test_history_execution、test_undo_inputs：命令/撤销、取消/消费受控竞争；完整批次决定前失败、决定回复丢失、同身份重试及忙碌时拒绝 |
| AC-09 | test_harness_tools、test_harness_continuations、test_undo_inputs、test_builtin_plan：旧工具范围/续跑计划/交互失效；旧终态不能 drain，Supervisor 旧请求拒绝执行 |
| AC-12 | test_agents：父接受前发生撤销时拒绝 child 迟到交付；已完成父 Run 的 bound child 被结算，保留子事实；子会话新执行保留委派约束 |

M4 阶段验证（Windows）：集中运行 513 项通过，2 项 File 专属故障点在 SQLite 参数下预期跳过；
覆盖 Runtime、Agent、精确撤销、队列、Harness、真实 Plan/Goal、Supervisor、两后端原生提交/迁移、
TUI 历史命令及架构边界。补充上下文压缩、Agent 恢复、执行所有权和消息裁剪回归 76 项通过；
Supervisor 查询避免新增消息历史读取后，31 项回归通过；排队继续请求竞争及 Plan 连续撤销另跑 3 项通过。
这些为分别运行的结果，存在重叠，不合计为独立用例总数。

Ruff、mypy（297 个源文件）、编译、33 个改动 Python 文件的格式检查与 diff 空白检查通过；
三份文档的 19 个本地链接有效。完整仓库质量门槛仍由 M6 执行。

TUI 本阶段只接入新 undo 结果及现有技能/资源草稿表示。Host 代次观察、完整输入显示、重启活动、跨后端
生命周期与性能测量仍由 M5 验收；当前证据不代表最初 Windows TUI 崩溃场景已经完成端到端验收。

## 8. M5：Host 观察、输入与会话生命周期

依赖 M4，复用现有应用 DTO、资源引用、草稿和订阅能力。

- [x] 提交通知和 initial/live snapshot 关联 history_epoch；lag/重连后读取一致的消息、状态、活动和队列。
- [x] TUI/CLI 接入精确撤销结果；成功后标准重载，再恢复正文、附件和技能；失败不丢原草稿。
- [x] 旧事件按归属过滤，不读取已撤回消息、不吞掉当前历史的真实 PersistenceError。
- [x] 验证重启后的活动恢复、中断再提交、多 Host 观察、同长度/增长重写及独立 reader 索引有效性。
- [x] 导出/导入、trash、File/SQLite 转换保留可达版本与锚点；分支 copy/reset/reject 一致处理状态和版本引用。
- [x] 完成旧会话迁移集成：基线前不可恢复边界明确可诊断；插件缺失时状态仍保留，执行报契约不可用。
- [x] 做性能对照：固定短后缀、100/10,000 条保留消息、多 owner 与不同 Harness 大小、无变化回调、只读并发。
  记录耗时、读取/写入量、fsync 次数和快照数，验证 PF-01；不为通过基准降低持久化保证。

退出条件：AC-02/11/13/15/16 通过；实际 Windows TUI 验证中断→撤销→编辑→提交→重启进入会话。
另覆盖 POSIX 的文件提交恢复语义。测试使用临时 OURO_HOME，不修改用户真实会话或计划制品。

M5 当前实现采用 Event schema 2、File layout 8、SQLite schema 6。Run 归属只在首次事件读取后进入有界缓存，
流式 delta 不重复读取 Run，也不升级为逐 token 的完整 fsync。消息索引用 canonical history_epoch 识别重写。
完整快照包含所有保存的 Harness 版本与提交回执；普通观察保持有界查询，SQLite 完整读取沿用已有只读事务。
文件快照发布使用短临时目录名，避免重复拼接 Session 身份造成 Windows 路径超长。

TUI 的附件选择进入 schema 2 草稿，和正文、技能选择共用生命周期；Controller 不再保留第二份待恢复输入。
空正文可以保留附件；提交失败恢复尚未被新输入替换的草稿。CLI 复用现有附件选择及 `/skill` 表示，
`/undo` 展示恢复内容，空行重发或输入替换正文；`/continue` 调用现行运行时继续入口。
显式 `session migrate-harness` 维护租用插件 Catalog，不启动模型、任务调度或 MCP。

M5 收尾（2026-09-12）：Host 观察、富输入、生命周期、显式迁移、性能及平台验收已完成。
本轮只增加诊断和验收记录，没有为性能测量调整业务实现或降低持久化保证。

| 验收 | 已通过的入口与证据 |
|---|---|
| AC-02 | test_history_commands、test_message_queries：同长/更长重发、撤销并发消息读取、独立 reader 与重启；另在 Windows PTY 实际运行 TUI，完成中断→undo→编辑→提交→退出→新进程重启 |
| AC-11 | test_history_observation、test_session_event_following、test_async_session_pipeline：初始快照/live、lag/重连、多 Host、旧事件隔离；当前历史真实缺失保持 PersistenceError |
| AC-13 | test_history_commands、test_input_drafts、CLI test_interactive_attachments：正文/技能/附件的撤销、导航和重启恢复；失败保留完整草稿；来源选择沿用 M4 输入契约 |
| AC-15 | test_harness_migration、test_event_migration、test_harness_lifecycle、CLI test_session_commands、test_history_execution：不可恢复旧基线、显式迁移重入、插件缺失、导出/转换/trash/分支及去重事实 |
| AC-16 | 同一脚本在 Windows、Linux 上分别顺序运行 M4/M5，每组 36 次，共 144 次；全部验证真实状态回退、版本数、进程 I/O 和同步次数 |

Windows 真实终端验证使用临时 OURO_HOME 和离线 Faux Provider。重启显示 Ready，再输入 `after restart`
提交成功；canonical Session 的 active_run_id 为空，原 Run 保留 epoch 0/aborted，新 Run 为 epoch 1/completed。
这是真实 PTY 中的自动终端按键验证，附件和技能等补充场景由 Textual/Pilot 测试覆盖。

POSIX 验证使用临时 WSL2 / Ubuntu 24.04.5，代码、运行时和测试数据均在 Linux ext4 内，Python 3.14.6。
业务回归 257 项通过（存储 183、核心 12、应用 62）；原生同步探针在 Windows、Linux 各 2 项通过。
在 ext4 源码目录中使用锁定依赖执行：

```bash
python -m pytest tests/sessions tests/core/test_harness_calls.py \
  tests/application/test_history_observation.py \
  tests/application/test_session_event_following.py \
  tests/application/test_history_execution.py -q
python -m pytest tests/benchmarks/test_sqlite_sync.py -q
```

文件测试覆盖决定前失败、决定已持久化后的部分应用、完成标记失败、迁移中断重入和独立 reader 恢复。
同步探针校准持久写入、只读和异常清理；Linux 使用 strace 观察内置 SQLite 的真实系统调用。
初次探针选择不适用于该 Python 构建的 VFS 入口，改为系统调用观察后两项均通过；未跳过可执行的验收。

性能原始数据和结论见 [Windows / POSIX 成本对照](../benchmarks/results/harness-platform-comparison-2026-09-12.md)。
Windows SQLite 状态提交/撤销各 4 次 FlushFileBuffers，Linux 各 5 次 fsync，M4/M5 一致。
File 撤销在保留前缀增长 100 倍时仍保持近乎固定读写量；撤销不新建状态版本，只读和无变化回调零写入、零同步。
Linux 状态读取的 8 字节写入经追踪确认为 SQLite 的共享内存页扩展，没有状态数据提交；报告保留此测量边界。
两种后端均保持既有持久化策略。M5 的 POSIX 恢复与原生同步计数缺口已闭合。

此前 Windows 分组回归记录仍有效，存在重叠，不合计用例总数：事件/观察聚焦组 110 项通过，
TUI 聚焦组 56 项通过；快照/生命周期组 194 项通过，订阅故障注入修正后 6 个初始化失败场景通过。
Runtime/Harness/真实 Plan/Goal 等集中组 210 项通过，另 2 项工具启动 1 秒超时，在 M4/M5 单独执行均通过；
未延长超时。既有 TUI 子会话审批测试的 2 秒启动超时也在 M4 复现，留在 M6 全仓库门槛中复核。

M5 收尾质量检查：架构边界 17 项通过；三份改动 Python 文件的 Ruff、格式和编译通过，
29 个本地文档链接、144 条测量记录一致性与 diff 空白检查通过。M6 全仓库测试、规范同步和最终审计仍未开始。

## 9. M6：集成验收与规范收敛

依赖 M1–M5，不能把尚未通过的链路标成“后续补充”。

- [ ] 汇总 AC-01–AC-16 的测试入口、平台、后端、插件形态和结果；故障注入证明恢复不会执行外部工具。
- [ ] 运行架构边界与聚焦测试，再执行仓库完整质量门槛。既有失败与本次引入失败分开记录，不能把失败写成通过。
- [ ] 检查所有 canonical 写入和恢复入口已接入同一机制，无 SDK 直接 save 旁路或插件特例。
- [ ] 更新现行会话持久化、核心运行时、工具/HITL、插件、上下文、观察、CLI/TUI 与性能规格中实际改变的章节。
- [ ] 同步插件开发指南、Goal 说明、message-history-editing、示例和 CHANGELOG；只宣称通过验收的行为。
- [ ] 本目标规格验收后标为现行规格，相关索引同步；按仓库惯例移除已完成计划，不长期保留重复实施日志。
- [ ] 最终评审新增抽象是否有单一职责，是否删除了被取代的旧路径，是否存在无关需求或未使用扩展接口。

完整质量门槛沿用 CONTRIBUTING.md：

```bash
uv sync --frozen --all-groups
uv run pytest --cov=ouro --cov-report=term-missing
uv run ruff check ouro tests
uv run ruff format --check ouro tests
uv run mypy ouro
uv run python -m compileall -q ouro
uv build
git diff --check
```

开发过程优先运行当前阶段相关测试；完整质量门槛用于实现交付，不要求每次仅编辑本计划时重跑全部运行时测试。
文档修改检查链接、编号追踪、术语一致性和 diff 空白。

## 10. 最终交付与停止条件

交付包含原生状态/提交实现、插件 SDK 与 worker 契约、内置插件迁移、撤销与恢复入口、Host 接入、
可重入数据迁移、AC 矩阵证据及同步后的现行规范。

全部验收完成后本次工作结束。不得把 redo、通用时间线平台、任意效果 DSL、额外 contribution、插件分发重写
或其他路线图能力塞入收尾；也不得以这些能力尚未存在作为本次完成的前置条件。

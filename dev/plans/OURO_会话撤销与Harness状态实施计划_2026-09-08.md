# OURO 会话撤销、Harness 状态与插件交互实施计划

> 文档类型：本次改进实施计划
> 状态：待实施；所有里程碑尚未完成
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

- [ ] 演进 owner 状态和 controls 为一个 canonical Harness 聚合；定义不可变版本、当前引用、消息锚点、
  history_epoch、有限的提交数据和明确错误语义。类型按职责放置，不创建第二份 Session 状态机。
- [ ] 规定操作中多消息/多状态变化的逻辑顺序，并用工具结果、输入批次和无消息命令样例验证锚点。
- [ ] 在 SessionRepository 定义短提交和一致读取端口；SQLite 实现事务，File 收敛 history-cut 恢复机制。
- [ ] 把消息/状态/挂起或队列意图/结果纳入可恢复决定；提交事件使用既有日志并按操作身份去重。
- [ ] 所有目标 Session canonical 读取路径接入未完成决定恢复；Catalog 查询保持既有只读投影边界。
- [ ] 建立新旧格式 fixtures、版本升级与迁移基线规则；有活跃 owner 时拒绝并发迁移。保留消息索引与尾部裁剪。
- [ ] 注入快照写入、决定持久化、消息更新、状态切换、事件 append 和完成标记各故障点。

退出条件：两后端通过 HS/AN/CM 的底层契约测试；状态与消息不存在可观察的半提交；恢复不调用插件。
AC-01、AC-06、AC-07 的底层部分和 AC-15 基线测试通过。此阶段不开放新的 /undo。

## 5. M2：插件调用范围与 Todo 纵向链路

依赖 M1。保留不可变 Catalog、Generation gate/lease 和现有 contribution 类型。

- [ ] 为现有 callback 适配链加入显式调用范围：owner/Session 绑定、快照、只读或暂存写、取消、关闭与有效性。
- [ ] 从 PluginContext 移除长期 canonical 写能力；共享最小状态视图和变更封装，不向 SDK 泄露存储端口。
- [ ] 明确 readonly contribution 和可变更 contribution 的授予规则；合并决策前不提交任何策略提案。
- [ ] worker 随调用传入快照、随结果返回变更，与 in-process 共用验证和结算；显式升级 SDK/协议与执行契约。
- [ ] 验证同步 handler、异步 handler、嵌套 callback、保存旧视图的后台 task、worker 超时/迟到的失效行为。
- [ ] Todo 完整迁移：读取、修改、工具结果共同提交、失败无写入；维护一个最小 in-process 与 worker 状态示例。
- [ ] 实例资源任务继续由现有 task group 管理；会话效果复用现有调度入口，确认不存在后台直接保存的旁路。

退出条件：Todo 验证新机制的正常、冲突、失败和恢复链路；只读写入/越界/关闭后写入明确失败。
AC-03 的 Todo 部分、AC-05/06 及插件形态等价性通过；不为 Plan/Goal 添加临时特殊撤销实现。

## 6. M3：控制、交互与续跑统一结算

依赖 M2；这是从“新增状态 API”转为“规范框架管线”的主要工作。

- [ ] 重构 SessionControlCoordinator 的 acquire/release/replace：嵌套 callback 共享提案，claim 提交时校验，
  controller 业务关系与 owner 状态共同生效；准备/失败过程进入操作记录，移除半完成业务状态发布路径。
- [ ] Tool/Command 输出接入应用提交边界；外部效果的事实归属与 canonical Harness 结算分别表达。
- [ ] 把 ToolInputRequest、审批和等待阶段的状态/恢复身份一并提交；恢复使用新调用范围并检查原等待记录。
- [ ] Continuation 使用现有 durable settlement plan，纳入状态提案和稳定去重；多策略失败/冲突无部分保存。
- [ ] Plan 迁移模式与业务值，去除旧 pending 执行许可混存；审批记录保留原生所有权，计划文件不回滚。
- [ ] Goal 从原生 Run 控制快照取得执行绑定；稳定目标身份关联原生 usage，Context 不保存、计费不随快照回退。
- [ ] 编写已知 Todo/Plan/Goal schema 的一次性迁移，处理可验证 usage 基线；不确定历史明确拒绝或保留边界。
- [ ] 删除被替代的插件存储锁、重复持久化 revision、run_binding 与补偿路径，保留必要业务校验。

退出条件：激活、replace、等待、取消、异常终结、完成、续跑均不产生模式/数据错位；schema 迁移可重入。
AC-04、AC-09 的交互部分、AC-14，以及 AC-03 的 Plan/Goal 部分通过。

## 7. M4：精确撤销、排队输入与执行有效性

依赖 M3。底层契约完整后才替换原撤销限制。

- [ ] 原生撤销接受任意消息角色和精确 before_message_id，恢复消息前版本，递增代次，修正消息头与派生状态。
- [ ] 从裁剪前置条件中移除工具帧限制；Provider 投影处理不完整交换，保留原切口并提供明确诊断。
- [ ] 应用入口按真实来源只接受直接用户输入，支持 message_id/queue_id 和约定的默认选择，返回完整输入。
- [ ] 消费与 acknowledge 收敛为原子语义；取消/消费竞争按 queue_id 定位，不出现双消费或误选其他输入。
- [ ] 撤销保留其他待消费直接用户输入，不自动 drain；旧 epoch 自动输入和交付失效，恢复消费重新检查策略。
- [ ] 将代次检查接到工具结果、状态命令、Continuation、挂起恢复、后台调用和 child delivery 所有会话效果入口。
- [ ] /continue 在撤销后基于当前历史创建新执行，不恢复旧 Run checkpoint，不重复添加保留输入或补交撤回结果。
- [ ] 保留 parent/child 审计；按既有归属取消相关工作，以父提交检查拒绝迟到投递。
- [ ] 用受控同步点覆盖执行所有权、中断结算、命令/工具/撤销竞争；提交后取消按实际 durable 结果处理。

退出条件：AC-01/03/06/08/09/10/12 全部对应场景通过；不存在按插件 id、消息格式或 delegation 存在性
建立的 blanket 撤销禁令。所有新增成功路径都有相应失败与恢复证据。

## 8. M5：Host 观察、输入与会话生命周期

依赖 M4，复用现有应用 DTO、资源引用、草稿和订阅能力。

- [ ] 提交通知和 initial/live snapshot 关联 history_epoch；lag/重连后读取一致的消息、状态、活动和队列。
- [ ] TUI/CLI 接入精确撤销结果；成功后标准重载，再恢复正文、附件和技能；失败不丢原草稿。
- [ ] 旧事件按归属过滤，不读取已撤回消息、不吞掉当前历史的真实 PersistenceError。
- [ ] 验证重启后的活动恢复、中断再提交、多 Host 观察、同长度/增长重写及独立 reader 索引有效性。
- [ ] 导出/导入、trash、File/SQLite 转换保留可达版本与锚点；分支 copy/reset/reject 一致处理状态和版本引用。
- [ ] 完成旧会话迁移集成：基线前不可恢复边界明确可诊断；插件缺失时状态仍保留，执行报契约不可用。
- [ ] 做性能对照：固定短后缀、100/10,000 条保留消息、多 owner 与不同 Harness 大小、无变化回调、只读并发。
  记录耗时、读取/写入量、fsync 次数和快照数，验证 PF-01；不为通过基准降低持久化保证。

退出条件：AC-02/11/13/15/16 通过；实际 Windows TUI 验证中断→撤销→编辑→提交→重启进入会话。
另覆盖 POSIX 的文件提交恢复语义。测试使用临时 OURO_HOME，不修改用户真实会话或计划制品。

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

# Goal 插件规格

> 文档类型：现行规格
> 范围：`ouro/builtin_plugins/goal/` 及其使用的公开 SDK、Application 契约。
> 架构约束见[插件系统规格](plugins.md)和[核心运行时规格](runtime.md)。

## 1. 目标与边界

Goal 为当前主会话保存一个用户明确要求的持久目标。系统在安全执行边界继续工作，模型依据证据声明完成或阻塞；
用户控制暂停、目标修改、取消和预算；用户或模型可显式恢复目标。一次回答结束不代表目标完成。

插件组合公开的命令、工具、上下文、会话控制和续跑能力，不拥有第二套执行循环、调度器或用量账本。
每个主会话最多保存一个最新 Goal；完成记录保留到取消或新建，不提供目标历史列表、子目标树或定时调度。
执行目标所用的工具仍受各自权限、审批和工具策略约束；子会话不能创建或继承主会话 Goal。

Goal 的命令语义、状态机、预算判断、空转暂停和模型提示均由插件定义。框架只提供通用的状态提交、
控制仲裁、输入归属、执行事实和用量查询，通过公开 SDK 交给任意插件；不识别 Goal 的身份、状态值或消息标签。
执行事实不自带继续或暂停策略，其他插件可以对相同事实作出不同决定。

## 2. 状态与事实来源

owner-bound Harness 使用 schema 4，保存以下业务字段：

| 字段 | 含义 |
|---|---|
| `goal_id` | 创建时分配的内部身份；修改、暂停和恢复保持不变 |
| `objective` | 用户指定的目标原文 |
| `status` | active、paused、blocked、budget_exhausted、complete |
| `token_budget` | 累计总 Token 预算；null 表示未设置 |
| `created_at`、`updated_at` | 程序维护的业务时间 |
| `report` | 完成、阻塞或系统停止报告；活动目标为 null |

原生控制视图提供 activation 和请求绑定，原生 usage ledger 提供实际消耗。插件不复制控制关系、累计用量、
存储 revision 或重复结算标记。公开状态隐藏 goal_id，增加 tokens_used 与 tokens_remaining：
用量不可用时为 null，不能当作零；有预算且用量可用时，剩余量为 max(budget - used, 0)。
用量变化不重写业务时间；恢复和撤销不删除已发生的消耗。状态采用 reset branch policy，只支持 schema 4。
Goal 声明 goal-context 和生命周期通知的 projection semantic tags；会话分支通过通用机制清除这些消息与目标状态，
保留普通对话。

| 当前状态 | 允许的变化 | 自动继续 |
|---|---|---|
| 无目标 | 命令或 CreateGoal 创建为 active | 否 |
| active | 模型 complete/blocked；用户 pause/replace/cancel；系统暂停或预算耗尽 | 通过边界检查后可以 |
| paused / blocked | 模型 active；用户 resume/replace/cancel | 否 |
| budget_exhausted | 用户调整预算后 resume/replace，或 cancel | 否 |
| complete | 读取、取消或创建新目标 | 否 |

取消删除记录，不是额外状态。Plan 与 Goal 共同独占主会话行为；冲突明确拒绝，不自动覆盖另一模式。

## 3. 用户命令

```text
/goal
/goal status
/goal [--token-budget N | --no-token-budget] <objective>
/goal pause
/goal resume [--token-budget N | --no-token-budget]
/goal replace [--token-budget N | --no-token-budget] <objective>
/goal cancel
```

无参数 /goal 与 status 只读；cancel 删除目标。命令要求目标会话；TUI 可按需创建，CLI 使用已有会话。
N 必须是正整数，两个预算选项互斥。objective 中的预算文字不自动成为系统预算。

| 操作 | 前置条件 | 省略预算选项 |
|---|---|---|
| 创建 | 无未完成 Goal | 不设置预算 |
| pause | active | 保留预算 |
| resume | paused、blocked 或 budget_exhausted | 保留预算 |
| replace | 存在未完成 Goal | 保留预算 |
| cancel | 存在 Goal | 删除记录 |

resume/replace 的有效总预算必须大于已用 Token；用量不可用时不能验证有限预算。
--no-token-budget 显式清除限制；已耗尽预算必须提高或清除后才能恢复。

### 3.1 创建与原子提交

/goal 直接请求 acquire mode controller。创建回调保存目标，Application 将状态、控制关系和启动输入作为同一原生
提交发布。成功返回时目标已为 active，不依赖模型调用 CreateGoal，也不把工具审批当作命令创建步骤。
参数错误、未完成目标、控制冲突或提交失败不能留下部分状态或孤立输入。

启动输入为不可拆分的有序批次：

1. role=user、scaffold=true、semantic_tag=goal-create：说明目标已经创建、无需再次创建，以及行动规则。
2. role=user、scaffold=false、semantic_tag=None：保留用户目标原文。

启动通知独立包含目标、预算和行动规则。首个目标请求已有目标绑定，其实际用量归入目标。
覆盖 complete 记录分配新身份、时间和零起始统计；已发出的旧请求不追溯归属新目标。

### 3.2 生命周期控制

pause 更新目标状态并返回通用 `run_action=stop`；框架将状态与当前执行的取消意图一同提交，
通过现有取消信号停止模型请求和工具执行，并等待当前执行安全收尾。状态改变不撤回已经发生的外部操作。
resume 更新状态并返回 `run_action=continue`。框架恢复已有执行检查点；已结束的对话从保留历史开始新的执行；
尚未开始的会话消费原有创建批次。已完成工具的结果保留，不重复执行；缺失结果由通用工具恢复协议处理。
插件不选择 Run、不处理锁、工具批次或检查点。暂停只终止当前执行，不锁住会话后续输入。

pause/resume 不额外排入执行输入。下一次模型请求准备时，当前目标状态作为 user 脚手架追加到历史。
二者旋转 activation，保留目标身份、创建时间、进展、预算和消耗。replace 更新目标并清空旧报告；cancel 释放控制并删除记录。

创建、replace 与 cancel 输入共用 owner/command 范围内的 goal-lifecycle 合并键。替换或取消整批覆盖未消费通知，
包括创建批次中的目标原文。暂停保留尚未消费的初始输入供恢复使用，不额外启动消费。
已提交命令的审计记录与 canonical messages 保持不可变。自动继续输入绑定 activation，失效时由原生控制机制撤销。

## 4. 模型工具

GetGoal、CreateGoal、UpdateGoal 始终同时暴露，保持工具前缀稳定。
三者均声明 permission_control=tool、permission_mode=auto，由插件控制权限；工具权限面板标记为
“Plugin controlled”和“Allow automatically”，不提供 Host 权限覆盖。状态校验、控制冲突与工具策略仍然生效。

### GetGoal

无参数，返回当前或最新完成目标的公开状态与实际用量。无记录返回 goal_not_found。

### CreateGoal

参数为非空 objective 与可选正整数 token_budget。仅在用户或 system/developer 明确要求建立目标时使用；
支持自然语言请求，不要求 slash-command 标签。普通任务再复杂也不自动升级为 Goal，拟定草案不等于激活目标。
目标忠实保留用户指定内容；预算只使用明确要求的数值。

工具与命令共用创建转换、状态校验和控制仲裁；工具按插件声明自动执行，不单独请求 Host 审批。
创建调用所属原请求不回溯绑定新目标，后续请求才获得新绑定。遇到未完成目标明确拒绝，不偷偷替换。

### UpdateGoal

参数为 status: active | complete | blocked 和非空 report：

- active：根据当前请求与证据自主决定恢复 paused 或 blocked 目标；报告说明恢复依据，成功后继续已有工作。
  保留目标和预算；已耗尽或无法验证的预算不能绕过，必须由用户处理。
- complete：目标已达成、结果经过验证、无剩余必需工作；报告包含交付物和验证证据。
- blocked：同一阻碍连续至少三个目标工作轮次阻止实质进展，无法在没有用户输入或外部变化时继续。
  报告包含已完成内容、尝试、阻碍和解锁条件。
- 一轮指由用户启动/恢复或自动继续消息发起的工作与结果检查；同轮工具重试不是额外轮次。
- 恢复 blocked 目标后重新审查，不沿用之前的连续次数，也不为凑次数重复无效操作。
- 满足终态条件时必须调用工具并停止目标工作，不能只口头宣布完成或阻塞。
- 工具不能修改目标、预算、暂停或伪造预算耗尽。预算不足和工作困难本身不是终态证据。

系统验证状态、参数和请求绑定，不解析报告来裁决完成证据，不维护语义阻塞计数器。
更新要求请求 activation 匹配；complete/blocked 还要求当前 active 且 usage_scope 匹配。旧请求返回 stale_goal_update。
complete 释放控制并保留记录；blocked 保留可恢复控制，停止目标用量归属。

## 5. 模型上下文与通知

目标状态与生命周期通知均使用 role=user、scaffold=true，并保留插件来源；user role 不证明来自真人，也不增加权限。
语义标签由统一 envelope 渲染器处理，目标文本不自行拼接成协议控制结构。

每次模型请求准备时，goal-context 从原生调用视图读取目标、状态、总预算、必要报告与行动规则，
通过 persistence=message 将状态快照追加到 canonical history 尾部。创建、完成、暂停、恢复、替换和取消
不改写已有消息或 system prompt；后续状态由追加消息表达。取消后的通知通过 goal-cancelled 输入交付。
上下文压缩后仍追加该边界的目标快照；必需上下文不可用时阻止请求。执行绑定与目标快照来自相容的请求边界。
上下文准备失败后重试同一模型轮次时，状态变化仍追加新快照；未变化的重复投递由通用 Context Provider 机制去重。
每条快照只描述记录时的状态；最新追加的通知或快照提供后续行动依据。实际消耗通过 GetGoal 与续跑通知提供，时间戳由 GetGoal 返回。

| 事件 | 必须表达的事实与动作 |
|---|---|
| goal-create | 目标已激活，无需再次创建；开始工作 |
| goal-continue | 继续已有授权；审查证据，完成、阻塞或采取下一步 |
| goal-updated | 使用更新目标；保留仍适用成果与约束 |
| goal-cancelled | 当前目标已取消；正常处理后续请求 |

系统因异常、用量不可用或自动执行未调用工具而暂停时，report 写明系统原因，不声称用户要求暂停。
预算耗尽报告包含实际用量、总预算和恢复方式。报告供状态查询与后续上下文使用，不为通知另启动模型请求。
活动目标引导要求复用已有进展、遵守用户范围和约束、以证据审查结果；结束回答或预算不足不代表完成。
文案不解释 controller、activation、Harness、事务等实现概念。无预算显示未设置，未知用量显示不可用，不伪造零。

## 6. 续跑与空转保护

完整执行结束后，通用续跑机制提供终态、请求绑定、用量和执行事实，依次检查：

1. 无当前目标/控制/绑定，或绑定过期：不要求续跑。
2. 目标已停止或完成：不要求续跑。
3. 执行异常结束：以 paused 保存系统原因。
4. 有预算但用量不可用：暂停，不猜测消耗。
5. 用量达到预算：保存 budget_exhausted，不创建新的目标执行。
6. 自动继续发起的执行无工具调用且目标仍 active：系统暂停并记录原因。
7. 其他情况：提交当前目标和用量的自动继续消息。

空转保护不等于语义进展判断，也不替代阻塞审查。首次用户工作、用户补充输入以及等待用户/审批/子任务的执行不因此暂停。
执行事实由原生消息投影，插件不另存统计。RunOutcome 的 tool_call_count、automatic_continuation 表示可用执行事实；
不可用时为 null，不能用未知值触发空转保护。自动继续低于排队用户输入、活动执行、审批和控制释放。

## 7. 停止期间的交互

paused、blocked、budget_exhausted 不阻止普通会话输入、`/continue` 或正常工具使用。
这些操作本身不改变目标状态、不恢复自动继续、不清除预算。模型上下文明确目标仍停止。

模型判断可以继续目标时，先调用 UpdateGoal(active)，成功后再开展目标工作；用户也可执行 /goal resume。
所有工具仍遵循正常权限和审批；Goal 不将停止状态转换为整个会话的只读限制。退出目标模式使用 cancel。
讨论用量仍计入正常会话账本，但停止控制没有目标 usage_scope，不计入目标预算。

## 8. 用量、恢复与权限

预算在完整目标执行边界检查，允许最后一轮超出，不截断工具帧或流式参数。
同轮明确 complete/blocked 优先于预算停止。原生账本按 Provider 请求身份去重，归属固定为请求开始时的目标。
迟到消耗保留原归属；释放控制后的确认回复不属于已完成目标。暂停、恢复、修改、撤销和重复结算不清零或重复计数。

控制转换、Harness 与队列共同提交；进程恢复不重复创建、消费批次或结算。
不存在排队输入时不因 Host 启动自行开始工作。插件关闭、状态损坏或绑定不明时不能继续未知工作。
Goal 不直接访问内部 SessionStore，不自行实现存储并发检查。

## 9. 贡献与验证

插件 ID dev.ouro.goal，默认启用：

- Command goal；Tools get-goal、create-goal、update-goal。
- Controller mode，独占 ouro.session.primary_behavior。
- Context goal-context；Continuation Policy goal-continuation。
- 权限 sessions.write、background.run；不申请文件、网络或 shell 权限。

针对性验证覆盖：命令直接创建、自然语言工具创建、原子拒绝、立即控制、两种队列消费方式、旧请求隔离、
压缩后投影、预算继承/修改/清除、缺失用量、自动空转停止、暂停取消与无消息续跑、停止期间普通交互、模型主动恢复、最终用量及恢复撤销。
公共执行事实覆盖 SDK/worker 传递及通用续跑边界。

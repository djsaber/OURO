# Host 会话观察与子智能体导航规格

> 文档类型：现行规格

本规格定义 Host 观察任意 OURO Session、从父工具调用发现子智能体 Session，以及可靠衔接 snapshot 与 live 事件的
通用契约。TUI 的具体交互是该契约的一个 adapter；Desktop/Web Host 可以采用不同策略。

## 术语

- **控制会话（controlled Session）**：Host 当前允许写入或执行控制动作的 Session。
- **可见会话（visible Session）**：Host 当前渲染的 Session。它可以不同于控制会话。
- **观察状态（observation state）**：某 Session 的事实快照、已确认位置和派生投影。
- **关系事件（agent relation event）**：描述 Delegation/Agent 生命周期而非 child 内容的 `agent_*` 事件。
- **已确认位置**：消费者已经应用的事实修订号、消息位置和当前请求的草稿偏移。

## 强制架构不变量

1. Core/Runtime 不得依赖 TUI widget、页面、点击、返回或只读策略。
2. 只读可见会话切换不得改变 Session/Run/Delegation 生命周期，不得隐式 abort、resume、spawn 或提交输入。
   选择控制主会话时的显式中断恢复独立于观察快照与订阅，由 Supervisor 尊重现有执行归属。
3. Host 只能通过 Application service/DTO 解析并观察 child，不得读取 delegation 文件、调用 store 或解析工具展示文本。
4. child 导航身份必须至少包含 `parent_session_id`、`parent_run_id`、`parent_batch_id`、`parent_tool_call_id`、`delegation_id` 和
   `child_session_id`；缺失或不一致时 fail closed。
5. ToolCall 身份必须在所属 parent Session/Run/batch 内解释，不能假设 `tool_call_id` 跨批次唯一。
6. 在线 event sequence 只在当前进程的交付来源内排序，不是持久游标，也不得跨 Session 或进程比较。
7. 状态与成果先提交，再发布在线通知；不永久保存 EventRecord。实时队列有界，遗漏可以检测。
8. 快照的事实、修订号和草稿在同一只读边界内取得；后续观察按已确认位置读取新增正文和变化后的状态。
9. 本地通知与补齐重叠时，按提交位置、消息身份和请求偏移去重；一个提交的最后一项通知才确认完整提交。
10. 观察订阅关闭或切换可见会话不得关闭 child Session 或改变 child Run；共享 Application 中关闭单个 Host adapter
    也不得关闭 Runtime。拥有整个 Application/进程的 composition root 仍按明确的 Host shutdown policy 释放 Runtime。
11. `session_kind`、root/parent identity 是 Application 事实；“subagent 在某 Host 中只读”是 Host 策略。
12. TUI 中控制会话必须与可见会话显式分离。child 可见时，所有会写入 child 的用户交互必须被禁用且不得错误
    路由到主会话或 child。
13. 不可见 Session 可以暂停 UI 渲染，但再次打开时必须从最后确认游标或 canonical snapshot 收敛后再宣称同步。
14. 任一会话的 lag、重连或渲染错误不得污染其他 Session 的投影和游标。
15. Session 观察不依赖执行所在的 Runtime 或进程。共用 home 和会话后端的 Host 必须能够持续观察另一进程
    写入的事实与当前草稿；Run 完成后，订阅仍能接收同一 Session 的后续 Run。

每次事实提交推进一次会话修订号。Run、Input 状态、Call 状态和摘要各保存自己的最新修改号，
补齐查询只读取发生变化的当前记录，不保存历史变更清单。消息使用已有顺序索引，草稿使用已有请求偏移。
本地通知直接携带执行者已有的内容，不为每条通知重新查询存储。工具结果、授权、失败原因与执行恢复依据
分别由原生事实负责，不依赖通知是否成功送达。

## Application 关系投影

`AgentTreeNode`（或等价的稳定 child-navigation DTO）必须投影：

- `root_session_id`、`parent_session_id`、`parent_run_id`、`parent_batch_id`、`parent_tool_call_id`；
- `delegation_id`、`child_session_id`、当前 `child_run_id`；
- Agent 稳定 identity、展示名、execution mode、lifecycle binding 和状态；
- relation revision。

Host 以 `(parent_session_id, parent_run_id, parent_batch_id, parent_tool_call_id)` 在已授权的 root tree 中解析 child。
没有匹配返回“未发现”，多条匹配或 backlink 不一致必须报告 diagnostic/错误，不能任选一条。
应用查询允许省略批次来查找唯一匹配；宿主持有批次身份时必须传递，不能依赖省略后的歧义检查。
`RuntimeToolCallActivity.batch_id` 在正式调用时来自原生批次；尚未形成 Call 的参数流投影没有批次身份。
历史条目更新、可变条目排除和委派关联使用相同的完整身份。

工具活动从已加载的 Call Start 投影开始时间；存在 Outcome 时使用其执行起止时间。
`tool_call_completed` 通知携带同一执行计时，事件处理和快照投影均以此为准，结果消息保留相同值。
计时不依赖 Host 是否观察到开始事件，也不以通知创建或送达时间代替工具结束时间。

`AgentHandleView` 若作为 SpawnAgent 工具的 structured result 返回，也必须携带足以与上述关系做一致性校验的 parent
identity；handle 不是 canonical 事实，重连后仍以 Agent tree/snapshot 为准。

## RuntimeEventCategory

`RuntimeEventCategory` 必须包含：

```text
text, reasoning, tool, interaction, run, compaction, lifecycle, agent, unknown
```

分类规则：

- `agent_spawned`、`agent_result_ready`、`agent_result_delivered`、`agent_closed` 归类为 `agent`；后续新增的
  Delegation/Agent 关系事件若没有更具体语义，也归类为 `agent`；
- child Session 的 text/reasoning/tool/interaction/run/compaction/lifecycle 事件仍按各自内容语义分类；
- category 不表达事件 actor。actor/session ownership 由 `session_id`、relation identity 和 provenance 表达；
- 未知事件必须保留为 `unknown`，不得为了隐藏 schema 漏洞默认为 `lifecycle` 或 `agent`。

`agent_spawned` payload 必须包含：

- `root_session_id`、`parent_session_id`、`parent_run_id`、`parent_batch_id`、`parent_tool_call_id`；
- `delegation_id`、`child_session_id`、`child_run_id`；
- `agent_id`、`execution_mode`。

其他 `agent_*` 事件必须至少包含 `delegation_id`，并在关系已绑定时包含 child identity。Application 投影必须对要求
字段做类型验证。

## Session snapshot 与已确认位置

`SessionObservationSnapshot` 包含 Session、选定消息窗口、相关 Run 与摘要、待处理输入、当前工具与交互、
当前请求小头、有效上下文指标和当前 Draft。`events` 只承载当前 Draft 的显示分片，不包含历史通知。
`position` 包含事实修订号、消息位置和草稿偏移；分页时附带仅在内存使用的事实页位置。

快照与后续事实页都在 `SessionRepository.session_read` 内取得一致数据。SQLite 使用短读事务，Files 使用短会话锁；
嵌套查询共享资源寿命。存储边界在通知脱敏、界面排版及异步等待前释放。观察不启动执行、申请 Run 所有权或恢复任务。

`observe_session(history_limit=...)` 可选择近期窗口，省略时读取完整历史。`history_window` 按消息顺序返回消息、
所属 Run 和锚定在该页的摘要。摘要通过消息顺序范围关联已有索引，查询参数不随页内消息数量增长。条数是软限制，完整模型轮次、工具帧和同批输入不能被初始历史窗口拆开。
`before_cursor` 仅对所属 Session 和 `history_epoch` 有效；Files 按正文偏移、SQLite 按消息索引定位，不读取无关前缀。
`SessionHistoryAnchor(history_epoch, first_sequence)` 保留已加载窗口起点，向前加载历史不推进实时确认位置。
完整导出不受显示窗口影响。只有撤销等历史代次改变才替换快照；普通继续运行保留已呈现历史。

## 统一增量观察

`RuntimeSupervisor.observe_session` 返回 `SessionObservation`。订阅先登记，再读取一致快照；共享跟踪器的初始
活动与起始位置也在同一读取边界内取得，避免旧状态配上新位置而漏掉变化。

`get()` 返回一页 `SessionObservationBatch`：初次包含 snapshot；后续包含当前事实、新消息或在线通知。
消费者应用后调用 `acknowledge(batch)`，才推进 `applied_position`；未确认时重复 get 返回同一页。
每个消费者独立确认，慢消费者不能阻塞执行者或其他观察者。

跨进程观察、重进会话和缓存遗漏统一调用 `SessionService.observation_changes`：

1. 读取会话修订号与当前请求小头。事实未变化时只续读当前草稿。
2. 事实变化时按索引分页读取修改过的当前记录，按消息位置读取缺少的正文。
3. 正文和状态追齐后继续读取草稿，避免新输出越过缺少的旧正文。
4. 应用并确认实际消费的位置。未收到通知不影响下次发现变化，不重建过去的生命周期通知。

每个被观察 Session 在同一 Supervisor 内共享一个跟踪器，后台事实页最多 128 条；消费者遗漏后的补齐页大小由
观察参数控制。运行或排队时最多每 100 ms 检查外部写入，空闲时每 500 ms；本地缺口立即唤醒。
所有磁盘读取在受管理线程执行，正文与状态观察全部关闭后停止读取。
历史代次改变时，共享跟踪器同时重置消息位置、事实分页位置和草稿偏移；消费者只重建失效的快照。
旧代次的 Run 仍可保留审计事实，但不能恢复为当前活动或待处理交互。

本地连续通知直接交付，不等待轮询。提交通知携带提交修订号及其之前的消息位置，最后一项携带完整确认位置。
若缺少早先正文，则交给同一事实读取管线补齐，避免晚到的旧正文排在新输出之后。纯在线进度不确认事实提交。
状态订阅只保留一个合并通知；Draft 增长不推进事实修订号，也不逐字刷新任务树。

当前响应使用 `ResponseStreamPosition(context_id, after_cursor, cursor)`。草稿保存可恢复前缀后即交付本地通知；
工具参数片段仅在线预览。正式 assistant 消息提交后关闭 Draft，遗漏的正文由消息补齐，已显示前缀不重复追加。
新请求重置草稿偏移。跨进程读取不会永久保存或恢复每一个 token 的原始切分。

`SessionContentProjection` 负责通用正文、活动、工具与交互投影。`assistant_text`、`reasoning_text` 只保留当前请求，
提交后释放原分片对象，下次请求清空。它不依赖 Rich、Textual、布局、滚动或执行控制。历史 Run 结束与摘要展示按
消息边界排列；Host 只把缺少的内容追加到已有视图，不重新排版整段历史。

## 历史代次与失败

Session、消息页和活动投影携带 `history_epoch`；Run 通知保留其所属代次。撤销递增代次，保留业务审计事实，
不存在永久通知历史。旧代次通知在解析消息引用前过滤。新代次通过标准快照入口重新同步。

共享读取失败向依赖该来源的订阅交付 `runtime_subscription_read_failed` 并停止来源；单消费者补齐失败只关闭
该观察并返回 `session_observation_read_failed`。缓存溢出报告 `runtime_subscription_lagged`，从已确认位置补齐。
错误不会被静默当成同步完成，也不会触发无限历史重载；重新打开是明确的重试入口。

## TUI adapter 行为

### 多 Host 状态与时序

- Supervisor job/handle 描述单个进程的一次执行尝试；已绑定 Run 的 Session 活动由 Run 等当前事实和在线通知共同投影。
  排队接管、竞争失败或旧 job 延迟返回不得覆盖较新的会话状态。调度器仅在尚未绑定 Run 时投影临时排队/启动状态。
- 持续观察时，活动查询复用共享跟踪器维护的投影，不逐次读取磁盘。未持续观察的缓存不可用于新订阅或按需查询，
  因为其他 Host 可能已更新同一个 Run。
- 按需状态查询读取 Session 引用的 active/last Run，不枚举全部 Run。活动快照在同一 Session 只读边界内读取
  Run 和事实位置，避免“旧状态配新游标”漏掉终态变化；该读取仍遵循受管理线程与有界查询的既有约束。
- 失败、中断或可恢复 Run 保留的 ToolBatch 属于恢复检查点。实时投影与重新打开时均将未结算工具投影为对应终止
  状态，保留已提交结果并停止动画；耗时优先使用持久化结束时间，不修改恢复检查点本身。

### 视图行为

TUI 必须实现：

1. 主会话保持 controlled；visible 可以是主会话或其已知 child。
2. 历史和实时 SpawnAgent 工具展示均携带 `(parent_session_id, parent_run_id, parent_tool_call_id)` 点击身份。
3. 点击只能通过 Application relation 投影打开 child；不存在/尚未绑定时不猜测。
4. child 视图明确显示只读状态，禁用 composer、提交、abort、交互应答和会话写配置。
5. 从 root Agent tree 投影同一主会话下全部已知 child（包括嵌套派发），按 `parent_run_id` 保留 Run 分组，并提供
   root/child 与 child/child 直接切换路径。
6. 主会话和每个 child 使用独立 projection/cursor；交错事件不能串流、覆盖或错误去重。
7. 切换期间新到事件必须被缓存、投影或在 事实补齐中恢复，不能因 widget 重建而丢失。
8. active root 或 active child 不得成为只读视图切换的阻止条件。
9. 视图切换不应阻塞 Textual event loop 的持续刷新；大历史装载必须分块/yield。
10. 关闭 TUI 必须释放所有观察任务/订阅；复用共享 Application 时不得终止后台 Session。若 TUI 是 Application 的
    composition root，则整体退出遵循既有 Runtime shutdown policy，而不是由会话视图逻辑隐式控制 child。
11. composer 上方的导航面板只消费 Agent tree DTO，不读取 store 或解析工具文本；正文布局保留单行导航区域，
    无 child 时内容为空。空正文页面有 child 时仍显示导航；root/child surface 使用同一 widget，按当前/选中节点
    调整最多十个 child 的有界窗口。已打开 Session 的身份独立于正文是否为空，不因欢迎页布局丢失命令或导航目标。
12. 导航面板必须保持在当前最上层 inline 菜单（命令、审批、问答或计划确认）的上方，使菜单与 composer 相邻；
    运行、结束和失败节点分别使用运行语义色、常规低亮色和失败语义色，不复制颜色常量。
13. 终端协议只产生带 modifier 的按键事件，不产生独立 Alt key-up。`Alt+方向键` 必须立即更新节点选择，但 TUI
    同时只能执行一笔 visible Session 切换；切换期间的新目标覆盖唯一待处理目标，当前切换完成后只处理最新目标。
    不得排队每次重复按键，也不得用全局键盘轮询或超时猜测模拟 key-up。
14. 鼠标悬停会话节点只改变临时选择和详情展示，不触发 Session 观察；移出后恢复当前或键盘选择。左键点击 root
    或 child 节点都通过与键盘相同的单飞切换入口打开目标 Session；child 选中时展示 Agent 名称和任务描述。
15. 安装 Session surface 时先重建 snapshot 内的未完成响应，再接入同一有界事件队列，继续使用原有按帧批量渲染。
    切换后旧订阅不得继续向新 surface 投递事件；安装被取消时必须关闭尚未接管的订阅。
16. Run 结束标签由终态通知或补齐事实驱动的同一呈现逻辑生成，执行端的 `RunResult` 回调不单独追加标签。Host 通过 Application
    按 `(session_id, run_id)` 读取精确 Run 记录，用持久化起止时间计算耗时，并使用该 Run 的模型请求端点名称。
    标签按 Run 去重，在本次内容之后、下次 Run 内容之前插入；不得为结束标签重载整个 Session 历史。

Run 的 `model_endpoint_id` 记录最近一次已提交模型请求的端点身份，与请求次数在同一持久化边界保存。
即使响应中途被中断、尚无完整响应元数据，结束标签仍能使用该身份。尚未提交模型请求的 Run 可以没有端点身份，
此时展示其已记录的响应端点或模型名称。

主 transcript 不自动混入 child 中间日志。Agent tree/HITL 摘要可以继续在 root Host 控制面显示；这不改变 child
完整流只属于 child Session 的事实。

## 失败与诊断

至少定义并测试以下失败：

- parent ToolCall 未找到 relation；
- relation backlink/parent identity 不一致；
- child Session 被回收、损坏或无权访问；
- snapshot 装载失败；
- subscription lag/overflow/closed；
- duplicate/out-of-order event；
- 切换中 Host 关闭；
- 某 child 恢复失败但其他 Session 继续可观察。

错误必须对用户可见且可重试。恢复不得清空 canonical history、推进错误游标或静默切到另一个 Session。

## 验收测试

- 关系 DTO 与 `agent_spawned` 完整 identity/schema 测试；
- `agent_* -> category=agent` 及 child 内容分类测试；
- 快照位置与本地通知、事实页无缝交接测试；
- 有界缓存遗漏后的事实补齐、去重和顺序测试；
- 历史/实时 SpawnAgent 工具点击测试；
- child 只读且 root 控制目标不变测试；
- root 与至少两个 child 交错流、多次切换、切换窗口新事件测试；
- root tree 全层级节点、跨 Run 分组、十节点窗口及 root/child/child 方向键直切测试；
- 导航加载单飞、加载期间最新目标覆盖，以及节点悬停预览和点击切换测试；
- 导航面板与命令/审批 inline panel 的层叠几何和可操作性测试；运行/结束/失败状态色按[测试策略](../development/testing.md)人工验收；
- active Run 期间切换测试；
- 执行端、独立观察端和重载历史的结束标签一致性，覆盖完成、失败、中断、双存储后端和连续 Run 排序：
  `tests/tui/test_run_summaries.py`。
- TUI 退出资源释放且 child 继续运行测试；
- Local 与 SQLite 后端相关契约测试，以及全局质量门。
- 独立 Runtime 持续写入、snapshot 读取期间新增事件、本地/外部事件交错、通知遗漏、共享有界跟踪与
  退出后停止 I/O、读取故障诊断：`tests/application/test_session_event_following.py`。
- 另一 Runtime 自动运行任务，TUI 在工具执行时进入、完成前继续渲染文本、切换离开与返回后补齐并继续流式：
  `tests/tui/test_live_session_attachment.py`；取消安装释放订阅：`tests/tui/test_async_observation.py`。
- 双 Host 交替审批、竞争同一审批、旧 job 延迟结算、子会话初始化失败后同步唤醒父会话：
  `tests/application/test_agent_multi_host.py`；跨 Host 等待态锁交接、超时和取消：`tests/application/test_run_ownership.py`。
- 两个 TUI 同时订阅工具执行与连续 Run、结束标签去重，以及失败工具在实时与重开后的动画停止：
  `tests/tui/test_live_session_attachment.py`；未订阅缓存失效、精确 Run 查询和快照位置一致性：
  `tests/application/test_session_event_following.py`。
- 当前事实索引分页、同次提交多记录与并发修改：`tests/sessions/test_observation_positions.py`。
- 独立进程持续追加大正文、双后端后缀读取：`tests/sessions/test_event_tail_process.py`。
- 慢消费者独立确认、遗漏前缀与本地输出衔接：`tests/application/test_session_observation.py`。

TUI 呈现、导航和只读交互见 [TUI 规格](tui.md)。

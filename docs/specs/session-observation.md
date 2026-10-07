# Host 会话观察与子智能体导航规格

> 文档类型：现行规格

本规格定义 Host 观察任意 OURO Session、从父工具调用发现子智能体 Session，以及可靠衔接 snapshot 与 live 事件的
通用契约。TUI 的具体交互是该契约的一个 adapter；Desktop/Web Host 可以采用不同策略。

## 术语

- **控制会话（controlled Session）**：Host 当前允许写入或执行控制动作的 Session。
- **可见会话（visible Session）**：Host 当前渲染的 Session。它可以不同于控制会话。
- **观察状态（observation state）**：某 Session 的 canonical snapshot、事件游标和派生投影。
- **关系事件（agent relation event）**：描述 Delegation/Agent 生命周期而非 child 内容的 `agent_*` 事件。
- **高水位（high-water mark）**：某 snapshot 已包含的最大 Session event sequence；无事件时使用 `0`。

## 强制架构不变量

1. Core/Runtime 不得依赖 TUI widget、页面、点击、返回或只读策略。
2. 只读可见会话切换不得改变 Session/Run/Delegation 生命周期，不得隐式 abort、resume、spawn 或提交输入。
   选择控制主会话时的显式中断恢复独立于观察快照与订阅，由 Supervisor 尊重现有执行归属。
3. Host 只能通过 Application service/DTO 解析并观察 child，不得读取 delegation 文件、调用 store 或解析工具展示文本。
4. child 导航身份必须至少包含 `parent_session_id`、`parent_run_id`、`parent_tool_call_id`、`delegation_id` 和
   `child_session_id`；缺失或不一致时 fail closed。
5. ToolCall 身份必须在所属 parent Session/Run 内解释，不能假设 `tool_call_id` 跨 Run 全局唯一。
6. 每个 Session 的 event sequence 独立单调；Host 不得跨 Session 比较 sequence 或构造虚假的全局事件顺序。
7. canonical EventRecord 必须先持久化再进入实时 fan-out；实时队列有界且 overflow/lag 必须可检测。
8. snapshot/history 必须暴露一致的 event high-water；live 观察必须支持 `after_sequence`，语义为只交付 sequence
   大于该值的事件。
9. Host 必须按 `(session_id, sequence)` 去重；重连和 snapshot/live 交接不得重复渲染或遗漏 canonical 事件。
10. 观察订阅关闭或切换可见会话不得关闭 child Session 或改变 child Run；共享 Application 中关闭单个 Host adapter
    也不得关闭 Runtime。拥有整个 Application/进程的 composition root 仍按明确的 Host shutdown policy 释放 Runtime。
11. `session_kind`、root/parent identity 是 Application 事实；“subagent 在某 Host 中只读”是 Host 策略。
12. TUI 中控制会话必须与可见会话显式分离。child 可见时，所有会写入 child 的用户交互必须被禁用且不得错误
    路由到主会话或 child。
13. 不可见 Session 可以暂停 UI 渲染，但再次打开时必须从最后确认游标或 canonical snapshot 收敛后再宣称同步。
14. 任一会话的 lag、重连或渲染错误不得污染其他 Session 的投影和游标。
15. Session 观察不依赖执行所在的 Runtime 或进程。共用 home 和会话后端的 Host 必须能够持续观察另一进程
    写入的事件；Run 完成后，订阅仍能接收同一 Session 的后续 Run。

## Application 关系投影

`AgentTreeNode`（或等价的稳定 child-navigation DTO）必须投影：

- `root_session_id`、`parent_session_id`、`parent_run_id`、`parent_tool_call_id`；
- `delegation_id`、`child_session_id`、当前 `child_run_id`；
- Agent 稳定 identity、展示名、execution mode、lifecycle binding 和状态；
- relation revision。

Host 应能以 `(parent_session_id, parent_run_id, parent_tool_call_id)` 在已授权的 root tree 中唯一解析 child。没有匹配
返回“未发现”，多条匹配或 backlink 不一致必须报告 diagnostic/错误，不能任选一条。

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

- `root_session_id`、`parent_session_id`、`parent_run_id`、`parent_tool_call_id`；
- `delegation_id`、`child_session_id`、`child_run_id`；
- `agent_id`、`execution_mode`。

其他 `agent_*` 事件必须至少包含 `delegation_id`，并在关系已绑定时包含 child identity。Application 投影必须对要求
字段做类型验证。

## Session snapshot 与事件游标

Host 可观察的 Session snapshot 至少包含：

- `SessionView`；
- canonical messages；
- 与展示相关的 Run/compaction 投影；
- snapshot 已覆盖的 `event_high_water`；
- 必要的 diagnostics。

snapshot 的 message/event 读取必须定义一致性边界。允许采用“锁内读取”或“订阅先行 + 有序合并”，但必须证明：

- 在边界之前持久化的事件包含在 snapshot 或 replay 中；
- 在边界之后持久化的事件由 replay/live 交付；
- 边界事件最多渲染一次；
- active Run 的 partial text/reasoning/tool 状态可从事件重建。

交互式观察不得在每次 attach 时线性扫描已经结束的 Run event history。Local Files 后端必须以可重建的 Run
字节偏移直接定位 active Run，并以稀疏 sequence→byte checkpoint 为 `after_sequence` replay 选择有界的前向扫描
起点；SQLite 使用索引查询。稳定路径的读取成本应由 canonical messages、active Run 事件和边界后新增事件决定，
而不是由 Session 累计 `events.jsonl` 长度决定。active Run 本身的事件是重建未完成状态所需输入，其读取成本允许与
active Run 大小成正比。

读取最新事件水位时必须倒读到完整的最后一条 JSONL 记录，不能将尾部读取块当作记录边界。事件与消息使用
同一完整记录倒读机制，以 LF 确定边界，并在 UTF-8 字节拼接完整后解析；单条事件可跨越多个读取块。
读取成本由末条记录长度决定，不依赖历史总长度。真正截断或损坏的末条记录必须报告持久化错误，不能跳过。

Local 加速索引不是 canonical 数据：缺失、尾部不完整或损坏不能隐藏 EventRecord 或使 Session 不可读，必须从
`events.jsonl` 安全回退并可惰性重建。旧会话允许首次目标读取执行一次历史扫描来建立 Run offset，之后必须进入
稳定快速路径。派生索引写入是 best-effort、无 fsync 的加速行为，失败不得阻断 canonical event；隐式 0 游标不得
制造无定位收益的磁盘写入。delta 分组耐久性不能因为写入派生索引而退化成每 token fsync。

订阅通知必须包含 Session identity、event sequence 和可区分的 event/lag/closed 状态。发生 lag 时，通知必须给出恢复
所需的最后安全游标或明确要求 Host 重新加载 snapshot。Host 完成恢复后方可恢复 synced 状态。

## 历史代次

Session snapshot、消息点查和活动投影携带 history_epoch；Run 事件携带其不可变归属代次，提交事件携带
原生决定的代次。sequence 继续单调，撤销不删除审计事件。

Host 在解析消息身份之前过滤旧代次事件。点查 `messages_at_history` 在同一读取边界返回当前代次：若历史
已变化则重载标准 snapshot/live 交接；若代次一致而消息缺失则保留真实持久化错误。新代次活动从当前
Session/Run 构建，旧 job 和旧 Run 事件不能覆盖它。增量投影与终态快照使用同一代次规则。

## 持续事件跟踪

`RuntimeSupervisor.subscribe` 提供统一的 Session 事件流，用户、自动任务和子智能体使用同一契约。
`SessionEventStream` 在应用层合并进程内推送和持久化事件，不依赖调度器、任务状态、TUI 或具体存储后端。

- 订阅先登记，再读取 replay；持久化跟踪的起始水位必须在读取 replay **之前**取得，使读取期间的新增事件
  仍被后续增量覆盖。每个订阅开始读取 replay 前，共享跟踪器必须已经建立；并发订阅不得各自在读取完成后
  推进共享起点。replay 与实时通知按 Session sequence 去重。
- 本地连续事件直接推送，不引入逐 token 查询或等待；发现序号缺口时先读取持久化区间，再交付较新的本地事件。
  持久化页提供完整顺序，可以跨过规范历史中不存在事件的序号，不能因此无限重读。
- 每个被观察 Session 在同一 Supervisor 内共享一个跟踪器。使用 `SessionStore.load_events_after` 每页最多
  256 条增量读取；不轮询完整 transcript，不扫描未订阅 Session，不为不同后端增加另一套观察路径。
- 运行或排队时最多每 100ms 检查一次外部写入，空闲或已结束时每 500ms 检查；本地序号缺口立即唤醒。
  长工具没有 token 输出时仍持续观察。磁盘读取在受管理线程边界执行，不阻塞 Host event loop。
- 各订阅保留独立、有界缓冲；慢订阅的 lag 不暂停执行者或其他订阅。最后一个订阅关闭后停止该 Session 的
  增量读取，Supervisor 关闭时等待在途读取完成。
- 持久化读取失败通过 `runtime_subscription_read_failed` 报告，并停止该跟踪器。Host 显示错误并关闭失效订阅，
  用户重新打开会话可重试；不得静默停留，也不得无休止地自动重载历史。

## TUI adapter 行为

### 多 Host 状态与时序

- Supervisor job/handle 描述单个进程的一次执行尝试；已绑定 Run 的 Session 活动由持久化 Run 与有序事件决定。
  排队接管、竞争失败或旧 job 延迟返回不得覆盖较新的会话状态。调度器仅在尚未绑定 Run 时投影临时排队/启动状态。
- 持续观察时，活动查询复用共享跟踪器维护的投影，不逐次读取磁盘。未持续观察的缓存不可用于新订阅或按需查询，
  因为其他 Host 可能已更新同一个 Run。
- 按需状态查询读取 Session 引用的 active/last Run，不枚举全部 Run。活动快照在同一 Session mutation 边界内读取
  Run 和事件水位，避免“旧状态配新游标”吞掉终态事件；该读取仍遵循受管理线程与有界查询的既有约束。
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
7. 切换期间新到事件必须被缓存、投影或在 durable replay 中恢复，不能因 widget 重建而丢失。
8. active root 或 active child 不得成为只读视图切换的阻止条件。
9. 视图切换不应阻塞 Textual event loop 的持续刷新；大历史装载必须分块/yield。
10. 关闭 TUI 必须释放所有观察任务/订阅；复用共享 Application 时不得终止后台 Session。若 TUI 是 Application 的
    composition root，则整体退出遵循既有 Runtime shutdown policy，而不是由会话视图逻辑隐式控制 child。
11. composer 上方的导航面板只消费 Agent tree DTO，不读取 store 或解析工具文本；无 child 时隐藏，有 child 时在
    root/child surface 上使用同一 widget，按当前/选中节点调整最多十个 child 的有界窗口。
12. 导航面板必须保持在当前最上层 inline 菜单（命令、审批、问答或计划确认）的上方，使菜单与 composer 相邻；
    运行、结束和失败节点分别使用运行语义色、常规低亮色和失败语义色，不复制颜色常量。
13. 终端协议只产生带 modifier 的按键事件，不产生独立 Alt key-up。`Alt+方向键` 必须立即更新节点选择，但 TUI
    同时只能执行一笔 visible Session 切换；切换期间的新目标覆盖唯一待处理目标，当前切换完成后只处理最新目标。
    不得排队每次重复按键，也不得用全局键盘轮询或超时猜测模拟 key-up。
14. 鼠标悬停会话节点只改变临时选择和详情展示，不触发 Session 观察；移出后恢复当前或键盘选择。左键点击 root
    或 child 节点都通过与键盘相同的单飞切换入口打开目标 Session；child 选中时展示 Agent 名称和任务描述。
15. 安装 Session surface 时先重建 snapshot 内的未完成响应，再接入同一有界事件队列，继续使用原有按帧批量渲染。
    切换后旧订阅不得继续向新 surface 投递事件；安装被取消时必须关闭尚未接管的订阅。
16. Run 结束标签由公共终态事件渲染链路生成，执行端的 `RunResult` 回调不单独追加标签。Host 通过 Application
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
- snapshot high-water + `after_sequence` 无缝交接测试；
- bounded lag 后 durable replay、去重和顺序测试；
- 历史/实时 SpawnAgent 工具点击测试；
- child 只读且 root 控制目标不变测试；
- root 与至少两个 child 交错流、多次切换、切换窗口新事件测试；
- root tree 全层级节点、跨 Run 分组、十节点窗口及 root/child/child 方向键直切测试；
- 导航加载单飞、加载期间最新目标覆盖，以及节点悬停预览和点击切换测试；
- 导航面板与命令/审批 inline panel 的层叠几何、运行/结束/失败状态色测试；
- active Run 期间切换测试；
- 执行端、独立观察端和重载历史的结束标签一致性，覆盖完成、失败、中断、双存储后端和连续 Run 排序：
  `tests/tui/test_run_summaries.py`。
- TUI 退出资源释放且 child 继续运行测试；
- Local 与 SQLite 后端相关契约测试，以及全局质量门。
- 独立 Runtime 持续写入、snapshot 读取期间新增事件、本地/外部事件交错、持久化序号缺口、共享有界跟踪与
  退出后停止 I/O、读取故障诊断：`tests/application/test_session_event_following.py`。
- 另一 Runtime 自动运行任务，TUI 在工具执行时进入、完成前继续渲染文本、切换离开与返回后补齐并继续流式：
  `tests/tui/test_live_session_attachment.py`；取消安装释放订阅：`tests/tui/test_async_observation.py`。
- 双 Host 交替审批、竞争同一审批、旧 job 延迟结算、子会话初始化失败后同步唤醒父会话：
  `tests/application/test_agent_multi_host.py`；跨 Host 等待态锁交接、超时和取消：`tests/application/test_run_ownership.py`。
- 两个 TUI 同时订阅工具执行与连续 Run、结束标签去重，以及失败工具在实时与重开后的动画停止：
  `tests/tui/test_live_session_attachment.py`；未订阅缓存失效、精确 Run 查询和快照水位一致性：
  `tests/application/test_session_event_following.py`。
- 大型 Local event journal 下 active Run 定位和 `after_sequence` replay 的有界反序列化测试；派生索引缺失/损坏
  回退及 delta group-commit 不回归测试。
- 超过 8KB 的事件尾部、跨块 UTF-8、真实尾部损坏与独立进程逐次跟踪大事件：
  `tests/sessions/test_local_files.py`、`tests/sessions/test_event_tail_process.py`。

TUI 呈现、导航和只读交互见 [TUI 规格](tui.md)。

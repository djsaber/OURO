# OURO Host 会话观察与 TUI 子智能体视图设计

日期：2026-08-27
性质：架构设计说明；规范性约束见
[`dev/specs/session-observation.md`](../dev/specs/session-observation.md)。

## 1. 问题定义

主会话通过 `SpawnAgent` 派遣子智能体后，TUI 需要允许用户点击对应工具消息，打开该子智能体的会话视图，
连续观察其文本、推理、工具调用和 Run 状态；子视图只读，并可在主会话和多个子会话之间任意切换。切换期间，
后台会话继续运行，重新打开后不得缺消息、重复消息或破坏流式边界。

这是 Host 的观察与交互策略，不是 Agent Runtime 的执行语义。Desktop、Web 或 API Host 可以采用不同的导航、
交互和权限策略，因此 OURO 底层不能包含“点击工具行”“返回主会话”“禁用输入框”等 TUI 概念。

## 2. 第一性原理

### 2.1 会话是运行事实，视图是 Host 状态

每个子智能体已经拥有独立、持久的 Session 和 Run。切换视图不得切换、暂停或重启 Runtime，也不得修改
Session 的生命周期。Host 只改变当前观察目标。

因此必须分离：

- **控制会话**：接收用户输入、abort、配置变更和交互应答的会话；
- **可见会话**：当前被 Host 渲染的会话；
- **观察订阅**：按 Session 独立推进的事件游标；
- **视图策略**：Host 决定可见会话是否只读、如何导航和保留滚动位置。

TUI 中控制会话始终是主会话；打开子会话只改变可见会话。Desktop Host 可以在相同底层契约上允许更多
控制行为，而无需修改 OURO Core。

### 2.2 委派关系必须是类型化身份，不能从文本猜测

可点击的父工具消息必须通过以下稳定关系定位子会话：

```text
(parent_session_id, parent_run_id, parent_tool_call_id)
    -> DelegationRecord
    -> (delegation_id, child_session_id, current_child_run_id)
```

工具名称、展示文案、Agent nickname、消息顺序和 `result_data` 都不是可靠主键。历史消息可能不保留完整工具结果，
工具调用 id 也只应在所属 Run 的上下文内解释。因此 Application DTO 必须暴露父 Run/ToolCall 身份和 child Session
身份，Host 不得读取 delegation 文件或解析 Markdown。

### 2.3 事件类别不是事件来源

`RuntimeEventCategory` 是 Host 用来筛选渲染语义的粗粒度内容类别，不是 actor/source 维度：

- `agent_spawned`、`agent_result_ready`、`agent_result_delivered`、`agent_closed` 属于 `agent`，因为它们描述
  委派关系与 Agent 生命周期；
- child Session 内的输出仍分别属于 `text`、`reasoning`、`tool`、`interaction`、`run`、`compaction`
  或 `lifecycle`；
- Host 通过 `RuntimeEventView.session_id` 判断事件属于哪个会话，不能把所有子智能体事件都归入 `agent`。

这使 `agent` 成为关系发现信号，而不会破坏既有渲染器对内容类别的复用。

### 2.4 “不丢消息”是可恢复一致性，不是无限内存队列

实时 callback 或有界订阅可能因慢消费者发生 lag。正确性不能建立在无限队列上，而必须建立在：

1. EventRecord 先持久化，后 fan-out；
2. 每个 Session 使用独立、单调的 `sequence`；
3. snapshot/reload 返回已覆盖的高水位；
4. live 订阅从 `after_sequence` 继续；
5. lag/disconnect 可检测，Host 从最后确认游标重放；
6. 投影按 `(session_id, sequence)` 去重。

“连续渲染”不要求所有不可见会话一直占用 widget。它要求切回时从最后游标收敛到 canonical history，并在装载边界
之后无缝接续 live 事件。不同 Session 的 sequence 不存在全局顺序，禁止合并比较。

### 2.5 可恢复不等于反复全量扫描

`events.jsonl` 可以累积数万乃至数十万条事件。snapshot 即使只保留 active Run 事件，如果读取时仍从文件头过滤，
切换成本仍会随会话年龄单调恶化；snapshot 后的 `after_sequence` 补读也存在同样问题。因此 Local Files 后端需要
两类派生游标：每个 Run 的首个可能字节偏移，以及固定跨度的 sequence→byte 稀疏 checkpoint。观察 snapshot 从
active Run offset 读取，补读从不晚于目标 sequence 的最近 checkpoint 前向扫描。

这些游标只负责定位，不是第二份事实源。规范数据仍只有 `events.jsonl`；索引可删除、可重建，损坏时必须回退且
不能漏事件。新 Run 在首个事件前记录 offset，旧会话在首次目标读取时惰性建立。这样稳定切换成本取决于当前
transcript、active Run 和切换窗口内新增事件，而不取决于所有已结束 Run 的事件总数。active Run 本身可能很大，
其未完成状态仍必须读取；若未来需要进一步压缩该上界，应引入规范的 active projection checkpoint，而不是丢弃事件。
索引维护必须是 best-effort 且无 fsync：不可写索引只使查询退化到 canonical 扫描，不能使事件写入失败；offset 0
不落盘，因为缺失索引已经表达相同起点。

## 3. 实施前框架判断

OURO Core/Runtime 已具备主要事实基础：

- `SessionRecord` 区分 primary/subagent，并保存 root、parent、delegation backlink；
- `DelegationRecord` 保存父 Session、父 Run、父 ToolCall 和 child Session/Run；
- child 拥有独立持久消息与 EventRecord；
- `RuntimeSupervisor.subscribe(session_id, after_sequence=...)` 已按 Session 提供重放和 live 订阅；
- Agent tree 已从 durable relation 重建节点和状态。

实施前缺口位于 Application 投影和 TUI 状态模型，而非 Runtime 执行模型：

- Agent tree/handle 没有完整投影 `parent_run_id`、`parent_tool_call_id`；
- `agent_*` 事件目前落入 `unknown`，Host 缺少稳定关系类别；
- TUI 只有一个同时承担控制和显示职责的 `_session_id`/projection；
- TUI 打开 Session 会重置控制目标，并在 active Run 时禁止切换；
- 历史和实时工具行没有统一携带父 Run 身份，也没有导航消息；
- 当前 load 与全局 sink 之间没有清晰的 snapshot/live 交接屏障。

所以结论是：现有架构方向支持需求，Core 无需重写；但当前公开 Application 契约和 TUI 实现尚不能满足完整需求。

## 4. 目标分层

### 4.1 Core / persistence

保持既有 Session、Run、Delegation、Message、EventRecord 事实模型。这里不保存“当前 TUI 标签页”“是否只读”或
滚动位置。仅在发现关系字段缺失或一致性约束不足时增强 canonical 模型。

### 4.2 Application

Application 提供 Host-neutral 能力：

- 类型化投影父 ToolCall 到 Delegation/child Session 的关系；
- `agent` 关系事件类别与完整身份 payload；
- Session snapshot/history 和每 Session 的事件高水位；
- 从 `after_sequence` 建立 live 订阅；
- 各 persistence adapter 对 active Run/sequence 的高效定位；派生索引不进入 Host DTO；
- lag、关闭和恢复语义；
- Session kind/root/parent 等只读身份，使 Host 自行决定控制策略。

Application 不提供 TUI 页面、返回按钮、widget 或“子会话一律不可交互”的产品策略。

### 4.3 TUI

TUI 独立拥有：

- 控制主会话与可见会话的分离状态；
- 父 SpawnAgent 工具行的点击消息；
- 主/多个 child 的导航、标题和返回行为；
- child 可见时禁用 composer、abort、模型/模式等写操作；
- 每会话投影、游标及可选滚动位置缓存；
- snapshot/live 原子交接、lag 重连和 UI 生命周期清理。

## 5. 切换协议

打开目标 Session 时，TUI 遵循以下逻辑协议；具体实现可以缓存投影，也可以重建：

1. 从 Application relation DTO 解析目标 child，拒绝文本推断和不存在的关系；
2. 为目标 Session 建立/恢复独立观察状态；
3. 取得 canonical history/snapshot 及其事件高水位 `N`；
4. 从 `after_sequence=N` 接续 live 事件；若订阅先建立，则缓存并在 snapshot 后按 sequence 合并；
5. 在一次 UI 状态提交中替换可见 transcript 和 projection；
6. 对缓存事件按 sequence 应用，忽略 `<= N` 的重复事件；
7. 后续事件只推进其所属 Session 投影；不可见投影可后台推进或在再次可见时重放；
8. 返回主会话只改变可见目标，不影响 child 或 root 的执行和订阅生命周期。

任何 lag/overflow 必须进入明确恢复路径，而不是静默继续。恢复完成前可显示 catching-up 状态，但不得把不完整状态
伪装成已同步。

## 6. TUI 交互结果

- SpawnAgent 对应的历史工具行和实时工具行均可打开已建立的 child Session；尚未建立关系时保持普通工具行。
- child 顶部显示 Agent/会话身份和只读标识，并提供返回主会话及切换其他已知 child 的入口。
- composer 上方以右对齐导航条投影整个 root Agent tree；root 与 child 节点以 `|` 分隔，child 按父 Run 分组，
  最多显示十个并围绕当前节点滑动。
- 当前或键盘选中节点使用 `▣`，其他节点使用 `■`；运行、结束和失败分别复用运行语义色、常规低亮色和失败语义色。
- 鼠标悬停 root 或 child 节点时临时使用选中标记，child 同时显示对应 Agent 名称和任务描述；移出后恢复当前或
  键盘选择。点击任一会话节点都通过与键盘相同的切换入口打开该 Session。悬停不重排十节点窗口，也不触发
  Session 观察。
- child composer 的只读快捷提示只高亮按键组合；底部状态栏不追加 `Subagent` 标签，继续显示 child 自身的实时
  activity、审批模式、模型和推理状态。
- 主会话 composer 中带有键盘操作的占位提示复用同一结构化片段渲染；例如运行期的 `Enter` 和
  `Ctrl+Enter` 与子会话快捷键使用相同主题高亮样式。
- 打开首个 child surface 时，TUI 暂存并清空 controlled root 的 composer 草稿；child/child 切换不覆盖该草稿，
  回到 root surface 时原样恢复。child composer 因此始终为空并可稳定显示只读快捷提示。
- `Alt+←/→` 选择相邻 child，`Alt+↑` 选择 root，`Alt+↓` 选择最新 child。选择反馈立即更新；昂贵的 visible
  Session 切换同时只执行一笔，加载期间后续选择只覆盖唯一待处理目标，当前切换完成后仅打开最新目标。终端没有
  modifier key-up 事件，因此不使用键盘轮询、超时推断或逐键加载队列。
- child 可见时，键盘提交、粘贴提交、abort、审批/AskUser 应答及其他会写入 Session 的动作均不可发往 child。
- 主会话继续接收其 own stream 和 Agent tree/HITL 更新；切回时立即显示最新一致状态。
- 多 child 的事件、工具 id、sequence、滚动状态互不污染。

## 7. 非目标

- 不把 child 中间输出自动复制进主 transcript；
- 不引入跨 Session 的全局 sequence；
- 不让 TUI 直接访问 SessionStore/DelegationRepository；
- 不把只读策略写进 Core；
- 不通过无限缓存承诺可靠性；
- 不改变现有子智能体执行深度限制；导航只投影 root Agent tree 中已经存在的节点。

## 8. 验收依据

实现必须由 Application DTO/事件投影测试、订阅重放和 lag 测试、TUI 点击与只读测试、多会话交错流测试、
切换竞态测试以及完整回归质量门共同证明。执行拆分见
[`dev/plans/tui-subagent-session-views.md`](../dev/plans/tui-subagent-session-views.md)。

## 9. 实施结果

当前实现按上述分层完成：Application 投影完整 parent/root/ToolCall relation 和 `agent` 事件类别，Session service
提供带 `event_high_water` 的原子观察 snapshot，Runtime sink 可按 Host 注册独立解绑；Local/SQLite adapter 分别以
派生 byte index/数据库索引完成 active Run 与 sequence 定位。TUI 保留 controlled root，以独立 visible projection
观察 child，历史和实时 SpawnAgent 工具行通过同一 typed navigation identity 打开会话，child surface 全面只读。

root 与两个并发 child 的集成测试覆盖 snapshot 前缀、live 后缀、不可见期间完成、返回 root、重新打开 child、输入与
abort 防线；最终全量回归结果为 `1874 passed, 2 skipped`。规范已转为
[`状态：当前`](../dev/specs/session-observation.md)，最终执行证据记录于
[`dev/plans/tui-subagent-session-views.md`](../dev/plans/tui-subagent-session-views.md)。

后续导航面板改良继续复用同一 root Agent tree 和 controlled/visible Session 分离模型：面板覆盖 root tree 全部层级，
按父 Run 分组并提供十节点窗口，主/子 surface 共用同一 widget；child/child 直切仍只替换 visible observation，
不改变 controlled root 或任何 Runtime 生命周期。

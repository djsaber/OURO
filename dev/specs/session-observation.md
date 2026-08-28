# Host 会话观察与子智能体导航规格

状态：当前

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
2. 切换可见会话不得改变 Session/Run/Delegation 生命周期，不得隐式 abort、resume、spawn 或提交输入。
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

Local 加速索引不是 canonical 数据：缺失、尾部不完整或损坏不能隐藏 EventRecord 或使 Session 不可读，必须从
`events.jsonl` 安全回退并可惰性重建。旧会话允许首次目标读取执行一次历史扫描来建立 Run offset，之后必须进入
稳定快速路径。派生索引写入是 best-effort、无 fsync 的加速行为，失败不得阻断 canonical event；隐式 0 游标不得
制造无定位收益的磁盘写入。delta 分组耐久性不能因为写入派生索引而退化成每 token fsync。

订阅通知必须包含 Session identity、event sequence 和可区分的 event/lag/closed 状态。发生 lag 时，通知必须给出恢复
所需的最后安全游标或明确要求 Host 重新加载 snapshot。Host 完成恢复后方可恢复 synced 状态。

## TUI adapter 行为

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
13. 终端协议只产生带 modifier 的按键事件，不产生独立 Alt key-up。TUI 将每个 `Alt+方向键` 作为一笔导航事务：
    进入节点选择状态、更新选中节点并切换 visible Session，目标 surface 提交后恢复当前节点状态；不得用全局键盘
    轮询或超时猜测模拟 key-up。

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
- 导航面板与命令/审批 inline panel 的层叠几何、运行/结束/失败状态色测试；
- active Run 期间切换测试；
- TUI 退出资源释放且 child 继续运行测试；
- Local 与 SQLite 后端相关契约测试，以及全局质量门。
- 大型 Local event journal 下 active Run 定位和 `after_sequence` replay 的有界反序列化测试；派生索引缺失/损坏
  回退及 delta group-commit 不回归测试。

实现计划见 [`../plans/tui-subagent-session-views.md`](../plans/tui-subagent-session-views.md)。

# TUI 子智能体会话视图实施计划

状态：已完成

目标：在不把 TUI 策略下沉到 Core 的前提下，补齐 OURO Host-neutral 会话观察契约，并实现 TUI 主会话/多个子智能体
会话之间的只读、无丢失、连续流式切换。

规范：[`../specs/session-observation.md`](../specs/session-observation.md)
设计：[`../../docs/OURO_Host会话观察与TUI子智能体视图设计_2026-08-27.md`](../../docs/OURO_Host会话观察与TUI子智能体视图设计_2026-08-27.md)

## 约束

- 遵守 [`../../docs/OURO_AGENT_编码准则_ZH.md`](../../docs/OURO_AGENT_编码准则_ZH.md)。
- 不修改 child 执行语义，不新增 TUI 专用 Core 状态。
- 不让 TUI 读取 store/delegation 文件或解析工具文本。
- 不用无限队列掩盖 lag；用 durable replay + cursor 证明正确性。
- 每个里程碑先增加失败/边界测试，再实现，再更新规格状态。

## M0：基线与契约冻结

- [x] 审计 Session/Run/Delegation/EventRecord、Supervisor subscription、Agent tree 和 TUI 单投影状态。
- [x] 记录控制会话/可见会话分离、稳定 ToolCall relation、per-session cursor 和 snapshot/live 交接不变量。
- [x] 建立设计、目标规格和实施计划。
- [x] 运行相关 Application/TUI 测试，记录实施前基线。

退出条件：目标契约可独立审查，未以 TUI 行为替代底层语义。

## M1：Application 关系与事件契约

- [x] 为 Agent tree/handle 投影 `parent_run_id`、`parent_tool_call_id` 和必要 root/parent identity。
- [x] 提供类型化、唯一的 parent ToolCall → child Session 解析入口，或由等价 Agent tree DTO 无歧义完成解析。
- [x] `RuntimeEventCategory` 增加 `agent`，分类全部现有关系事件。
- [x] 扩充 `agent_spawned` payload 和 schema 验证，保持恢复/重复 spawn 的 identity 一致。
- [x] 添加 DTO、event projection、幂等/恢复和负向 identity 测试。

退出条件：任意 Host 不访问持久化内部即可从父工具调用稳定定位 child。

## M2：Session 观察交接契约

- [x] 定义并投影 Session snapshot 的 `event_high_water`。
- [x] 证明 snapshot 与 `subscribe(after_sequence)` 的无缝边界；必要时增加窄 Application facade，而不是在 TUI 拼接 store。
- [x] 明确 subscription lag/closed 的 Host 恢复信息和资源释放语义。
- [x] 添加 active stream、边界并发事件、重复、out-of-order、lag/replay 测试。
- [x] Local Files 观察不得扫描 terminal Run 历史；增加 per-Run offset、稀疏 sequence checkpoint 及旧会话惰性
  重建测试，保持 delta group-commit。

退出条件：测试证明边界前后每个 canonical event 恰好进入最终投影一次；无需无限内存缓冲。

## M3：TUI 控制/可见状态拆分

- [x] 保持 `TuiController.session_id` 只代表 controlled root，新增不改变控制目标的 observe/read API。
- [x] 引入 visible Session 和 per-session observation/projection 状态。
- [x] 使 root/child 的 sequence、stream accumulator、tool activity 和 interaction 状态互相隔离。
- [x] active Run 不再阻止只读视图切换。
- [x] 添加控制目标不变、多 child 独立投影和生命周期清理测试。

退出条件：打开 child 不会把 submit/abort/config 路由到 child，也不影响任何 Run。

## M4：TUI 导航与只读交互

- [x] 为历史工具 entry 保留 parent `run_id`，为实时工具行绑定当前 parent Run。
- [x] 历史 RichLog 工具片段和实时 `ToolActivityRow` 发出统一的 typed navigation message。
- [x] 点击 SpawnAgent 工具展示时仅由已绑定的 Application relation 打开 child；未绑定时不猜测。
- [x] 实现主会话返回、多个 child 选择、当前 Agent/只读标识。
- [x] child 可见时禁用所有写入动作；为键盘、鼠标、粘贴、interaction/abort 等入口增加防线测试。

退出条件：用户可在 root/多个 child 间导航，child 只可观察，且历史与实时工具行行为一致。

## M5：连续渲染与恢复

- [x] 在 UI apply 锁内完成 snapshot/live 交接或等价原子提交，切换窗口事件进入正确 Session 投影。
- [x] 保留 ConversationHistory 分块装载/yield；避免大历史阻塞 event loop。
- [x] 实现 lag/catching-up/error 状态和显式重连，不静默显示不完整 transcript。
- [x] 验证切换中新增 text/reasoning/tool、Run 完成、child 结束以及快速往返切换。
- [x] 验证不可见投影缓存策略有界；淘汰后可由 canonical replay 完整恢复。

退出条件：root + 两个 child 的交错压力测试无遗漏、重复、串流或 UI 停顿性回归。

## M6：回归与规格转正

- [x] Application、TUI、SessionStore（Local/SQLite）相关定向测试通过。
- [x] 全量 `python -m pytest` 通过。
- [x] Ruff check、修改文件 Ruff format、mypy、compileall、`git diff --check` 通过。
- [x] 审计订阅/task/timer 在退出与失败路径全部释放。
- [x] 审计 Host event sink 可独立解绑，复用 Application 时 TUI 关闭不保留 callback 也不关闭 Runtime。
- [x] 审计 TUI 未导入 persistence/internal runtime 实现。
- [x] 将目标规格状态改为“当前”，更新规格/计划索引与相关子智能体/TUI 文档。

退出条件：所有强制不变量都有当前类型或测试证据；计划状态改为“已完成”。

## M7：根会话级子智能体导航面板

- [x] 复用 root Agent tree，展示整个主会话内全部子智能体，并按父 Run 分组。
- [x] 在 composer 上方增加单一导航 widget；无 child 时隐藏，并通过 compose 顺序自然适配命令、审批、问答和计划
  inline panel。
- [x] 实现 root 节点与最多十个 child 节点的有界滑窗；root 视图显示最新窗口，child 视图围绕当前节点重排。
- [x] 使用 TUI 运行/低亮/失败语义 token 渲染实时状态，并在选中 child 时同步任务描述、名称、所属 Run、派发方式
  和实时 activity。
- [x] 实现 `Alt+←/→/↑/↓` 的相邻 child、主会话和最新 child 导航，保持 controlled root 不变，并隔离模态页面。
- [x] 覆盖嵌套派发、Run 分组、窗口重排、节点颜色、菜单/审批几何、兄弟 child 直切和观察订阅取消竞态。

终端输入协议没有独立 modifier key-up 事件，因此每个 `Alt+方向键` 组合是一次完整导航事务；实现不通过轮询或
超时推断 Alt 松开，从而保持跨终端的一致性。

## 预期修改面

- Application：`ouro/application/event_models.py`、Agent DTO/projector/service、Session/Supervisor 观察 DTO。
- TUI：controller、app 状态模型、session projection、ConversationHistory、ToolActivityRow、导航/只读 widget。
- 测试：`tests/application`、`tests/tui`、必要的 shared SessionStore contract。
- 文档：本规格、子智能体规格交叉引用、TUI 实施文档和索引。

如 M2 证明现有 Supervisor 契约已经完整满足 high-water 交接，则只增加缺失 DTO/测试，不为形式一致另建 facade。

## 最终验证

- 全量：`1874 passed, 2 skipped`。
- 关键 Application/TUI/SessionStore 定向回归：`459 passed`；最终双后端索引专项 `45 passed`，工具批处理顺序
  回归连续三轮各 `19 passed`。
- Ruff lint、修改文件 format check、mypy（263 source files）、compileall 与 `git diff --check` 通过。
- repository-wide Ruff format audit 仍识别出 107 个本任务未修改文件的既有混合换行/格式基线；为遵守最小必要
  改良，本计划不制造跨 107 个无关文件的机械 diff。所有本计划修改的 Python 文件均通过 format check。

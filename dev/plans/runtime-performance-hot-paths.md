# 运行全生命周期热路径性能改进计划

状态：进行中（第一、第二里程碑已完成；第三里程碑核心修复已实施，真实终端响应性待验收）

关联规格：[`../specs/performance-hot-paths.md`](../specs/performance-hot-paths.md)

阅读顺序：先读关联规格确认当前强制契约，再读本计划的实施门禁、任务与证据；审查报告只作为分析输入，不能覆盖
规格或把未经动态证据支持的候选重构升级为实施项。

分析依据：

- [`../../docs/OURO_全生命周期性能深度审查与既有报告复核_2026-08-30_ZH.md`](../../docs/OURO_全生命周期性能深度审查与既有报告复核_2026-08-30_ZH.md)

## 目标

本计划处理性能审查中已经由代码、剖析或可重复基准确认的热路径根因。第一里程碑收敛传输碎片、压缩算法、
Observer、草稿和工具结果提交粒度；第二里程碑治理 TUI 与 Runtime 共享事件循环时的同步持久化、取消探测和
结构渲染尖峰；第三里程碑只修复工具进入/退出边界仍可达的 loop 阻塞 I/O、已经确认的 App dispatch 漏洞，
并以动态证据决定是否处理投影或结构提交中的主导不可让出片段。

改进必须减少系统实际完成的工作量，同时保持事件顺序、消息原子性、取消、恢复、插件快照和 Host 可见性契约。

## 第一性原理约束

- Provider chunk 是传输分片，不是业务事实边界；普通文本的持久化和展示频率应有上限，结构事件仍是立即边界。
- 压缩容量判断只需要对每个候选消息计算一次增量度量；不得反复重建同一前缀。
- 没有消费者时，观察投影没有语义价值；持久事件和 Host sink 仍必须正常工作。
- 草稿的 canonical 值是内存中的最后修订，磁盘只需要在短暂静默或关闭边界保存该值。
- 一个工具批次的结果天然是有序业务批次，应使用已经存在的 `append_messages` 原子边界。
- 事件循环只做编排；一个完整同步 mutation 在同一 worker 上执行，不以全局单线程串行不同 Session。
- 取消检查频率由 Provider/工具循环决定，因而必须是内存 O(1)；跨进程 durable 状态由低频后台桥接。
- TUI 每帧工作必须有上界；定宽动画属于 paint，工具进入/退出才属于结构边界。
- worker 线程不是响应性边界；持 GIL 工作是否需要再拆分，必须由现实 payload 下的不可让出时长证明，不能只用
  thread id 或理论对象大小推导。
- 理论增长阶用于选择测量点，不单独授权数据布局重构；只有当前支持配置下的工具边界现实量级超预算，才进入实施。
- TUI 先保持现有每帧事件条数上限；只有多事件累计被证明主导时才增加时间 deadline。一个 App handler 内与当前
  prefix 无关的 Store await 不应继续占有 pump。

## 已完成实施范围（第一、第二里程碑）

### A. Provider 流式降频

- 保留 `RuntimeAdapter` 的单 reader、固定容量 FIFO 和单消费者所有权。
- 每种普通 delta 的首个可见分片不等待；后续快速到达的同类连续分片按上次发射时间形成短窗口。
- 窗口截止时间不随新分片延长；慢于窗口到达的分片立即交付，避免对慢流逐片叠加等待。
- continuation、tool、retry、done、error、usage/completion 和类型切换都是立即边界；单批仍受字符上限约束。

### B. 压缩选块线性化

- 每个 frame 至多执行一次模型可见序列化。
- 摘要 prompt、已有摘要包络只计算一次；以可加的 ASCII/非 ASCII 字符度量保持现有 token estimator 的精确结果。
- 保留单 frame 超预算、已有摘要超预算和 frame 原子性错误语义。

### C. 无 Observer 热路径短路

- `ObserverRegistry` 提供无需构造 `Observation` 的轻量匹配查询。
- 已知没有匹配 runtime-event Observer 时，不做 payload 脱敏/冻结、不 fork ExtensionCatalog lease、不进入 dispatcher。
- Host event sink 与 canonical event 持久化完全不受该短路影响；无法证明没有消费者时保守执行原路径。

### D. 输入草稿尾沿防抖

- 连续编辑只在最后修订静默 300 ms 后保存一次。
- 保存期间又有修订时继续保存最新值；TUI 关闭必须等待最终修订落盘。
- 继续使用现有原子草稿 Store，不引入第二份 durable 状态。

### E. 工具结果批量提交

- 同一完成工具批次先校验所有资源引用，再一次 `append_messages`。
- 保持 source order、sequence、幂等查重、interrupted reconciliation 和结果 Observer 的提交后触发语义。
- 单结果恢复路径继续可调用单条接口，但内部复用同一批构造/提交规则。

### F. Runtime 完整阻塞操作边界

- 新增受取消所有权保护的 `run_blocking()`：一个同步操作完整在线程中 settle，caller cancellation 不遗弃持锁事务。
- 将模型轮 begin/commit、上下文投影/快照、usage、工具检查点、Run start/complete/fail/pause 和关键 Session
  mutation 整体移出事件循环。
- 使用默认 worker pool 保留跨 Session 并发；Session mutation、文件锁和 SQLite 事务继续决定正确顺序，不新增全局
  I/O 串行器。
- 文件与目录发布复用同一 Windows sharing-race 有界重试，解决并发扫描暴露的 `WinError 5`，不降低原子性。

### G. 取消与 Supervisor 调度快路径

- `AbortSignal.is_aborted()` 只读内存；signal 激活先读取一次 durable abort，后续由 50 ms 后台 poller 在线程中桥接；
  model-round mutation 从 latest Run 派生，不能覆盖并发 `abort_requested`。
- Run 结束取消并等待 poller；abort settlement 和 Supervisor 首次 activity 快照读取离开事件循环。
- Supervisor 显式保持 FIFO 的 provider-stream 启动次序，不再依赖同步 I/O 偶然串行；shutdown 先标记 queued job，
  再等待 active abort，避免尚未产生 durable Run 的启动竞态。

### H. TUI 帧与工具结构边界

- 状态栏、工具 spinner 和 reasoning 计时采用 paint-only update；实际文字宽度变化才触发布局。
- 工具进入使用 `mount_all`，终态使用批量历史 append/离线渲染和 `remove_children`；assistant Markdown 冻结复用
  后台渲染路径。
- pending Runtime events 每个 compositor frame 最多应用 64 条，积压留到下一原生帧；既有 1000 buffer 上限和
  lagged durable reload 不变。

## 第一、第二里程碑测试先行任务

- [x] Adapter：首分片即时、快速后续合并、慢分片不额外等待、结构边界、字符上限、abort/异常关闭。
- [x] Compaction：与旧 estimator 的选块结果一致；序列化调用数随 frame 数线性增长；预算边界错误不变。
- [x] Observer：空 registry 和不匹配过滤器不构造 runtime observation；匹配、快照、sink 行为不变。
- [x] Draft：突发修订只保存最终值；关闭等待最后修订；失败只通知一次并允许后续修订重试。
- [x] Tool batch：多结果只产生一个消息批 append；顺序、资源校验失败原子性和部分恢复幂等性不变。
- [x] Blocking boundary：人工延迟 context/tool/terminal 保存时 ticker 持续推进，操作线程 ID 不等于 loop 线程。
- [x] Cancellation：高频 signal 检查不读 Store；durable poll、settlement、释放和本地 abort 状态均有测试。
- [x] Supervisor：多 worker FIFO 启动、global concurrency、queued/active shutdown abort 均保持既有语义。
- [x] TUI：paint-only 更新、批量 mount/remove、后台冻结渲染、每帧事件前缀上限均有回归测试。

## 第一、第二里程碑验收与退出条件

- 各项实现均有正常、边界和错误路径测试。
- 800-frame 压缩选块不再呈现二次增长，模型可见序列化调用次数不超过输入消息数。
- 快速 1-character delta 压测的 canonical delta 数量显著低于 provider chunk 数；首分片和结构边界延迟不回退。
- 多工具结果的 durable message append 次数由结果数次降为一次。
- 连续草稿编辑的物理保存次数由修订数次降为一次，关闭后可重载最终值。
- 通过相关测试、全量 `pytest`、Ruff、Mypy、compileall 和 `git diff --check`。
- 权威规格更新为当前状态，计划记录最终验证证据。

## 第一里程碑完成证据

- 100/200/400/800 frame 的 `_next_chunk` 中位耗时分别为 1.534/2.843/5.375/11.298 ms；800 frame
  的模型可见序列化调用保持线性上界。
- 1000 个连续 1-character Provider chunk 在 adapter 基准中投影为 3 个事件；完整 Runtime 文件/SQLite
  后端分别只写入 2/4 个 `model_text_delta`，拼接文本均逐字节等于原始 1000 字符。
- 无匹配 Observer 的 10,000 次 publish-only fan-out 耗时 4.529 ms，且不构造脱敏观察投影或 Catalog lease。
- 多工具结果测试确认只执行一次消息 batch append；资源引用在任何消息写入前统一验证，部分恢复只补缺失消息。
- TUI 突发草稿测试确认静默窗口只保存最终修订，关闭测试确认 pending 修订完成后才释放 Host。
- 完整测试 collection 以互斥分区运行：`tests/tui/test_app.py` 原有 215 项全通过，新增错误路径再单独通过；
  其余测试为 1745 passed、2 skipped；总计 1961 passed、2 skipped、0 failed。
- `ruff check ouro tests`、`mypy ouro`、`compileall -q ouro`、`git diff --check` 通过。仓库当前
  `ruff format --check ouro tests` 在本轮开始即会要求格式化 131 个既有文件；本里程碑未扩大为全库机械格式化。

## 第二里程碑完成证据

- 同一 model→tool→model 场景在含 `RuntimeSupervisor` 的 10 ms event-loop ticker 基准中，files 最大 loop lag
  从 93.2 ms 降至 15.6 ms，SQLite 从 69.1 ms 降至 16.3 ms；分别下降约 83% 和 76%。wall time 受本机磁盘和
  测试噪声影响，不作为吞吐结论。
- 60 ms 人工延迟 `save_latest_context` / tool checkpoint / terminal `save_run` 时，三个边界都在非 loop 线程执行，
  每段阻塞期间 1 ms ticker 至少推进两次。
- 热路径探针确认 `AbortSignal` 不再触发 loop-thread `load_run`；durable abort 的后台轮询间隔上界为 50 ms。
- TUI 400 条历史、4 工具的诊断基线曾在工具首次进入/终态观察到约 61/79 ms 结构处理和 15 ms 级 loop lag；本轮
  将 Markdown/批量工具 immutable 渲染移出 loop、批量化结构操作并限制每帧最多 64 条事件。该数字只用于定位，
  不将 headless Textual wall time 冒充终端 SLA。
- 保留恢复检查点、事件先持久化后发布、Session 锁、跨进程 abort 和 FIFO 调度语义；未引入全局 executor、
  write-behind、通用缓存或 TUI renderer 重写。
- 当前全量 collection 为 1972 passed、2 skipped，另有 2 个 TUI 菜单键盘时序用例在 14 分钟全量运行中超时，
  两项隔离复跑均通过；最终代码上的受影响边界集合另有 application 136、core 15、TUI/history 23 项全通过。
- `ruff check ouro tests`、`mypy ouro`（265 个源文件）、`compileall -q ouro tests` 与 `git diff --check` 通过。

## 第三里程碑：工具边界最小响应性修复

### 目标、范围与实施门禁

第三里程碑只治理工具首次进入、工具完成/下一轮和 Run 终态时，能够直接造成事件循环不可运行或 App pump 键盘
队头阻塞的工作。理论增长阶、冷路径同步调用数量和未来极端规模不自动构成本里程碑范围。

已确认在 event loop 上直接执行 Store、文件锁或 SQLite I/O 的路径违反 loop 所有权，并且等待时长受外部状态影响，
即使短样本偶尔未超预算也进入 M3-B。除此之外，一个候选改动只有同时满足以下条件才允许实施：

1. **因果同相位**：在重复复现场景中与 Key 延迟、spinner 漏帧或 event-loop lag 同时出现；
2. **现实量级超预算**：使用当前支持配置和真实量级时，单段工作稳定越过响应性预算，而不只是合成超大对象可变慢；
3. **最小充分**：没有只移动一个现有调用边界、消除一次重复工作或复用现有 consumer 就能解决问题的更小方案；
4. **净工作量下降**：不新增第二事实源、通用缓存、长期后台服务或一套只为隐藏等待而存在的异步状态机。

未通过门禁的项目不进入代码。本里程碑达到响应性验收后立即停止，不顺带治理其他扩展性问题。

### M3-A. 最小边界探针与基线

- 使用当前真实配置建立一个可重复的 model→tool→model 场景，同时覆盖工具首次进入、完成、下一轮和 Run 终态；
  files/SQLite 分别记录，但不把 10,000 Session、全量历史或最大工具输出组合成默认实施前提。
- 只在测试/诊断钩子中记录 event-loop lag、`RuntimeEventsReady` handler wall time、最长 loop 同步片段、
  `finish_assistant`/mount/remove、controller await，以及同相位 worker operation 的阶段、时长和输入/输出字节数。
- 探针不记录 prompt、reasoning、工具参数或结果内容；关闭探针时不复制 payload、不执行第二次序列化，也不建立新的
  生产遥测框架。
- 受控基准的目标预算为：1 ms ticker 的 event-loop lag p99 不超过 16 ms、最大值不超过 32 ms；Key 注入到 Composer
  状态变化的 p95 不超过 50 ms；标称 120 ms 的状态动画不得连续错过两个周期。预算用于判定是否继续修复，不用于
  预先证明某个数据布局改造必要。

### M3-B. 工具边界 loop 同步段消除与投影成本验证

以下执行边界修复为必做，但只覆盖能从工具边界或 Run 终态到达的路径：

- 将 `shared_budget_reason` 中直接执行的同步 port 调用移入现有 `run_blocking()` 边界；
- 审计 `_finalize_requested_abort`、`_finalize_run_boundary`、continuation/terminal settlement 和相关 delegation 查询，
  其中实际执行 Store、文件锁或 SQLite 访问的完整 query/mutation 必须在同一 worker settle；
- `session_controls.apply` 只有在控制工具实际到达的 mutation 分支中执行同样迁移，不为未命中的冷分支扩展范围；
- caller cancellation、Session 锁、SQLite transaction 和“durable 后发布 event”语义保持不变。

投影部分只做分项验证，不追求“清零”：

- 分别测量 `project_runtime_event`、`reduce_runtime_activity`、`runtime_activity_changed`、redaction 和 `freeze_json`；
- 只有现实 payload 下单段稳定超过 8 ms，或与超预算样本持续同相位，才允许实施最小短路；优先以显式 changed bit/
  小字段比较替换重复 dump，或在已知无匹配 Observer 时跳过 freeze；
- 活动中的 TUI Session 本来存在订阅者，“无订阅者不构造 event view”不能作为本症状的默认修复；
- 改变 `RuntimeActivitySnapshot` 的字段形状、裁剪 Host payload 或引入第二种摘要 DTO，必须另行更新规格并通过 Host
  兼容审查，不能在本项中顺带实施。

### M3-C. 已确认并修复的 TUI dispatch 漏洞

- `_apply_run_result()` 不得直接无界应用完整 `RunResult.events`；live subscription 与 terminal result 复用现有按
  sequence 去重、每帧最多 64 条的 consumer，终态不重新应用已经消费的事件前缀。
- 一个 event prefix 只登记一次 Session controls refresh。`session_control_statuses()` 已经在线程中读取，但该 await
  不得继续占有 `RuntimeEventsReady` App handler；使用一个合并的后续任务更新状态，并在提交前校验当前 Session。
- 保留现有批量 mount、批量 immutable history append 和批量 remove。M3-A 未证明单个结构 promotion 超预算前，
  不新增 pump 外 prepare/commit 协议、render generation 状态机或第二个 renderer。
- 只有 M3-A 证明卡顿由一个 64-event 前缀中的多事件累计造成，而不是单个不可让出单元造成，才在现有 64 条上限旁
  增加简单单调时钟 deadline；时间预算不能被描述成对单个同步函数的抢占机制。

### M3-D. 证据触发的单点修复与停止规则

M3-B/M3-C 已完成；若 M3-A 的最终真实终端验收仍超预算，只修复届时捕获的主导片段：

- provider completion 的完整 context 序列化若在现实 Session 中稳定主导 GIL stall，先为该单点提交独立设计、迁移和
  崩溃测试方案，再决定是否引入 completion sidecar；
- Run/tool checkpoint 只有在现实工具结果下的重复编码被证明主导时才允许拆分，不能因理论上 O(batch) 直接增加
  revision 合并和第二事实源；
- reasoning 冻结只在展开 reasoning 的复现场景超预算时统一后台化，默认折叠路径不为此增加工作；
- 若局部 render/controller 解耦后 App pump 仍主导，才另行评审 pump 外 prepare/commit 协议；
- files catalog 的 10,000 Session 写放大属于独立扩展性问题，不纳入本次交互卡顿里程碑。

任何触发项在编码前必须先把已获得的动态证据、最小设计和新增恢复状态更新回本计划与目标规格。满足响应性预算后，
其余触发项立即停止，不以“顺手优化”为理由继续实施。

### 第三里程碑已实施范围

- `shared_budget_reason` 改为 async application boundary；Agent 聚合预算 query 作为一个完整 `run_blocking()` 操作
  settle，Inner Loop 不再直接调用同步 port。
- 工具审批、用户回答、child wait 恢复、abort/final boundary、continuation settlement、terminal Agent settlement
  及其结果交付中的同步 Store query/mutation 已按完整操作移入 worker。Session control 的 acquire/release/replace
  保留“durable prepare → async controller → durable commit/rollback”既有三相语义，只把两个持久化相位移入 worker。
- `RunResult.events` 先按当前 projection 和 pending high-water 去重，再进入既有 64-event-per-frame consumer；终态不再
  旁路帧上限，也不重放 live subscription 已消费的前缀。
- Session controls refresh 从 `RuntimeEventsReady` handler 的 await 链中移出。同一前缀只置 dirty flag，单一任务合并
  连续请求；查询完成后校验当前 Session 和只读视图，过时结果不提交。
- 未新增 executor、缓存、sidecar、checkpoint revision、投影 DTO、时间 deadline、pump prepare/commit 或第二 renderer。
  手动分项基准没有触发投影改造门禁，因此该路径保持现状。

### 第三里程碑测试先行任务

- [ ] Probe：两个边界的 loop/App handler/worker 相位可归因；探针不含用户内容，关闭时不复制 payload。
- [x] Loop ownership：目标路径中的同步 Store/锁操作不在线程循环执行；取消继续等待所属 worker settle。
- [x] Projection gate：各投影阶段独立计时；未越过门禁时不改变 DTO、Host payload 或 Observer 语义。
- [x] Terminal consumer：`RunResult.events` 不旁路 64 条上限，不重复应用 live subscription 已消费的事件。
- [x] Controls refresh：同一 prefix 最多调度一次，App handler 不等待 Store 查询，过时 Session 结果不提交。
- [x] TUI semantics：assistant→tool 顺序、批量 mount/remove、folded/expanded reasoning 和 session switch 行为不变。
- [ ] Interaction：工具进入、完成、下一轮和终态满足 loop lag、Key latency 与 spinner cadence 预算。

### 第三里程碑当前验证证据

- files/SQLite 的 Runtime model→tool→model 测试在 context snapshot、tool checkpoint 和 terminal Run save 各注入
  60 ms 同步延迟；三个操作均在非 loop 线程执行，阻塞期间 1 ms ticker 均持续推进。shared Agent budget、requested
  abort、continuation terminal query、Session control persistence phase 和 Agent terminal settlement 另有线程所有权测试。
- TUI 测试确认 terminal `RunResult.events` 的每次 applied prefix 不超过配置上限，并跳过 projection/live pending 已覆盖
  sequence；controls 查询可保持未完成而 `RuntimeEventsReady` handler 已返回，多个请求合并且 Session 切换后的旧结果
  被丢弃。
- 本机 2,000 次分项基准中，1 KiB `tool_call_completed` payload 的 project/reduce/change/redact+freeze p99 分别约
  0.081/0.030/0.027/0.103 ms。100 KiB payload 的 project 与 redact+freeze p50 约 4.41/4.45 ms、p95
  约 5.73/5.80 ms；p99 约 8.56/8.37 ms，但没有稳定越过 8 ms，也没有与当前卡顿样本持续同相位。因此按门禁
  不实施 projection、DTO 或 Observer payload 改造。
- 受影响 application/core 集合 102 项、`tests/application/test_runtime.py` 69 项、conversation history 19 项通过；
  TUI 全文件在排除一个 HEAD 同样失败的 2 秒 child-start 时序用例后为 229 passed、1 failed、1 deselected，唯一失败
  是启动状态仍为 `Loading session`，隔离复跑通过。结果投递 exact-once/race/deadlock 相关 6 项在最终锁范围修正后通过。
- 项目 `.venv` 下 `ruff check ouro tests`、`mypy ouro`（265 个源文件）、`compileall -q ouro tests` 与
  `git diff --check` 通过。全量 `pytest` 和真实终端 Key/spinner 分布仍属于第三里程碑最终验收，不在当前证据中宣称。

### 第三里程碑退出条件

- 基线与最终分布均有记录，每个原始超预算样本至少能归因到 loop sync、worker GIL、App pump await、layout/paint 或
  terminal flush 中一类；不得用总 wall time 或 thread id 单独代替归因。
- 工具边界可达的同步 Store/锁访问均位于完整 worker operation 内；`RunResult.events` 和 controls refresh 两个已确认的
  App dispatch 漏洞关闭。
- 真实复现场景满足 M3-A 响应性预算；失败不得通过扩大 delta 窗口、降低动画频率、减少 checkpoint 或隐藏状态掩盖。
- 未通过门禁的数据布局、DTO 和 renderer 改造没有进入实现；通过门禁的新增范围已先更新计划/规格并具有恢复测试。
- 事件顺序、恢复、Observer、Host subscription、跨进程 abort、Session catalog 可见性和 TUI presentation 顺序不变；
  通过受影响测试、全量 `pytest`、Ruff、Mypy、compileall、文档链接检查和 `git diff --check`。
- 完成后移除规格的混合状态，把剩余真实终端响应性验收增量转为当前证据，并在本计划追加最终分布。

## 第三里程碑非目标

- 不默认引入 Context completion sidecar、Run/tool checkpoint 拆分、files catalog 新布局或 SQLite schema 迁移。
- 不把整个 Runtime 搬到专用线程，不引入全局 executor、通用缓存、生产遥测框架或新的后台服务。
- 不默认建立 pump 外 prepare/commit 状态机，不重写稳定 history/live widget 双表示，也不引入第二个 renderer。
- 不为“同步调用数量归零”迁移工具边界不可达的冷路径，不为“投影清零”改变 Host DTO 或复制一份摘要事实。
- 不处理历史 tail 分页、会话冷打开、Provider 请求二次 materialization、静态 extension generation 或 10,000 Session
  catalog 扩展性；这些问题需要各自的现实证据和独立里程碑。
- 不降低 Event/Message/Run/Context 的耐久级别，不引入正确性状态 write-behind 或跨边界事务。

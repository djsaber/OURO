# 运行全生命周期热路径性能改进计划

状态：已完成（两个里程碑）

关联规格：[`../specs/performance-hot-paths.md`](../specs/performance-hot-paths.md)

## 目标

本计划处理性能审查中已经由代码、剖析或可重复基准确认的热路径根因。第一里程碑收敛传输碎片、压缩算法、
Observer、草稿和工具结果提交粒度；第二里程碑治理 TUI 与 Runtime 共享事件循环时的同步持久化、取消探测和
结构渲染尖峰。

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

## 实施范围

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

## 非目标

- 不降低 event/message/Run 的现有耐久级别，不引入异步 write-behind 或跨边界事务。
- 不在本计划重做文件 catalog 表示、Session 快照布局或 SQLite schema。
- 不引入通用缓存框架、全局事件总线或新的后台服务。
- 不把稳定 history 与交互式 live widget 强行重写成单一 TUI surface；当前结构边界有成本，但尚无证据支持以可维护性
  换取全面 renderer 重构。
- 不为尚未由剖析证明占主导的历史 tail 分页、context completion sidecar 或 catalog 表示做推测性重构。

## 测试先行任务

- [x] Adapter：首分片即时、快速后续合并、慢分片不额外等待、结构边界、字符上限、abort/异常关闭。
- [x] Compaction：与旧 estimator 的选块结果一致；序列化调用数随 frame 数线性增长；预算边界错误不变。
- [x] Observer：空 registry 和不匹配过滤器不构造 runtime observation；匹配、快照、sink 行为不变。
- [x] Draft：突发修订只保存最终值；关闭等待最后修订；失败只通知一次并允许后续修订重试。
- [x] Tool batch：多结果只产生一个消息批 append；顺序、资源校验失败原子性和部分恢复幂等性不变。
- [x] Blocking boundary：人工延迟 context/tool/terminal 保存时 ticker 持续推进，操作线程 ID 不等于 loop 线程。
- [x] Cancellation：高频 signal 检查不读 Store；durable poll、settlement、释放和本地 abort 状态均有测试。
- [x] Supervisor：多 worker FIFO 启动、global concurrency、queued/active shutdown abort 均保持既有语义。
- [x] TUI：paint-only 更新、批量 mount/remove、后台冻结渲染、每帧事件前缀上限均有回归测试。

## 验收与退出条件

- 各项实现均有正常、边界和错误路径测试。
- 800-frame 压缩选块不再呈现二次增长，模型可见序列化调用次数不超过输入消息数。
- 快速 1-character delta 压测的 canonical delta 数量显著低于 provider chunk 数；首分片和结构边界延迟不回退。
- 多工具结果的 durable message append 次数由结果数次降为一次。
- 连续草稿编辑的物理保存次数由修订数次降为一次，关闭后可重载最终值。
- 通过相关测试、全量 `pytest`、Ruff、Mypy、compileall 和 `git diff --check`。
- 权威规格更新为当前状态，计划记录最终验证证据。

## 完成证据

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

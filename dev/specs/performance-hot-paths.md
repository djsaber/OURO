# 运行热路径性能规格

状态：当前

## 目标与原则

运行热路径按业务语义边界分配工作，而不是按传输碎片、UI 按键或可重复派生投影分配工作。异步事件循环只做
编排和轻量投影；可能等待文件锁、`fsync`、SQLite writer 或执行大对象物化的同步工作必须离开事件循环。性能改进
不能改变 canonical 顺序、原子提交、恢复、取消、插件 generation 隔离或 Host 观察语义。

## 流式事件

`RuntimeAdapter` 为每个 Provider stream 维护一个 reader 和一个固定容量 FIFO。reader 独占 Provider async
iterator，消费者独占 FIFO；FIFO 满是唯一无损背压点。

普通 `text_delta` 和 `reasoning_delta` 的 Provider chunk 边界不构成稳定业务语义：

- 每种普通 delta 在一个连续语义段中的首个分片不得等待未来分片；
- 后续分片以最近一次同类发射时间为锚点，在最多 8 ms 的固定窗口内合并；窗口不得因新分片到达而续期；
- 分片到达时窗口已经截止则立即发射，因此慢流不逐片承担额外等待；
- 每批最多 16 KiB，超出部分保持顺序留给下一批；
- continuation、tool、retry、done、error、usage/completion、delta 类型切换和 abort 都会立即结束当前合并边界；
- Provider 异常在先前事件后按序交付；取消或消费者关闭必须关闭 reader 和 Provider iterator。

该合并只降低事件频率，不改变拼接后的 text/reasoning 内容。

## 压缩容量规划

摘要选块必须保持 frame 原子性，并使用与 `estimate_text_tokens` 相同的精确保守估算。一次 `_next_chunk` 中：

- summarization prompt 和已有摘要包络只度量一次；
- 每个候选 frame 的模型可见文本最多序列化一次；
- 累计字符度量必须可加，选块工作量对已检查 frame/message 数为线性复杂度；
- 已有摘要已占满输入容量、单 frame 超预算时继续抛出原有 `ContextOverflowError`。

不得通过放宽 token 预算或拆开工具交换获得更低计算成本。

## Runtime Event Observer

canonical Event 持久化和 Host event sink 投递总是执行。Plugin Observer 是独立可选消费者：

- Registry 可用 event name、runtime event type 和 active controller 集合判断是否存在 enabled 匹配项；
- 已知不存在匹配项时，不构造脱敏/冻结的 `RuntimeEventObservation`，不获取额外 Catalog lease，也不进入 dispatcher；
- 当前 Catalog 或 Run snapshot 不可确定时，必须保守走完整观察路径；
- 存在匹配项时继续在事件发布时保留对应不可变 Registry/Catalog 视图。

## TUI 输入草稿

内存 `TuiInputDrafts` 始终包含最新修订。磁盘保存采用 300 ms 尾沿防抖：持续编辑只延后保存，静默后保存
最新完整快照。保存期间出现新修订时必须再次保存最新值。Host 关闭必须等待最终修订完成原子保存；保存失败沿用
现有 warning 语义，不能阻塞编辑或 Runtime。

## 工具结果消息

同一 `ToolCallBatchRecord` 的完成结果按 `source_index` 构造消息，并作为一个有序 `append_messages` 批次提交。
所有资源引用必须在任何新消息提交前验证。提交成功后才发布 `ToolCallFinishedObservation` 并推进批次状态。

幂等恢复时，已有 `(tool_result, tool_call_id)` 消息直接复用，只把缺失消息放入新批次；sequence 和
`result_message_id` 必须保持确定性。单结果接口与批接口共享同一构造和校验规则。

## Runtime 阻塞边界

同步 Store port 保持同步接口；application/core 的异步编排通过一个受所有权保护的 worker 调用边界执行它们：

- 一个 transaction/mutation 闭包从取得锁、读取、状态转换、写入到释放锁必须完整留在同一个 worker；不得把闭包内
  的 Store 调用逐个切换线程；
- model round begin/commit、上下文投影和快照保存、usage、工具批检查点、Run complete/fail/pause，以及 Run 启动所需
  的 durable mutation 不得在事件循环线程执行；
- 不使用全局单线程 executor。不同 Session 可以由默认 worker pool 并行，顺序与排他性仍由 Session/SQLite 的既有锁
  和事务定义；“同一 worker”只约束一次完整操作；
- caller cancellation 不能遗弃仍持锁或正在修改 durable state 的线程。worker 必须先 settle，随后再向 caller 传播
  cancellation；
- canonical event 仍在 durable boundary 成功后由事件循环按序发布。

Windows 的文件/目录原子发布共享同一个有界 transient sharing-race 重试；重试发生在 worker 上，不得冻结 TUI。

## 取消观察

Provider 和工具执行中的 `AbortSignal.is_aborted()` 是高频同步检查，只能读取进程内状态：

- 本进程 abort 在 durable transition 成功后立即发布到本地 signal；
- signal 激活时先执行一次 worker-side durable read，关闭 `run_started` 与 signal 建立之间的竞态；后续 Run mutation
  必须从 latest Run 派生，不能用旧快照覆盖 `abort_requested`；
- 跨进程 abort 由后台 poller 最多每 50 ms 读取一次 durable Run，再桥接到本地 signal；
- durable poll 和 settlement 检查在 worker 上运行，signal 检查不得读 Store；
- Run 结束必须取消并等待 poller，不能留下后台任务或未观察异常。

## TUI 流式消费与结构边界

- spinner、计时器和其他定宽内容动画只触发 paint；只有几何尺寸实际变化时才触发布局；
- 同一工具批的新行使用一次批量 mount，终态使用一次批量 history append 和一次批量 remove；复杂 Markdown/批量
  immutable transcript 渲染在 worker 上完成；
- Runtime event buffer 每个 compositor frame 最多应用 64 条；积压前缀处理完后在下一原生帧继续，buffer 上限和
  lagged 后 durable reload 语义保持不变；
- `batch_update()` 只用于合并可合并的刷新，不能被当作消除 `AwaitMount`、`AwaitRemove` 或结构布局成本的替代品。

## 验收证据

- `tests/core/test_adapter_stream.py`
- `tests/application/test_compaction.py`
- `tests/core/test_observers.py`
- `tests/application/test_events.py`
- `tests/tui/test_app.py`
- `tests/application/test_tool_call_batches.py`
- `tests/application/test_resources.py`
- `tests/core/test_callbacks.py`
- `tests/application/test_runtime.py`
- `tests/application/test_run_ownership.py`
- `tests/application/test_supervisor.py`
- `tests/tui/test_conversation_history.py`

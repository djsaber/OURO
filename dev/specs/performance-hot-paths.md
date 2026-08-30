# 运行热路径性能规格

状态：当前

## 目标与原则

运行热路径按业务语义边界分配工作，而不是按传输碎片、UI 按键或可重复派生投影分配工作。性能改进不能改变
canonical 顺序、原子提交、恢复、取消、插件 generation 隔离或 Host 观察语义。

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

## 验收证据

- `tests/core/test_adapter_stream.py`
- `tests/application/test_compaction.py`
- `tests/core/test_observers.py`
- `tests/application/test_events.py`
- `tests/tui/test_app.py`
- `tests/application/test_tool_call_batches.py`
- `tests/application/test_resources.py`

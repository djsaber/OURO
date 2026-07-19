# 核心运行时规格

状态：当前

## 目标与范围

核心运行时负责把一次用户输入转换为可恢复的 Run，并在模型响应、工具调用、人工交互和终止状态之间
维护一致的状态机。核心状态机不直接访问 CLI、文件系统或具体 Provider 实现。

## 领域对象

### Session

Session 是长期会话容器。关键字段包括：

- `session_id`：稳定身份；
- `current_model`：完整 `provider_id/endpoint_id` 路由；
- `status`：`idle`、`running`、`waiting_user`、`waiting_approval` 或 `failed`；
- `active_run_id`：唯一 active Run；
- `last_message_id`、`message_count`：canonical message 游标；
- `active_compaction_id`：当前上下文摘要投影；
- lineage、标题、归档、置顶、工作区与标签状态。

同一 Session 同时最多存在一个 active Run。

### Run

Run 表示一次可恢复执行。状态集合为：

`queued`、`running`、`waiting_user`、`waiting_approval`、`completed`、`aborted`、`failed`、
`paused`、`recoverable`。

运行阶段为：`model_streaming`、`tool_preflight`、`human_interaction`、`tool_execution`、
`result_commit`。Run 保存模型、usage、活动工具批次、待审批身份、终止请求和错误信息。

### Message 与 model turn

- 用户和普通 assistant 消息属于 canonical history。
- assistant 工具请求写入类型化 `tool_calls`。
- 工具结果写入类型化 `tool_result`。
- 可展示的模型思考写入类型化 `reasoning_content`。
- 协议私有的工具续接状态写入 `provider_continuation`，并绑定 provider、protocol 和 model 路由。
- 一次模型响应中的 assistant 工具请求及其结果共享 `turn_id`。
- `turn_id` 只能出现在拥有 `run_id` 的消息上。
- 工具身份与思考续接状态不得写入 `metadata`。

## 运行流程

1. Host 调用 `RuntimeService.run`。
2. 新会话被创建，或现有会话通过不含 replay events 的运行态快照执行恢复检查。
3. 取得 session run lock；锁竞争失败时返回 active Run 或明确的 `SessionBusyError`。
4. `RunLifecycle.start` 持久化 active Session/Run。
5. 用户消息持久化并产生事件。
6. `InnerAgentLoop` 复用已加载的 canonical messages 构建上下文并调用 request-local `RuntimeAdapter`；正常
   模型轮次不得重新加载完整 Session 事件历史。
7. 模型文本、reasoning 和工具调用增量到达时依次形成事件；`done` 后完整工具调用交给
   `ToolCallCoordinator`，最终消息则完成 Run。
8. waiting 状态通过 approve/reject/AskUser 恢复同一 Run。
9. terminal Run 释放 lock、abort signal 和每 Run Hook 隔离状态。
10. 在 model turn 边界消费 steer 输入；completed Run 先释放 session run lock，再串行启动 follow-up Run。

## Inner loop 边界

`InnerAgentLoop` 只拥有一个 active Run 内的模型回合循环。所有副作用通过 `InnerLoopPorts` 注入：

- 上下文准备；
- 事件写入；
- Hook 失败记录；
- 工具批次处理；
- usage 记录；
- waiting、complete、fail、pause 状态转换；
- abort 结果构造。

应用层不得在 core 中引入 SessionStore、CLI 或 Provider factory。

## Provider 流契约

- 流式 Provider 必须把每个工具调用 chunk 规范化为 `tool_call_delta`，不得在 Provider 内等待完整响应后再
  一次性提交；只支持非流式响应的 Provider 可以产生完整 `tool_call`。
- `tool_call_delta.index` 是当前 assistant 响应内从零开始的工具调用序号，并稳定关联同一调用；Provider
  适配器必须在统一边界内把供应商原生 content-block 坐标归一化，不能向 Core 泄漏。`id_delta`、
  `name_delta` 和 `arguments_delta` 字段均保持追加分片语义。
- `InnerAgentLoop` 按到达顺序产生 `model_tool_call_delta`，并只在 `done` 边界聚合工具调用、解析 arguments
  JSON、校验身份和批次唯一性。
- Provider 可在工具调用响应中产生一个 `continuation` 事件；Inner loop 只接受一个类型化 artifact，并与
  assistant 工具消息原子提交。普通最终回答不保存协议续接状态。
- `done` 只携带完成边界和 usage，不重复携带工具调用，避免增量流和最终快照成为两个互相竞争的真相来源。

## 预算与暂停

软预算只在 durable 模型回合边界检查：

- 下一模型轮次；
- 累计输入 token；
- 累计输出 token；
- 已用时间。

超过软预算进入 `paused`。上下文无法安全构建时以 `context_blocked` 暂停。预算未配置时不限制运行轮数。

## 排队输入

active Run 期间的新输入写入 Session metadata 中的 durable 队列，每项显式保存交付模式：

- `follow_up`：当前 Run 完整结束后交付，每次消费创建一个后续 Run；
- `steer`：当前 model turn 完整结束后交付，作为同一 Run 下一轮模型请求的新 canonical 消息。

每项还显式保存生产来源：

- `user`：真实用户输入，持久化为 `role=user`，可以携带用户资源引用；
- `host`：OURO Host/Application 产生的输入，必须携带稳定 `host_source_id`，持久化为
  `role=system_synthetic`；
- `plugin`：插件产生的输入，必须携带完整 plugin/contribution/generation/contract provenance，持久化为
  `role=system_synthetic`。

Host 和插件输入都不能冒充用户，也不能携带用户资源引用。未知来源、Host 来源缺少 source id、插件来源缺少完整
执行归属的持久化项均按无效项移除。

工具请求及结果共享原子 turn，steer 必须在工具结果提交后插入。`queued_inputs.consumption_mode` 决定每个
消费边界只取一条（`one_by_one`）或取出当时全部符合交付时机的消息（`all_at_once`）。过滤不同交付模式时，
符合条件的消息保持原始入队顺序；较早的 follow-up 不阻塞较晚 steer 在 turn 边界生效。

队列项具备唯一 id；只有 canonical 消息已经持久化后才确认队列项。恢复时若发现同一 queue id 已进入消息历史，
必须只确认队列而不重复追加消息。缺少必需字段的队列项按无效项移除，不按旧格式补全。

审批和 AskUser 恢复属于原 Run 的同一锁事务。恢复后即使 Run 已 completed，也不能在该锁的作用域内递归
启动 follow-up；后续 Run 必须等恢复入口释放非重入 session run lock 后再取得新的锁。

## 取消语义

- abort 首先持久化 Run 的 aborted 状态，再通知进程内 `AbortSignal`。
- Provider stream、工具和子进程通过协作式轮询观察 signal。
- 未知外部副作用不能因恢复而自动重放。
- terminal Run 的后续迟到写入不能把 Session 恢复为 running。

## 进程内监督

`RuntimeSupervisor` 在 RuntimeService 外管理跨 Session FIFO 队列、全局并发上限、状态快照和 Host 订阅。
它不复制本节的 durable Run 状态机。Host detach 只关闭订阅；explicit abort 通过 RuntimeService 先持久化
aborted；shutdown 在有限时间内 wait 或显式 abort。未在超时内完成的 Run 由 SessionStore 在下次启动恢复。

`RuntimeActivitySnapshot` 是 Host-facing 投影：Run、ToolBatch 和 ToolCall 提供 durable 权威状态，模型增量
及 compaction 事件补足响应中的瞬时阶段。投影必须能从 SessionSnapshot 重建；`Session.status` 继续保持粗粒度，
不得扩张为 UI 状态枚举。活动订阅是可选能力，已有 durable event 订阅语义不因 UI 投影而改变。

工具批次在 `tool_batch_planned` 前持久化 planned calls 和 `tool_preflight`，结果写入 canonical messages 前产生
`tool_batch_results_committing`。`tool_batch_started` 携带预检后的逐调用状态，交互恢复与调用完成事件也携带
精确 ToolCall status；Host 不得仅凭 `is_error` 推断 aborted/failed。因而 Host 不需要从相邻事件间隙猜测
预检、执行结果和提交阶段。

## 失败语义

- 配置、持久化和 session lock 错误必须保持类型化异常。
- Provider 失败转换为模型流 error event，再由 inner loop 结束 Run。
- Hook 和 Host event sink 失败隔离为 diagnostic/event，不破坏 durable Run。
- 工具失败作为 `ToolResult` 回灌模型；协调器自身失败才结束 Run。

## 验收证据

- `tests/core/test_inner_loop.py`
- `tests/application/test_runtime.py`
- `tests/application/test_followups.py`
- `tests/application/test_queued_input_modes.py`
- `tests/application/test_events.py`
- `tests/providers/test_openai_compatible.py`
- `tests/architecture/test_boundaries.py`

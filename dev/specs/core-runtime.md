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
- 一次模型响应中的 assistant 工具请求及其结果共享 `turn_id`。
- `turn_id` 只能出现在拥有 `run_id` 的消息上。
- 工具身份不得写入 `metadata`。

## 运行流程

1. Host 调用 `RuntimeService.run`。
2. 新会话被创建，或现有会话在无进程锁时执行恢复检查。
3. 取得 session run lock；锁竞争失败时返回 active Run 或明确的 `SessionBusyError`。
4. `RunLifecycle.start` 持久化 active Session/Run。
5. 用户消息持久化并产生事件。
6. `InnerAgentLoop` 构建上下文并调用 request-local `RuntimeAdapter`。
7. 模型返回最终消息时完成 Run；返回工具调用时交给 `ToolCallCoordinator`。
8. waiting 状态通过 approve/reject/AskUser 恢复同一 Run。
9. terminal Run 释放 lock、abort signal 和每 Run Hook 隔离状态。
10. 在 model turn 边界消费 steer 输入，在 completed Run 边界串行处理 follow-up 输入。

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
- `steer`：当前 model turn 完整结束后交付，作为同一 Run 下一轮模型请求的新用户消息。

工具请求及结果共享原子 turn，steer 必须在工具结果提交后插入。`queued_inputs.consumption_mode` 决定每个
消费边界只取一条（`one_by_one`）或取出当时全部符合交付时机的消息（`all_at_once`）。过滤不同交付模式时，
符合条件的消息保持原始入队顺序；较早的 follow-up 不阻塞较晚 steer 在 turn 边界生效。

队列项具备唯一 id；只有用户消息已经持久化后才确认队列项。恢复时若发现同一 queue id 已进入消息历史，
必须只确认队列而不重复追加消息。旧 follow-up 队列项没有模式字段时按 `follow_up` 解释。

## 取消语义

- abort 首先持久化 Run 的 aborted 状态，再通知进程内 `AbortSignal`。
- Provider stream、工具和子进程通过协作式轮询观察 signal。
- 未知外部副作用不能因恢复而自动重放。
- terminal Run 的后续迟到写入不能把 Session 恢复为 running。

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
- `tests/architecture/test_boundaries.py`

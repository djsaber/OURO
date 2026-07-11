# OURO Session Persistence Write Model

- Date: 2026-07-12
- Status: Accepted / implemented
- Parent: `2026-07-11-session-core-foundation.md`

## 1. Problem

旧实现把每个 provider text/reasoning delta 同时写入 `events.jsonl`，并重新生成 run snapshot 与
`session.json`。这产生了两份 partial-output truth，也让一次回复触发数百次完整聚合读取和原子文件
替换。在 Windows 上，实时扫描器或读取者短暂持有 `session.json` 就可能让下一次 replace 失败，进而
把正常 provider stream 误判成 run failure。

该问题不能通过 application 层的 timer、后台 retry queue 或额外 checkpoint cache 解决；这些机制会
增加第三份状态和新的 shutdown/race 语义。

## 2. Canonical write roles

会话持久化按数据形态分工：

- `session.json`：会话生命周期快照，只在运行开始、durable message 推进、等待状态、终止状态和显式
  session command 时替换；
- `runs/<run_id>.json`：run 生命周期快照，只保存 run 状态、usage、等待信息和终止结果；
- `messages.jsonl`：已经提交的用户、assistant、tool-result 消息，顺序追加；
- `events.jsonl`：运行过程 journal，流式 text/reasoning delta 只写这里；
- `sessions/index.json`：可删除重建的查询投影，不参与 canonical 写入成功判定。

因此，流式 partial output 只有 event journal 一份 durable truth。`InnerAgentLoop` 不再拥有 checkpoint
持久化 port，`RunRecord.metadata` 也不再更新 `checkpoint` 副本。

## 3. Recovery

进程重启发现无活锁的 queued/running run 时，从该 run 最后一个 `model_request_started` 之后的
`model_text_delta` / `model_reasoning_delta` 重建 incomplete assistant message。如果该轮已经提交了
assistant tool-call 或 tool-result message，则不重复物化；新一轮已开始时只恢复新一轮的 delta。

恢复完成后 run 转为 `recoverable`，session 清除 `active_run_id`，并写入单一 recovery event。旧
`metadata.checkpoint` 不再作为 truth，终止或恢复时会被清除。

## 4. Atomic replacement

Snapshot 继续使用同目录临时文件、flush、`fsync` 和原子 replace。底层只对 Windows 明确的 sharing
violation / access-denied replace 竞争做有限指数退避；非瞬时错误立即失败，临时文件始终清理。

该重试属于文件系统适配层对 OS sharing semantics 的封装，不改变 application 状态机，也不掩盖永久
ACL、只读或路径错误。

## 5. Deliberate limits

当前 event journal 每条事件仍同步追加并 `fsync`，优先保证 crash recovery。若性能测试证明需要批量
写入，后续必须定义独立的 journal flush/durability contract，不能重新通过高频 snapshot replace
实现 partial persistence。

## 6. Verification

测试覆盖：

- 60 个流式 delta 不增加 session/run snapshot 写次数；
- crash 后从 event journal 恢复 partial text/reasoning；
- tool round 已提交的消息不会被重复恢复；
- Windows replace 瞬时错误会有限重试，非瞬时错误不会重试且不残留临时文件。

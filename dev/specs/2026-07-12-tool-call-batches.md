# OURO Tool-Call Batch Runtime

- Date: 2026-07-12
- Status: Accepted / implemented
- Extends: `2026-07-07-phase-1-core-runtime.md`
- Extends: `2026-07-07-tools-permissions-hitl.md`

## 1. Problem

Provider protocols may return multiple tool calls in one assistant response. The original Phase 1
runtime converted that response into a sequence of single-call events and immediately handled each
event. That lost the assistant-message boundary, forced automatic tools to execute serially, and
dropped later calls whenever an earlier call suspended the run for approval or `AskUser`.

The runtime must treat the complete assistant response and its ordered tool-call batch as durable
domain concepts. Concurrency is an execution policy; it must not change transcript identity,
approval semantics, result correlation, or recovery behavior.

## 2. Invariants

1. One completed provider response produces exactly one assistant message.
2. The assistant message preserves every provider tool call in source order.
3. No tool starts before the provider response is complete and the assistant message is durable.
4. Every tool call receives exactly one correlated tool-result message.
5. Planning and human interaction finish before batch execution starts.
6. A rejected or failed call does not cancel unrelated calls in the same batch.
7. Execution completion order may vary; committed tool-result messages use source order.
8. The next model request starts only after all batch results are durable.
9. Run/session snapshots change from `waiting_approval` to `running` before approved tools start.
10. An in-flight non-idempotent tool with an unknown crash outcome is never automatically replayed.

## 3. Provider and turn boundary

Text and reasoning remain streaming events. Complete tool calls belong to the terminal provider
event. The core creates one immutable assistant turn containing text, reasoning, usage, and an
ordered tuple of typed tool calls. Legacy singular tool-call stream events may be accepted during
migration, but the inner loop only accumulates them and never executes them before the terminal
event.

Every model request receives a `turn_id`. The assistant message, batch, invocations, approvals,
tool-result messages, and runtime events produced by that request carry the same `turn_id`.

## 4. Batch lifecycle

Batch phases:

- `planned`
- `awaiting_interaction`
- `ready`
- `executing`
- `committing_results`
- `completed`
- `aborted`
- `recoverable`

Per-call states:

- `planned`
- `awaiting_approval`
- `waiting_user`
- `ready`
- `rejected`
- `running`
- `completed`
- `failed`
- `aborted`
- `unknown_outcome`

Preflight resolves the tool, validates arguments, resolves the effective permission mode, and runs
`before_tool_call` for every call before execution. Calls requiring approval receive independent
approval records. Automatic calls wait behind the same interaction barrier; no partial batch is
executed while another call is unresolved.

Rejection produces an error tool result for that call. Once every interaction is resolved, approved
and automatic calls execute according to the snapshotted batch policy. `AskUser` is also an
interaction barrier and cannot cause sibling calls to be discarded.

## 5. Execution policy

The typed configuration is:

```text
ToolExecutionConfig:
  mode: sequential | parallel
  max_concurrency: positive integer
```

The compatibility default is `sequential`. `parallel` uses bounded concurrency. A tool definition
may force sequential batch execution when concurrent use is unsafe. Configuration is resolved by
the composition root and snapshotted into the batch; core code does not read configuration files or
environment variables.

Each invocation has an isolated execution wrapper. Tool exceptions become error results and do not
cancel siblings. Run abort propagates one shared abort signal to all active calls. Persistence,
state-transition, and batch-level hook failures remain batch failures rather than tool results.

## 6. Persistence and recovery

Canonical roles remain unchanged:

- `messages.jsonl`: committed assistant and tool-result transcript;
- `runs/<run_id>.json`: active batch, approvals, execution checkpoint, and run lifecycle;
- `events.jsonl`: streaming and execution journal;
- `session.json`: session lifecycle snapshot;
- `sessions/index.json`: rebuildable catalog projection.

The assistant message is committed before the active batch snapshot. A crash in that window can
reconstruct the batch from the assistant message. Each completed execution outcome is checkpointed
in the run before source-ordered tool-result messages are committed. Result commits are idempotent
by `tool_call_id` and recorded message id.

Recovery preserves resolved approvals and completed results. Before a new run starts, every
assistant tool call without a canonical result receives exactly one recovery result. A
checkpointed result is committed as-is; a call recorded as running receives an error result with
`unknown_outcome=true` and an instruction to verify external state before retrying. Recovery never
automatically re-executes a tool unless a future tool contract explicitly declares retry safety.

## 7. Event order

Normal batch:

1. `model_request_started`
2. streaming text/reasoning events
3. `model_message_completed`
4. `tool_batch_planned`
5. `tool_call_requested` for each source call
6. `approval_requested` for each protected call
7. `run_status_changed` when waiting
8. `approval_resolved` for each user decision
9. durable transition to running/tool execution
10. `tool_batch_started`
11. per-call `tool_call_started` / `tool_call_completed`
12. source-ordered tool-result message commit
13. `tool_batch_completed`
14. next `model_request_started`

Events contain `turn_id`, `batch_id`, `tool_call_id`, and source index where applicable.

## 8. Compatibility

Legacy assistant metadata and singular pending approval data are normalized on read into a
single-call batch. New writes use the typed batch representation. Compatibility fields are derived,
not treated as a second source of truth, and are removed after the migration window.

## 9. Acceptance

- Non-streaming and streaming providers preserve 0/1/N tool calls.
- Automatic batches work in sequential and parallel modes.
- Approval batches never execute before all decisions are resolved.
- Approving one of multiple calls keeps the run waiting; resolving the final approval starts one
  batch execution.
- Rejection, argument validation failure, tool failure, timeout, and cancellation are isolated per
  call.
- Session catalog state changes away from `waiting_approval` before a long approved tool starts.
- Transcript order and call/result correlation remain deterministic under parallel completion.
- Crash recovery does not duplicate messages, approvals, or non-idempotent tool side effects.

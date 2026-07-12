# OURO Context Frames, Recovery, and Run Soft Budgets

- Date: 2026-07-12
- Status: Accepted / implemented
- Extends: `2026-07-11-built-in-summary-compaction.md`
- Extends: `2026-07-12-tool-call-batches.md`

## 1. Problem and decision

A long agent run can contain many model/tool rounds after one user message. Treating the whole
user-to-user interval as one compaction unit eventually leaves no eligible prefix even though the
canonical transcript already records the correct per-model-request `turn_id`.

OURO therefore uses a context frame, not a user conversational turn, as the compaction boundary.
One current-format frame is all messages carrying one contiguous `turn_id`: the assistant model
response and every correlated tool result. Messages without `turn_id` remain valid for user input,
synthetic context, imports, branches, and legacy data. Legacy tool exchanges are grouped by typed
or migrated call/result ids.

No second frame model is persisted. The existing `MessageRecord` is the source of truth.

## 2. Message identity invariants

- Canonical runtime user messages require `run_id` but do not require `turn_id`; they exist before
  the next model request starts.
- Every runtime assistant model response has both `run_id` and the request's `turn_id`, including
  an incomplete response persisted after a stream failure.
- Every tool-result message has the `run_id` and `turn_id` of its assistant tool request.
- `turn_id` without `run_id` is invalid.
- Both ids remain optional in the general schema because synthetic compaction context, imported
  history, branch projections, and legacy records are not necessarily produced by a live run.

This keeps the storage contract compatible while making current runtime writes strict.

## 3. Context preparation and compaction

Token estimates include text blocks, resources, typed tool calls, typed tool-result references,
and metadata. Compaction selects a canonical prefix of complete frames and retains a recent frame
tail. An incomplete legacy tool exchange protects the remaining suffix so call/result protocol
order cannot be split.

Automatic compaction failure has two outcomes:

1. If the complete current projection still fits the provider input budget, emit
   `compaction_failed`, mark the context metadata as deferred, and continue the model request.
2. If compaction is required to fit, pause the run with `reason=context_blocked`, return the
   session to `idle`, and preserve all canonical history for manual maintenance or a larger model.

Provider requests never receive an over-budget view and no history is silently truncated.

## 4. Interrupted-run reconciliation

Filesystem recovery first converts a run left in an active status into terminal
`recoverable`. Under the next session run lock, the application recovery coordinator then closes
canonical protocol gaps before appending the new user message:

- a durable checkpointed tool result is committed idempotently;
- a tool that was running receives an error result whose outcome is explicitly unknown;
- a tool that never produced a durable result receives an interrupted error result;
- no tool is automatically replayed;
- an incomplete plain assistant stream remains canonical but is projected to the provider as
  `system_synthetic` recovery context, so it cannot masquerade as a completed assistant answer.

Recovery messages and events retain the original `run_id`/`turn_id`. A terminal old run cannot
become active again while reconciliation updates its transcript.

## 5. Run soft budget

`RunBudgetPolicy` supports optional limits for:

- model rounds;
- cumulative provider input tokens;
- cumulative provider output tokens;
- elapsed wall-clock seconds.

Every field defaults to `None`, so the compatibility behavior is unlimited. Limits are checked
only before a new model request, which is a durable context-frame boundary. Exceeding a limit
creates a terminal `paused` run and an idle session; it is not an execution failure and it does
not discard tool results already committed in the previous frame. The rebuildable session catalog
projects the latest run as `paused`, while the canonical session remains idle and continuable.

Configuration is typed and persisted in shared `config/config.json` under `run_budget`.
`ouro config run-budget` reads or updates it, and `--clear` restores unlimited defaults.

## 6. Follow-up queue and future steering

This change deliberately keeps the current input behavior queue-only: input received during an
active run is durable FIFO work that starts a new run after the current run completes. Queue
entries are acknowledged only after their user message is durable.

Future steering is a different lifecycle operation and should not be represented by today's
follow-up queue item. Its insertion point is the inner-loop frame boundary after the current
assistant/tool frame is durable and before the next model request. A later design can add an
application input-mode discriminator and a `consume_steering` loop port at that boundary.

That work is deferred because it also needs precise policy for approval waits, aborts, simultaneous
writers, and durable inbox storage. Adding a mode field now without those semantics would create a
false public contract and couple steering to the current session-metadata queue. The current
`FollowUpQueue` and storage format remain unchanged.

## 7. Failure behavior matrix

| Interruption | Durable result | Next conversation behavior |
| --- | --- | --- |
| Provider error or stream disconnect | Failed run; partial assistant marked incomplete | Partial text is recovery context; a new run can continue |
| Terminal closed during provider stream | Active run recovered; streamed events may reconstruct partial text | Same projection as stream failure |
| Tool exception or timeout | Correlated error tool result | Same run continues to the model |
| Terminal closed during tool execution | Recoverable run; missing result becomes unknown/interrupted result | No automatic replay; new run continues after reconciliation |
| Auto-compaction strategy failure, context fits | Failure event only | Current run continues with full context |
| Compaction required but unavailable | Paused run with `context_blocked` | Session remains usable for a later run/manual compaction/model switch |
| Network loss after completed response was committed | Completed canonical message/run | Later input starts normally |
| Multiple sequential follow-ups | Durable FIFO queue | Each starts after the preceding run completes |

## 8. Deferred boundary

Session storage scaling, a dedicated concurrent inbox/journal, large-resource externalization, and
provider-specific exact tokenizers remain separate persistence/performance work. This refactor does
not introduce those concerns into the core frame or recovery model.

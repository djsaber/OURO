# Goal Built-in Plugin

## Overview

`dev.ouro.goal` maintains one user-owned Goal for a primary conversation and drives that Goal
through bounded Runs. The user owns objective, lifecycle, and budget decisions; the model can read,
create, complete, or report a genuine blocker only through the declared Goal tools.

## Plugin Metadata

| Field | Value |
|---|---|
| Plugin ID | `dev.ouro.goal` |
| Namespace | `goal` |
| Version | `2.0.0` |
| Runtime | In-process Python (`python-inprocess`) |
| Default state | Enabled |
| Dependencies | None |
| Declared permissions | `sessions.write`, `background.run`, `sessions.prompt.inspect` |

The plugin is shipped with OURO and participates in the standard built-in enable/disable lifecycle.
It does not use the user-plugin installation lifecycle.

## Contributions

| Type | ID / Public Name | Contract |
|---|---|---|
| Tool | `get-goal` / `GetGoal` | Auto-approved, read-only access to the latest Goal |
| Tool | `create-goal` / `CreateGoal` | Host-approved, state-changing, exclusive-batch Goal creation |
| Tool | `update-goal` / `UpdateGoal` | Auto-approved, state-changing, exclusive-batch transition to `complete` or `blocked` |
| Command | `/goal` | Creates, inspects, pauses, resumes, replaces, or cancels the Goal |
| Session Controller | `mode` | Owns one Goal lifecycle and exclusively claims `ouro.session.primary_behavior` |
| Continuation Policy | `goal-continuation` | Reads native usage and decides whether bounded work continues or stops |
| Prompt Policy | `goal-input-gate` | Prevents ordinary prompts from implicitly resuming a stopped Goal |

The three tools are exposed together in every Goal state. Stable tool visibility preserves the
model-facing tool prefix across a conversation. Exact descriptions and schemas are defined in
[`definition.py`](definition.py), [`goal_plugin.py`](goal_plugin.py), and
[`prompts.py`](prompts.py).

## Behavior

### Goal Model

The Session owns a capacity-one Goal slot. Its schema 4 business value contains a stable goal_id,
objective, status, optional total token budget, report, and timestamps. Execution bindings and actual
usage come from framework-owned read-only views. Supported statuses are:

- `active`;
- `paused`;
- `blocked`;
- `budget_exhausted`;
- `complete`.

A completed Goal remains available to status queries. Creating another Goal replaces the completed
record. Cancelling removes the current record entirely.

### User Commands

| Invocation | Behavior |
|---|---|
| `/goal [--token-budget N] <objective>` | Delivers one creation instruction and the exact objective as an atomic pair of user messages; the model must call `CreateGoal` |
| `/goal status` | Reads the latest Goal without changing it |
| `/goal pause` | Pauses an active Goal |
| `/goal resume [--token-budget N]` | Resumes `paused`, `blocked`, or `budget_exhausted` work |
| `/goal replace [--token-budget N] <objective>` | Replaces an unfinished objective while preserving creation time and cumulative usage |
| `/goal cancel` | Releases the controller and removes the Goal |

Omitting `--token-budget` during creation means unlimited work. Omitting it during resume or
replace clears an existing budget. An explicit replacement budget must be greater than the tokens
already used.

Lifecycle commands return the accepted action and controller settlement; query status or GetGoal
for the current business state and native usage.

### Model Tools

`GetGoal` returns the latest public Goal state. `CreateGoal` creates an active Goal only when no
unfinished Goal occupies the slot. `UpdateGoal` can set only `complete` or `blocked` and requires
the current execution binding; it cannot change identity, activation, usage, or budget.

### Execution and Budget Settlement

Each model request captures native controller bindings. Goal reads that request's activation and
usage scope to authorize updates. Pause, resume, and replace rotate the native activation; only
creation allocates a new goal_id. Goal uses no Context writer or private Run binding.

The native usage ledger attributes each response to the goal_id bound when its request started.
Requests before activation or after release have no Goal scope. Reads, retries, and undo do not
recount or refund usage. Paused controllers retain their claim without a usage scope. At the end of
bounded execution, a valid complete or blocked transition takes precedence over the budget check.
Otherwise:

- reaching the cumulative budget sets `budget_exhausted`;
- unavailable usage for a budgeted execution pauses work instead of guessing;
- a non-terminal active Goal receives an attributed continuation input;
- failed, aborted, paused, or recoverable Runs stop automatic continuation with an explicit report.

## State and Boundaries

- Goal state is canonical Session extension state in the `dev.ouro.goal` namespace.
- State uses a versioned envelope, atomic updates, and `branch_policy="reset"`.
- The plugin's private data directory does not store canonical Goal state.
- Goal mode is supported only for primary conversations and competes for the same exclusive primary
  behavior claim as other Session modes.
- User commands own pause, resume, replace, cancel, and budget changes; the model cannot infer or
  perform those user-owned transitions.
- Provenance-bearing Goal scaffolds use user-message roles and explicit semantic tags.

## Source Layout

- [`definition.py`](definition.py): built-in definition, permissions, and manifest.
- [`goal_plugin.py`](goal_plugin.py): contributions, controller lifecycle, tools, policies, and
  settlement.
- [`commands.py`](commands.py): `/goal` parsing and user-owned lifecycle commands.
- [`state.py`](state.py): versioned business state and native control/usage projections.
- [`prompts.py`](prompts.py): model-facing tool and continuation instructions.

The implementation and manifest are the source of truth for tool schemas, transition validation,
permissions, and continuation decisions.

Only schema 4 is supported. Unsupported state fails explicitly; historical data is not converted.

## Verification

```bash
uv run pytest tests/plugins/test_builtin_goal.py -q
```

Application and Runtime tests additionally cover Session claims, continuation arbitration, usage
settlement, recovery, and bounded-Run behavior.

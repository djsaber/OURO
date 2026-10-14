# Goal Built-in Plugin

`dev.ouro.goal` maintains one user-requested objective for a primary conversation. The system
continues at safe execution boundaries; the model checks evidence and explicitly reports completion
or a genuine blocker. Users own pause, objective changes, cancellation, and budgets.
Users or the model can explicitly resume a stopped goal.

## Commands

```text
/goal
/goal status
/goal [--token-budget N | --no-token-budget] <objective>
/goal pause
/goal resume [--token-budget N | --no-token-budget]
/goal replace [--token-budget N | --no-token-budget] <objective>
/goal cancel
```

Creation commits Goal state, control ownership, and an initial message batch before the model starts.
It does not require a CreateGoal call. An unfinished Goal or another mode's ownership rejects creation.
Bare /goal reads status; cancel removes the goal. Replace preserves identity, creation time, and usage.

Creation without a budget is unbudgeted. Resume and replace preserve the existing budget by default.
--token-budget N sets a positive cumulative total greater than usage; --no-token-budget removes the
limit. Resuming an exhausted budget requires increasing or removing it.

## Model Interface

All three tools remain visible in every state. Their permissions are plugin-controlled and
automatic; Host permission overrides do not apply. State validation, control arbitration and tool
policies still apply.

| Tool | Contract |
|---|---|
| GetGoal | Read objective, status, budget, actual usage, timestamps, and report |
| CreateGoal | Create from an explicit natural-language request using the same transition as the command |
| UpdateGoal | Resume with active, or set complete/blocked, with evidence and a matching request binding |

Ordinary tasks do not authorize creation. Completion requires a checked result and no required work
remaining. Blocking requires the same obstacle across three consecutive Goal work turns and no path
forward without user input or external change. Retries within one turn do not count as separate turns.
Resume starts a fresh blocker assessment.

## Context and Controls

Temporary goal-context reads current state for each request, including after compaction. It contains
the objective, status, total budget, relevant report, and action rules. Changing usage and timestamps
stay out of the request prefix. Pending creation, replacement and cancellation batches coalesce.
Raw objective text remains a non-scaffold user message in the initial atomic batch.

Pause commits goal state with the framework's generic stop intent and cancels the current execution.
Resume uses the generic continue intent to resume retained work or consume the initial pending input.
Neither operation adds a scaffold or input message. The framework owns cancellation, settlement and
checkpoint recovery; completed tool results are retained without replaying their execution.

Stopped goals allow normal conversation, tools and /continue without automatic goal resumption.
The model can call UpdateGoal(active) when current evidence justifies resuming, preserving the budget;
the user can also use /goal resume. Exhausted budgets require user action. Discussion has normal
conversation usage but no Goal scope. Cancel leaves Goal mode.

## Execution and Accounting

Goal uses schema 4 owner-bound Harness state for identity, objective, status, budget, timestamps and
report. Native views supply request bindings and usage. Completed records remain readable;
cancellation deletes the record; a new Goal gets a new identity.

Budgets are checked after complete bounded execution, allowing a final overshoot. Explicit complete
or blocked results precede budget settlement. Missing budgeted usage or abnormal execution pauses
work. An automatic continuation without tool calls also pauses to avoid empty repetition; this does
not claim the task is blocked or complete. Reports explain stop reasons without another model request.

Requests keep the scope captured at their start. Late usage belongs to the original Goal; creation
does not reassign earlier requests. Undo, resume and repeated settlement do not refund or double-count
usage. Old requests cannot complete a newly activated objective.

## Integration

- Default-enabled in-process plugin, namespace goal, version 2.0.0.
- Controller mode exclusively claims ouro.session.primary_behavior, shared with Plan.
- Context goal-context; continuation goal-continuation.
- Permissions: sessions.write and background.run.
- State uses branch_policy=reset; only schema 4 is supported.
- No private store, polling loop, history service, or permission escalation.

commands.py handles commands; goal_plugin.py composes tools, transitions and policies; state.py owns
business state and native projections; prompts.py owns model-facing language; definition.py declares
the manifest. See [the full specification](../../../docs/specs/goal-plugin.md).

## Verification

```bash
uv run pytest tests/plugins/test_builtin_goal.py tests/plugins/test_goal_interactions.py -q
```

Targeted Application and SDK tests cover command atomicity, continuation facts, coalescing,
request-local context, and recovery.

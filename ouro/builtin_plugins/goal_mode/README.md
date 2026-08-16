# Goal builtin plugin

`dev.ouro.goal_mode` maintains a capacity-one slot containing the latest Goal for a primary
conversation. A completed Goal is retained for status queries; creating a new Goal replaces that
completed record, while `/goal cancel` empties the slot.

The user owns lifecycle and budget controls:

- `/goal [--token-budget N|unlimited] <objective>` asks the model to call `CreateGoal`. The
  creation instruction and exact objective are delivered as one atomic pair of user messages.
- `/goal pause`, `resume`, `replace`, and `cancel` update state without asking the model to infer
  user intent. Replace preserves creation time and cumulative token usage.
- `GetGoal` reads the latest record. `UpdateGoal` can only set `complete` or `blocked`; it has no
  goal identity or budget parameter.

Every active execution is privately bound to the activation that started it. Updating, pausing,
or resuming a Goal rotates that activation, so an older execution cannot complete the new
objective. All Goal scaffolds use the user message role with an explicit semantic tag; provenance
continues to identify the plugin as their source.

Token usage is settled after a complete bounded execution. A completed or blocked update in that
execution wins; otherwise reaching the cumulative budget sets `budget_exhausted` and prevents a
new Goal execution. Resuming that state requires an explicitly larger total budget or `unlimited`.
The latest Goal remains readable in every state.

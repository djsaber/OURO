# Goal Mode builtin plugin

`dev.ouro.goal_mode` owns one durable Goal for a primary Session. It composes only public
plugin contracts:

- `/goal` is the user intent and lifecycle control plane. New creation requests are submitted to
  the current Agent and activate only through `CreateGoal`; status, pause, resume, cancel, and
  replace remain user-owned commands.
- `GetGoal`, `CreateGoal`, and `UpdateGoal` are the model-facing read/create/settle tools.
- the `mode` Session Controller exclusively claims `ouro.session.primary_behavior`.
- `goal-scaffold` persists the objective and completion rules in model context.
- `goal-continuation` accounts canonical usage and schedules the next bounded Run.

The Goal record is plugin-private canonical state. Normal Run completion never implies Goal
completion. Only `UpdateGoal(complete|blocked)`, an explicit user lifecycle command, a Runtime
failure, or a token-budget boundary stops automatic continuation.

The plugin is disabled by default. Enable `dev.ouro.goal_mode` and authorize its declared
`sessions.write` and `background.run` permissions before using `/goal`.

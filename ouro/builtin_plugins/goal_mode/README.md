# Goal Mode builtin plugin

`dev.ouro.goal_mode` owns one durable Goal for a primary Session. It composes only public
plugin contracts:

- `/goal` is the user intent and lifecycle control plane. Creation and explicit replacement
  requests are submitted to the current Agent and activate only through `CreateGoal`; status,
  pause, resume, cancel, and replacement authority remain user-owned.
- `GetGoal`, `CreateGoal`, and `UpdateGoal` are the model-facing read/create/settle tools.
- the `mode` Session Controller exclusively claims `ouro.session.primary_behavior`.
- `goal-scaffold` persists the exact source request, six-field Goal Contract, and completion rules
  in model context under a `goal-contract` semantic envelope.
- `goal-continuation` accounts canonical usage and schedules the next bounded Run.

`CreateGoal` must never be called autonomously. It is allowed only after the current user
explicitly requests Goal creation or replacement. The Agent preserves `source_request` and
compiles `outcome`, `verification`, `constraints`, `boundaries`, `iteration_strategy`, and
`blocked_conditions`; the state-changing tool remains subject to Host approval.

The Goal record is plugin-private canonical state. Normal Run completion never implies Goal
completion. `UpdateGoal(complete)` requires auditing the contract's verification items and
constraints. `UpdateGoal(blocked)` requires a genuine user-input or external-state blocker.
An explicit user lifecycle command, a Runtime failure, or a token-budget boundary also stops
automatic continuation.

The plugin is disabled by default. Enable `dev.ouro.goal_mode` and authorize its declared
`sessions.write` and `background.run` permissions before using `/goal`.

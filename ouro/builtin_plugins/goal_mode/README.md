# Goal Mode builtin plugin

`dev.ouro.goal_mode` owns one durable Goal for a primary Session. It composes only public
plugin contracts:

- `/goal` is the user intent and lifecycle control plane. Creation and explicit replacement
  requests are submitted as user-role inputs with plugin provenance and activate only through
  `CreateGoal`; internal continuation stays synthetic, while status, pause, resume, cancel, and
  replacement authority remain user-owned.
- `GetGoal`, `CreateGoal`, and `UpdateGoal` are the model-facing read/create/settle tools.
- the `mode` Session Controller exclusively claims `ouro.session.primary_behavior`.
- `goal-scaffold` persists the exact source request, six-field Goal Contract, and completion rules
  in model context under a `goal-contract` semantic envelope.
- `goal-continuation` accounts canonical usage and schedules the next bounded Run.

`CreateGoal` must never be called autonomously. It is allowed only after the current user
explicitly requests Goal creation or replacement. The Agent preserves `source_request` and
compiles `outcome`, `verification`, `constraints`, `boundaries`, `iteration_strategy`, and
`blocked_conditions`; the state-changing tool remains subject to Host approval.

The Goal record is private, owner-bound canonical Session extension state. It is stored by the
Host's SessionRepository rather than under plugin data, so branch/export/trash cannot separate
the `goal-contract` projection from its source state. Goal uses `reset` branch semantics: a new
branch contains neither the active Goal nor inherited Goal projection. Normal Run completion never implies Goal
completion. `UpdateGoal(complete)` requires auditing the contract's verification items and
constraints. `UpdateGoal(blocked)` requires a genuine user-input or external-state blocker.
The active scaffold requires `UpdateGoal` before a terminal response; a bounded Run that omits
the terminal update is explicitly treated as nonterminal and receives a continuation audit.
An explicit user lifecycle command, a Runtime failure, or a token-budget boundary also stops
automatic continuation.

The plugin is disabled by default. Enable `dev.ouro.goal_mode` and authorize its declared
`sessions.write` and `background.run` permissions before using `/goal`.

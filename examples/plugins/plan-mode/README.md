# Plan mode plugin example

This in-process example implements plan mode entirely through OURO's public
plugin contracts. It does not add a plan branch to the agent loop.

It contributes:

- `/plan`, `EnterPlanMode`, and resumable `ExitPlanMode` entry/exit paths;
- a required Context Provider whose scaffold messages are persisted;
- a Tool Policy that permits read-only tools and the shared `Write`/`Edit`
  tools only for the current plan file without adding extra restrictions.

Plan mode does not control whether a Run continues or completes. Its two tools
return the current transition result to the model, which remains responsible
for deciding whether to call another tool or finish the current response.

Plan files live under the current workspace at
`.ouro/sessions/<session_id>/plans/`. Keeping the artifact inside the workspace
allows the existing `Write` and `Edit` tools to retain their normal
`workspace_only` path boundary. The plugin does not contribute duplicate file
tools and OURO Core does not know about a privileged plan path.

The phase, artifact reference, and pending approval are stored in the plugin's
private data as one file per Session. This keeps concurrent Sessions and old
Generation recovery from overwriting unrelated state.

Install it with the normal explicit trust flow:

```bash
uv run ouro plugin validate examples/plugins/plan-mode --json
uv run ouro plugin install examples/plugins/plan-mode --json
uv run ouro plugin authorize dev.ouro.examples.plan_mode --json
uv run ouro plugin trust-code dev.ouro.examples.plan_mode --json
uv run ouro plugin enable dev.ouro.examples.plan_mode --json
```

In an interactive Session, enter `/plan` and then send the planning request as
the next message, or let the model call `EnterPlanMode`. The command only
changes plugin state; it does not submit a model input or start a Run.
`ExitPlanMode` produces a `plan_confirmation` input request.
A Host answers with one of these JSON values:

```json
{"action":"accept"}
{"action":"accept","option":"Option A"}
{"action":"revise","feedback":"Keep the public API stable."}
{"action":"reject"}
```

If options were supplied to `ExitPlanMode`, an accepted answer must select one
of their labels. The plugin also verifies that the plan file has not changed
between display and approval.

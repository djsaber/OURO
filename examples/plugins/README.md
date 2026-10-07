# OURO plugin examples

These packages are focused examples and validate against the current development schema.

- `declarative-skill`: no executable plugin code; contributes one Agent Skill.
- `inprocess-command`: trusted Python in-process command contribution.
- `inprocess-observer`: non-blocking typed observation of a completed Run.
- `inprocess-prompt-policy`: fail-closed rejection before a user prompt is persisted.
- `worker-tool`: protocol 3.0 stdio worker with one approval-gated Tool; its call count and last value are proposed through the call-scoped Session state, then committed with the result.

Validate any example before installing it:

```bash
uv run ouro plugin validate examples/plugins/declarative-skill --json
```

Installation never enables a plugin. Enabling explicitly accepts and runs the selected immutable
digest. Follow the [plugin development guide](../../docs/development/plugin-development.md) for the
complete local flow.

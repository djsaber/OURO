# OURO plugin examples

These packages are intentionally small and validate against the current development schema.

- `declarative-skill`: no executable plugin code; contributes one Agent Skill.
- `inprocess-command`: trusted Python in-process command contribution.
- `worker-tool`: stdio JSON-RPC worker with one approval-gated Tool.

Validate any example before installing it:

```bash
uv run ouro plugin validate examples/plugins/declarative-skill --json
```

Installation never enables or grants a plugin. Follow
[`docs/OURO_插件开发指南.md`](../../docs/OURO_插件开发指南.md) for the complete local flow.

# Agent Context Built-in Plugin

## Overview

`dev.ouro.agent_context` adds request-scoped Agent instructions and runtime facts to the complete
system prompt. It implements the product-neutral `AGENTS.md` convention and keeps reusable
instructions separate from environment information.

## Plugin Metadata

| Field | Value |
|---|---|
| Plugin ID | `dev.ouro.agent_context` |
| Namespace | `agent_context` |
| Version | `1.0.0` |
| Runtime | In-process Python (`python-inprocess`) |
| Default state | Enabled |
| Dependencies | None |
| Declared permissions | None |

The plugin is shipped with OURO and participates in the standard built-in enable/disable lifecycle.
It does not use the user-plugin installation lifecycle.

## Contributions

| Type | ID / Public Name | Contract |
|---|---|---|
| Context Provider | `agent-instructions` | Priority 9,000; reloads canonical `AGENTS.md` documents for each model request; failure mode `omit` |
| Context Provider | `runtime-facts` | Priority 10,000; appends system, workspace, and clock facts after Agent instructions; failure mode `omit` |

The two providers remain independent. A failure in instruction discovery does not suppress the
runtime-facts provider. Exact manifest declarations are defined in [`definition.py`](definition.py).

## Behavior

### Instruction Discovery

For every model request, `agent-instructions` loads documents from broad to specific scope:

1. `<user-home>/.agents/AGENTS.md`;
2. `OURO_HOME/AGENTS.md`;
3. each `AGENTS.md` from the Git root through the Session workspace.

Later, more specific project documents take semantic precedence without deleting broader content.
When the workspace has no Git root, only the workspace-level project document is considered.
Descendant directories are not scanned eagerly; the injected contract instructs the Agent to check
target-file ancestors before working below the current workspace.

Discovery applies the following safety and consistency rules:

- only regular files are loaded;
- canonical paths are de-duplicated;
- empty documents are ignored;
- UTF-8 and UTF-8 with BOM are accepted;
- combined source bytes may not exceed 32 KiB;
- unreadable, invalid, or oversized instruction sets fail that provider and are reported through the
  framework's Context Provider diagnostics.

`CLAUDE.md`, `GEMINI.md`, `MEMORY.md`, `SYSTEM.md`, and `SKILL.md` are not aliases for
`AGENTS.md`. Those names have different authority and lifecycle semantics; Agent Skills remain
owned by `ouro.skills`.

### Runtime Facts

`runtime-facts` renders the current date, time zone, request workspace, platform, architecture,
OURO/Python runtime, shell, path style, and preferred encoding. Platform-level facts are captured
when the plugin instance is created; date, time zone, and workspace are resolved for each request.

The provider runs after `agent-instructions`, so the `ouro-system-information` fragment remains at
the end of the complete system prompt.

## State and Boundaries

- Neither provider creates a canonical Session message or durable plugin state.
- Both providers target only the request-local system prompt.
- The plugin does not activate Skills, reinterpret vendor-specific instruction files, or search
  below the current workspace.
- Context Provider execution uses the framework callback boundary; synchronous file discovery does
  not run directly on the Runtime event loop.

## Source Layout

- [`definition.py`](definition.py): built-in definition and manifest.
- [`agent_context_plugin.py`](agent_context_plugin.py): Context Provider contributions and runtime
  fact rendering.
- [`instructions.py`](instructions.py): `AGENTS.md` discovery, validation, ordering, and rendering.

The implementation and manifest are the source of truth for priorities, limits, semantic tags, and
failure behavior.

## Verification

```bash
uv run pytest tests/plugins/test_builtin_agent_context.py -q
```

The verification suite covers source ordering, Git-root behavior, de-duplication, UTF-8 handling,
size limits, runtime facts, and contribution registration.

# Agent Context Built-in Plugin

## Overview

`dev.ouro.agent_context` contributes two ordered fragments to the system prompt. The first contains the applicable `AGENTS.md` instructions. The second describes the execution environment for the Session workspace.

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
| System Prompt Provider | `agent-instructions` | Loads applicable `AGENTS.md` documents at priority 9,000 |
| System Prompt Provider | `runtime-facts` | Appends environment facts after Agent instructions at priority 10,000 |

Both providers use the `omit` failure mode. If instruction discovery fails, the Host records a diagnostic and continues preparing runtime facts. [Manifest](definition.py).

## Behavior

### Instruction Discovery

For every model request, `agent-instructions` uses the public environment port to read documents
from broad to specific scope:

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
  framework's system prompt provider diagnostics.

`CLAUDE.md`, `GEMINI.md`, `MEMORY.md`, `SYSTEM.md`, and `SKILL.md` are not aliases for
`AGENTS.md`. Those names have different authority and lifecycle semantics; Agent Skills remain
owned by `ouro.skills`.

### Runtime Facts

`runtime-facts` renders the current date, time zone, request workspace, platform, architecture,
OURO/Python runtime, shell, path style, and preferred encoding. Platform-level facts are captured
when the plugin instance is created; date, time zone, and workspace are resolved for each request.

The `ouro-system-information` fragment follows `ouro-agent-instructions`. Its date has day precision. With the same documents, environment and local date, the plugin contributes identical text across requests.

## State and Boundaries

- Both providers return request-scoped system prompt fragments. The prepared request records their text in its system prompt.
- The plugin does not activate Skills, reinterpret vendor-specific instruction files, or search
  below the current workspace.
- The Host runs synchronous instruction discovery outside the Runtime event loop.

## Source Layout

- [`definition.py`](definition.py): built-in definition and manifest.
- [`agent_context_plugin.py`](agent_context_plugin.py): system prompt contributions and runtime
  fact rendering.
- [`instructions.py`](instructions.py): render instruction documents supplied by the environment port.
- [`ouro/plugins/instructions.py`](../../plugins/instructions.py): deployment-owned discovery,
  validation and ordering, shared by builtin and user plugins.

The implementation and manifest are the source of truth for priorities, limits, semantic tags, and
failure behavior.

## Verification

```bash
uv run pytest tests/plugins/test_builtin_agent_context.py -q
```

The verification suite covers source ordering, Git-root behavior, de-duplication, UTF-8 handling,
size limits, runtime facts, and contribution registration.

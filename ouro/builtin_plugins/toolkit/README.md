# Toolkit Built-in Plugin

## Overview

`dev.ouro.toolkit` contributes common workspace search tools and a Session-scoped Todo list through
the standard plugin SDK. It does not register tools through Runtime or CLI bypasses.

## Plugin Metadata

| Field | Value |
|---|---|
| Plugin ID | `dev.ouro.toolkit` |
| Namespace | `toolkit` |
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
| Tool | `glob` / `Glob` | Auto-approved, read-only workspace file discovery |
| Tool | `grep` / `Grep` | Auto-approved, read-only regular-expression content search |
| Tool | `todolist` / `TodoList` | Auto-approved replacement or retrieval of Session Todo state |

Exact model-facing descriptions and JSON schemas are defined in
[`definition.py`](definition.py) and [`toolkit_plugin.py`](toolkit_plugin.py).

## Behavior

### Glob

`Glob` discovers files below an authorized workspace directory. Bare filename patterns such as
`*.py` match recursively; anchored paths and brace expansion are supported. Results contain files,
not directories, and are ordered by most-recent modification time with deterministic tie handling.

The tool respects hierarchical `.gitignore`, `.ignore`, and `.rgignore` rules unless
`include_ignored=true`. It always excludes VCS metadata directories and sensitive `.env` files.
Results are capped at 100 paths and include an explicit truncation marker when the cap is exceeded.

### Grep

`Grep` searches an exact file or authorized directory with ripgrep-compatible regular expressions.
It supports case-insensitive and multiline matching, before/after context, line numbers, glob or
file-type filters, and three output modes:

- `files_with_matches`;
- `content`;
- `count_matches`.

Hidden files are included by default. Ignore and sensitive-file rules match `Glob`. Output defaults
to 250 lines or entries and supports `offset` plus `head_limit` pagination; the framework's tool
output budget remains authoritative when an explicit unlimited result is requested.

### TodoList

Calling `TodoList` without `todos` reads the current list. Supplying `todos` replaces the complete
list atomically, and an empty list clears it. Every item contains a non-empty title and one status:

- `pending`;
- `in_progress`;
- `done`.

The model-facing guidance requires exactly one `in_progress` item while tracked work is underway
and discourages updates when no meaningful progress has occurred. These are usage instructions;
the persisted schema validates titles and status values but does not invent task progress.

## State and Boundaries

- `Glob` and `Grep` resolve paths through `ToolExecutionContext` and cannot escape the authorized
  workspace.
- Search traversal cooperatively checks cancellation and never exposes `.env` or VCS metadata,
  including when ignored files are requested.
- Todo data is canonical Session extension state in the `dev.ouro.toolkit` namespace.
- Todo state uses a versioned envelope, atomic replacement, and `branch_policy="reset"`; an empty
  list deletes the state record.
- Plugin private data does not contain canonical Todo state.
- Search tools are read-only; `TodoList` is state-changing only within its Session state boundary.

## Source Layout

- [`definition.py`](definition.py): built-in definition and manifest.
- [`toolkit_plugin.py`](toolkit_plugin.py): contributions, schemas, handlers, and output rendering.
- [`search.py`](search.py): bounded file discovery and regular-expression search.
- [`ignore.py`](ignore.py): hierarchical ignore rules and sensitive-path exclusions.
- [`todos.py`](todos.py): Todo data model and versioned Session state.

The implementation and manifest are the source of truth for parameters, defaults, limits, output
formats, and error behavior.

## Verification

```bash
uv run pytest tests/plugins/test_builtin_toolkit.py -q
```

Core and Application tests additionally enforce workspace path policy, Session extension-state
semantics, cancellation, and global tool-output limits.

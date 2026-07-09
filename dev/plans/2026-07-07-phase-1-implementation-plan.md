# OURO Agent Phase 1 Implementation Plan

> For agentic workers: implement this plan task-by-task. Use checkbox (`- [ ]`) items for tracking. Do not skip verification steps. Keep implementation aligned with OURO specs, and keep OURO public concepts independent from any external agent framework.

**Date:** 2026-07-07
**Status:** Complete
**Goal:** Implement the minimum usable OURO core with Interactive and Print / JSON modes.

**Specs:**

- `../specs/2026-07-07-phase-1-core-runtime.md`
- `../specs/2026-07-07-local-persistence-and-events.md`
- `../specs/2026-07-07-tools-permissions-hitl.md`
- `../specs/2026-07-07-models-credentials-config.md`
- `../specs/2026-07-07-plugins-mcp-skills.md`
- `../specs/2026-07-07-interactive-print-json.md`

**Architecture:** Build bottom-up: project scaffolding, domain models, config/credentials, provider abstraction, session storage, tools/approval, runtime and hooks, plugin framework, CLI modes, then integration tests.

**Tech Stack:** Python 3.12+, Pydantic v2, pytest, pytest-asyncio, ruff, mypy. First implementation uses asyncio-only core code and defers Rich/Textual and YAML parser until their first concrete consumers.

**Implementation sync, 2026-07-08:**

- Core domain models, local session file storage with recovery and session run locks, session storage protocol boundary, hooks, tool execution, dual-layer runtime loop boundary, assistant tool-call history persistence, provider usage recording, FauxProvider, OpenAI-compatible Provider with multi-provider local configuration, provider management CLI, provider connection-check command, plugin/MCP/Skills framework boundaries, Interactive mode with HITL, suspended-run abort, framework-level ToolExecutionContext cancellation, cooperative active-run abort, active-run guard, queued follow-up input, process-level run locking, and Print / JSON CLI have working implementations.
- Verification is green: `pytest` 74 passed, `ruff check`, `ruff format --check`, `mypy`, and `compileall`.
- Phase 1 intentionally remains product-minimal: richer CLI/session management, true Desktop/TUI polish, full MCP protocol connection, resource management, SDK/RPC, subagents, memory, and browser automation move to later phases.

---

## File Structure

Initial target structure:

```
ouro/
├── pyproject.toml
├── README.md
├── ouro/
│   ├── __init__.py
│   ├── py.typed
│   ├── application/
│   │   ├── __init__.py
│   │   ├── runtime.py
│   │   ├── events.py
│   │   ├── followups.py
│   │   └── session.py
│   ├── core/
│   │   ├── __init__.py
│   │   ├── inner_loop.py
│   │   ├── results.py
│   │   ├── context.py
│   │   ├── hooks.py
│   │   ├── events.py
│   │   ├── messages.py
│   │   ├── state.py
│   │   ├── adapter.py
│   │   └── errors.py
│   ├── providers/
│   │   ├── __init__.py
│   │   ├── base.py
│   │   ├── faux.py
│   │   └── openai_compatible.py
│   ├── sessions/
│   │   ├── __init__.py
│   │   ├── base.py
│   │   └── local_files.py
│   ├── tools/
│   │   ├── __init__.py
│   │   ├── base.py
│   │   ├── registry.py
│   │   ├── executor.py
│   │   └── builtin.py
│   ├── config/
│   │   ├── __init__.py
│   │   ├── service.py
│   │   ├── credentials.py
│   │   └── models.py
│   ├── plugins/
│   │   ├── __init__.py
│   │   ├── manifest.py
│   │   ├── loader.py
│   │   └── hooks.py
│   ├── mcp/
│   │   ├── __init__.py
│   │   └── manager.py
│   ├── skills/
│   │   ├── __init__.py
│   │   └── loader.py
│   └── cli/
│       ├── __init__.py
│       ├── main.py
│       ├── interactive.py
│       └── run.py
└── tests/
    ├── core/
    ├── providers/
    ├── sessions/
    ├── tools/
    ├── config/
    ├── plugins/
    └── cli/
```

The exact structure can change during implementation, but module responsibilities must remain stable.

---

## Task 1: Project Scaffolding

**Files:**

- Create: `ouro/pyproject.toml`
- Create: package directories and `__init__.py` files.
- Create: `ouro/ouro/py.typed`
- Create: `ouro/tests/`

- [x] Initialize Python project metadata.
- [x] Add runtime dependencies: pydantic; use asyncio-only for first pass; defer HTTP client and YAML parser until needed.
- [x] Add dev dependencies: pytest, pytest-asyncio, ruff, mypy.
- [x] Create package structure.
- [x] Configure tests to run from `ouro/`.
- [x] Add minimal README with development commands.
- [x] Verify test collection through the full pytest run.

**Acceptance:**

- Empty package imports successfully.
- Test runner starts and discovers zero or placeholder tests.
- `ouro/dev/` docs remain outside runtime package.

---

## Task 2: Core Domain Models and Event Types

**Specs:** core runtime, local persistence.

**Files:**

- Create: `ouro/ouro/core/messages.py`
- Create: `ouro/ouro/core/events.py`
- Create: `ouro/ouro/core/state.py`
- Create: `ouro/ouro/core/errors.py`
- Create tests under `ouro/tests/core/`

- [x] Define Session, Run, Message, Event, Approval, ToolInvocation, ToolResult models.
- [x] Include schema_version in persistent records.
- [x] Include session_id, run_id, timestamp, sequence where required.
- [x] Define run statuses: queued, running, waiting_user, waiting_approval, completed, aborted, failed, recoverable.
- [x] Define event types for model, tool, approval, AskUser, diagnostics, and run status.
- [x] Add serialization/deserialization tests.
- [x] Add ordering tests for run-local event sequence.

**Acceptance:**

- Models round-trip through JSON.
- Invalid event payloads fail clearly.
- Events can be sorted by run_id + sequence.

---

## Task 3: Config, Credentials, and Model Provider Base

**Specs:** models, credentials, config.

**Files:**

- Create: `ouro/ouro/config/service.py`
- Create: `ouro/ouro/config/credentials.py`
- Create: `ouro/ouro/config/models.py`
- Create: `ouro/ouro/config/providers.py`
- Create: `ouro/ouro/providers/base.py`
- Create: `ouro/ouro/providers/faux.py`
- Create: `ouro/ouro/providers/openai_compatible.py`
- Create tests under `ouro/tests/config/` and `ouro/tests/providers/`

- [x] Implement config layers: runtime, local config file, environment, defaults.
- [x] Implement credential references: env and local inline placeholder.
- [x] Ensure credential references expose redacted display values.
- [x] Define Provider protocol.
- [x] Define Model metadata.
- [x] Implement FauxProvider for deterministic tests.
- [x] Implement OpenAI-compatible Provider first-pass path.
- [x] Wire Faux and OpenAI-compatible Provider options into CLI runtime construction.
- [x] Load provider configuration from `OURO_HOME/config/config.json`, with CLI arguments as overrides.
- [x] Implement multi-provider config management with default provider selection and legacy single-provider config compatibility.
- [x] Implement provider connection-check result model.
- [x] Add tests for config precedence.
- [x] Add tests for missing credentials and redacted display.
- [x] Add tests for OpenAI-compatible text, tool-call, and connection-check mapping.

**Acceptance:**

- A model endpoint can be configured without storing a raw key in session data.
- FauxProvider can stream deterministic text and tool calls.
- Config precedence behaves as specified.

---

## Task 4: Local Session Storage

**Specs:** local persistence and events.

**Files:**

- Create: `ouro/ouro/sessions/base.py`
- Create: `ouro/ouro/sessions/local_files.py`
- Create tests under `ouro/tests/sessions/`

- [x] Implement data root resolution with `OURO_HOME` override.
- [x] Implement session directory creation.
- [x] Write `session.json` atomically.
- [x] Append `messages.jsonl`.
- [x] Append `events.jsonl`.
- [x] Write `runs/<run_id>.json`.
- [x] Maintain rebuildable `sessions/index.json`.
- [x] Define a session storage protocol so Runtime does not depend directly on `LocalSessionStore`.
- [x] Implement initial resource index file.
- [x] Implement load session snapshot.
- [x] Implement recover incomplete run as recoverable or failed.
- [x] Add corruption tests for index rebuild.

**Acceptance:**

- A session survives process restart.
- Events are saved in order.
- Corrupt index does not destroy session data.
- Session list shows title, updated time, status, model, workspace.

---

## Task 5: Tools, Permissions, Approval, AskUser

**Specs:** tools, permissions, HITL.

**Files:**

- Create: `ouro/ouro/tools/base.py`
- Create: `ouro/ouro/tools/registry.py`
- Create: `ouro/ouro/tools/executor.py`
- Create: `ouro/ouro/tools/builtin.py`
- Extend: core state and events as needed.
- Create tests under `ouro/tests/tools/`

- [x] Define ToolDefinition, ToolInvocation, ToolResult.
- [x] Implement ToolRegistry.
- [x] Implement permission modes: auto and approval.
- [x] Implement approval state and approval resolution.
- [x] Implement Read tool.
- [x] Implement Write tool.
- [x] Implement Edit tool with a simple conservative edit strategy.
- [x] Implement Bash tool with timeout and captured stdout/stderr.
- [x] Implement AskUser tool that moves run to waiting_user.
- [x] Ensure refused approval returns a tool_result error, not a runtime crash.
- [x] Add cancellation plumbing for long-running tools.
- [x] Add ToolExecutionContext so plugin tools receive cancellation and execution helpers through the framework.

**Acceptance:**

- Read can run automatically.
- Write/Edit/Bash request approval.
- User can approve, reject, and modify tool arguments.
- AskUser can suspend and resume a run.
- Tool events and tool_result messages persist.

---

## Task 6: Runtime Hooks Pipeline

**Specs:** core runtime, plugins/MCP/skills.

**Files:**

- Create: `ouro/ouro/core/hooks.py`
- Create: `ouro/ouro/plugins/hooks.py`
- Add tests under `ouro/tests/core/test_hooks.py`

- [x] Define hook names and hook context models.
- [x] Define structured hook results.
- [x] Implement hook registration with plugin_id, source, priority.
- [x] Implement deterministic ordering.
- [x] Implement failure policy: record, degrade, isolate.
- [x] Add after_context_build hook support.
- [x] Add before_tool_call and after_tool_call hook support.
- [x] Add on_event hook support.
- [x] Ensure hooks receive controlled views, not mutable runtime internals.
- [x] Add tests for ordering, failure isolation, and context merge.

**Acceptance:**

- A fake plugin can append context through after_context_build.
- A fake plugin can observe or wrap tool result through after_tool_call.
- Hook failure produces event/diagnostic and does not crash core.

---

## Task 7: Runtime Service and Adapter

**Specs:** core runtime.

**Files:**

- Create: `ouro/ouro/application/runtime.py`
- Create: `ouro/ouro/application/events.py`
- Create: `ouro/ouro/application/session.py`
- Create: `ouro/ouro/application/followups.py`
- Create: `ouro/ouro/core/inner_loop.py`
- Create: `ouro/ouro/core/results.py`
- Create: `ouro/ouro/core/context.py`
- Create: `ouro/ouro/core/adapter.py`
- Create runtime service tests under `ouro/tests/application/`
- Create inner-loop tests under `ouro/tests/core/`

- [x] Implement RuntimeService run entrypoint.
- [x] Keep RuntimeService focused on the outer run lifecycle: session loading, active-run guard, locks, aborts, queued follow-ups, HITL resume, and finalization.
- [x] Move RuntimeService and SessionService out of `core` into `application`, with no `core` compatibility shims.
- [x] Extract persistent follow-up input queue into an application component with focused tests.
- [x] Extract runtime event persistence, `on_event` dispatch, and hook failure eventing into an application component with focused tests.
- [x] Implement InnerAgentLoop for provider rounds, stream events, tool-call continuation, and inner-loop failure boundaries.
- [x] Implement SessionService create/open/list.
- [x] Implement ContextBuilder.
- [x] Implement built-in session truncation compaction.
- [x] Implement RuntimeAdapter protocol.
- [x] Wire FauxProvider through adapter for tests.
- [x] Emit run_started, model deltas, tool events, run_completed, run_failed.
- [x] Continue model turns after automatic tool results, approval resolution, and AskUser answers.
- [x] Enforce one active main run per session in-process by returning the existing active run instead of creating a second run.
- [x] Handle queued follow-up input through persistent session metadata and serial drain after active run completion.
- [x] Add process-level session run locking for active runs.
- [x] Implement abort flow for waiting approval and AskUser states.
- [x] Implement cooperative cancellation for active provider streams and long-running tools.

**Acceptance:**

- End-to-end test: user prompt -> model text -> assistant message persisted.
- End-to-end test: model tool call -> approval -> tool result -> next turn.
- End-to-end test: abort leaves session continuable for suspended HITL runs.
- OURO public models do not depend on external agent framework types.

---

## Task 8: Plugin, MCP, and Skills Framework

**Specs:** plugins, MCP, skills.

**Files:**

- Create: `ouro/ouro/plugins/manifest.py`
- Create: `ouro/ouro/plugins/loader.py`
- Create: `ouro/ouro/mcp/manager.py`
- Create: `ouro/ouro/skills/loader.py`
- Create tests under `ouro/tests/plugins/`, `ouro/tests/mcp/`, `ouro/tests/skills/`

- [x] Define plugin manifest model.
- [x] Implement metadata-only discovery.
- [x] Implement enable/disable state.
- [x] Register plugin tools and runtime hooks.
- [x] Ensure registered plugin tools use the same ToolExecutionContext path as built-in tools.
- [x] Register plugin commands and context providers.
- [x] Add plugin load failure isolation.
- [x] Implement MCP server config model.
- [x] Implement MCP lifecycle state machine framework.
- [x] Register MCP tools into ToolRegistry in tests using MCP manager.
- [x] Implement Skills metadata loader.
- [x] Load full skill content only on explicit request.

**Acceptance:**

- A fake official plugin can register a tool and hook.
- A failed plugin does not prevent startup.
- A fake MCP server config can move through configured -> starting -> connected -> stopped.
- Skill metadata loads without injecting full content.

---

## Task 9: Interactive, Print, and JSON Modes

**Specs:** Interactive and Print / JSON.

**Files:**

- Create: `ouro/ouro/cli/main.py`
- Create: `ouro/ouro/cli/interactive.py`
- Create: `ouro/ouro/cli/run.py`
- Create tests under `ouro/tests/cli/`

- [x] Implement `ouro` interactive entrypoint.
- [x] Implement `ouro run --print`.
- [x] Implement `ouro run --json`.
- [x] Implement `ouro config check`.
- [x] Implement `ouro provider list/show/set/use/remove/check`.
- [x] Implement session selection flag.
- [x] Implement non-interactive approval failure behavior.
- [x] Implement Interactive approval handling: approve, reject, and modify tool arguments.
- [x] Implement Interactive AskUser answer handling.
- [x] Implement initial exit codes.
- [x] Implement basic slash commands: /help, /new, /sessions, /open, /model, /config, /export, /abort, /exit.
- [x] Route implemented user actions through RuntimeService.
- [x] Keep stdout clean in Print mode.
- [x] Keep JSON mode machine-readable.

**Acceptance:**

- Interactive mode streams model output.
- Print mode prints final text only to stdout.
- JSON mode outputs valid JSON or JSONL.
- Non-interactive mode fails clearly if approval is required.
- CLI tests use FauxProvider and local temp OURO_HOME.

---

## Task 10: Integration Verification

**Files:**

- Create or update: `ouro/tests/test_phase_1_e2e.py`
- Update docs if needed.

- [x] E2E: new session, prompt, final assistant message.
- [x] E2E: tool approval accepted.
- [x] E2E: tool approval rejected.
- [x] E2E: tool result is fed back into the model and produces a final assistant message.
- [x] E2E: AskUser suspend and resume.
- [x] E2E: abort waiting approval and AskUser runs.
- [x] E2E: continue session after abort.
- [x] E2E: abort active provider stream.
- [x] E2E: abort active approved Bash tool.
- [x] E2E: abort plugin-style tool through ToolExecutionContext.
- [x] E2E: reload persisted session.
- [x] E2E: hook appends context and failure is isolated.
- [x] E2E: Print mode.
- [x] E2E: JSON mode.
- [x] E2E: run mode continues an existing session.
- [x] E2E: Interactive mode handles prompt and basic slash commands.
- [x] E2E: Interactive approval approves and continues run.
- [x] E2E: Interactive approval modifies tool arguments before execution.
- [x] E2E: Interactive AskUser answer continues run.
- [x] E2E: OpenAI-compatible Provider CLI options build runtime without persisting raw keys.
- [x] E2E: local config file can configure OpenAI-compatible Provider.
- [x] E2E: provider management CLI writes default provider config consumed by runtime.
- [x] E2E: assistant tool-call messages are persisted before tool results.
- [x] E2E: provider usage is recorded on runs and JSON output.
- [x] E2E: active waiting run prevents accidental second run in the same session.
- [x] E2E: queued follow-up input is persisted and drained after the active run completes.
- [x] E2E: session run lock prevents active run recovery in another process view.
- [x] E2E: plugin commands and context providers register through plugin framework.
- [x] E2E: provider connection-check command.
- [x] E2E: corrupt session index rebuild and interrupted run recovery.
- [x] Architecture: `core` does not import application, persistence, CLI, or config layers.
- [x] Run all tests.
- [x] Run lint.
- [x] Run type checks.
- [x] Review specs for drift and update plan with conscious tradeoffs.

**Acceptance:**

- Phase 1 acceptance from all first-batch specs is covered by tests or documented as later-phase scope.
- No code path requires Desktop, SDK, or RPC.
- No upper-layer code depends on external agent framework internals.
- Core runtime modules do not expose application service shims or import outer layers.

---

## Final Verification

- [x] `pytest`
- [x] `ruff check`
- [x] `ruff format --check`
- [x] `compileall`
- [x] `mypy`
- [x] Manual run: `ouro`.
- [x] Manual run: `ouro run --print "hello"`.
- [x] Manual run: `ouro run --json "hello"`.
- [x] Manual run: `ouro run --session <session_id> "..."`.
- [x] Kill and restart process; verify session continuation through local files.
- [x] Inspect local data directory; verify messages/events/config are readable.

---

## Open Decisions Before Coding

1. Resolved: start with plain stdin/stdout Interactive, keep Rich/Textual as a later UX upgrade.
2. Use PyYAML or ruamel.yaml for YAML.
3. Resolved: wire FauxProvider first, then add OpenAI-compatible Provider after runtime is green. Current Provider uses non-streaming chat completions first; SSE streaming can extend the same boundary later.
4. Resolved: support env references and inline local credential placeholders, with redacted display.
5. Resolved: first-phase Edit uses conservative exact replace.
6. Resolved: first-phase JSON mode emits final JSON with events; JSONL event stream can be added later.

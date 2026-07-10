# OURO Agent 开发路线图

> For agentic workers: use this roadmap as the top-level orientation. Implement from the phase plans, not directly from this roadmap. Track concrete execution with checkbox tasks in the phase plan documents.

**Date:** 2026-07-07
**Status:** Phase 1 complete
**Related overview:** `../../docs/OURO_AGENT_项目概述_V0707.md`
**Related specs:** `../specs/2026-07-07-ouro-spec-index.md`

---

## Goal

Build OURO Agent as a local-first personal agent runtime. The first usable product should run in Interactive and Print / JSON modes, support local sessions, tool execution, approval, event persistence, configuration, credentials, plugins, MCP integration space, and a replaceable runtime adapter.

The roadmap intentionally avoids implementing Desktop, SDK, RPC, subagents, browser automation, and complex memory in the first phase. Those later features must reuse the same core.

## Design Constraints

- Keep OURO concepts stable and independent from any external agent framework.
- Default session storage is local readable files.
- Complete event streams are saved by default.
- Permissions stay simple: tool auto/approval and plugin read-write/network.
- Plugins integrate through OURO Runtime Hooks, not through lower-level framework hooks.
- Interactive and Print / JSON ship first. Desktop, SDK, and RPC come later.
- Core has built-in session truncation compaction only. Summary compaction is an official plugin.

---

## Phase 0: Planning Baseline

**Status:** Complete.

Deliverables:

- [x] Project overview: `../../docs/OURO_AGENT_项目概述_V0707.md`
- [x] First spec batch: `../specs/`
- [x] Development roadmap: this file
- [x] First implementation plan: `2026-07-07-phase-1-implementation-plan.md`

Exit criteria:

- Specs identify first-phase boundaries.
- Open questions are visible and do not block scaffolding.
- The phase 1 implementation order is clear enough to begin coding.

---

## Phase 1: Minimum Usable Core

**Status:** Complete. Phase 1 now covers project scaffolding, core models, local session file storage with recovery, a session storage protocol boundary, hooks, tools, application/core dual-layer runtime loop boundary, assistant tool-call history persistence, provider usage recording, FauxProvider, OpenAI-compatible Provider streaming, multi-provider local configuration, persisted system prompt configuration, provider/prompt/Skills management CLI, provider connection-check command, plugin/MCP/Skills framework boundaries, native Skills context injection, framework-level ToolExecutionContext for plugin tools, Interactive mode with HITL and streaming text, suspended-run abort, cooperative active provider/tool cancellation, active-run guard, queued follow-up input, process-level session run locking, session continuation, and Print / JSON CLI.

**Primary objective:** OURO can run locally from terminal, call a model, use tools with approval, persist sessions and events, and expose JSON output for scripts.

User-facing deliverables:

- Interactive terminal mode.
- Print final-text mode.
- JSON event/result mode.
- Local session creation and continuation.
- Basic session list with title, status, updated time, model, workspace.
- Basic model configuration and connection check.
- Local multi-provider configuration file with CLI overrides.
- Persisted system prompt configuration with CLI management.
- Provider CLI for list, show, set, use, remove, and check.
- Skills CLI for list, show, roots, and injected prompt preview.
- Read / Write / Edit / Bash / AskUser tools.
- Tool approval, rejection, parameter modification, cancellation.
- Assistant tool-call history preserved before tool results.
- Provider usage recorded on runs and emitted in JSON mode.
- Complete local event persistence.

Core deliverables:

- OURO domain models: Session, Run, Turn, Message, Event, Tool, Approval.
- Runtime service with one-main-run-per-session.
- Runtime Hooks pipeline.
- Context builder with built-in session truncation.
- Provider abstraction and at least one real provider path.
- Test provider for deterministic tests.
- Local session file storage backend.
- Tool registry and execution engine.
- Plugin loader with tool, hook, command, and context-provider registration.
- MCP configuration and lifecycle framework.
- Skills metadata loader and automatic ContextBuilder-native available-skills context injection.
- OURO self-built RuntimeAdapter and provider boundary.

Exit criteria:

- A user can run `ouro`, send a prompt, approve a tool call, and continue the same session later.
- A user can run `ouro run --print "..."` and get only final text on stdout.
- A user can run `ouro run --json "..."` and get machine-readable output.
- A failed or aborted run is visible and recoverable enough for the user to continue.
- Events include session_id, run_id, sequence, event_type, timestamp, payload.
- Tests cover provider, runtime, tools, session storage, hooks, ContextBuilder-native Skills context injection, prompt configuration, and CLI modes.

Primary plan:

- `2026-07-07-phase-1-implementation-plan.md`

---

## Phase 2: Complete Personal Workflow

**Status:** Planned. Specifications complete; implementation not started.

**Primary objective:** Make OURO comfortable for daily personal use.

Planned deliverables:

- Better Interactive experience.
- Session tree, branches, export, replay.
- Session title generation, search, and filtering.
- Multi-session switching and background run state.
- Attachment, generated file, and resource reference management.
- Agent Skills UX.
- Project configuration and workspace trust.
- MCP server lifecycle management UI/commands.
- Official summary compaction plugin.
- Official minimal local memory plugin.
- Official planning/progress plugin.
- Desktop basic application.
- Reserved integration space for subagent official plugin.

Exit criteria:

- Multiple sessions can be navigated and resumed without confusion.
- Resource references survive reload and export.
- MCP servers can be started, stopped, inspected, and repaired from OURO.
- Official plugins use Runtime Hooks without core changes.
- Desktop reuses the same core and can render selected session history plus active stream.

Phase 2 specifications and plan:

- `../specs/2026-07-11-phase-2-personal-workflow.md`
- `../specs/2026-07-11-phase-2-sessions-resources-desktop.md`
- `../specs/2026-07-11-phase-2-extensions-official-capabilities.md`
- `2026-07-11-phase-2-implementation-plan.md`

---

## Phase 3: Advanced Extension

**Primary objective:** Grow OURO into a richer extensible personal automation environment without compromising the phase 1 core.

Planned deliverables:

- SDK and RPC.
- Subagent and exploration official plugin.
- Advanced long-term memory and knowledge retrieval.
- Tool grouping and large tool-set loading.
- Richer tracing, metrics, and offline analysis.
- Data migration and optional SQLite backend.
- Remote execution capability.
- Browser and desktop automation plugins.

Exit criteria:

- SDK/RPC do not fork runtime behavior.
- Subagents are implemented as official plugin capability.
- Optional storage backends satisfy the same session storage contract.
- Advanced tools can be disabled without breaking core operation.

---

## Cross-phase Risks

1. Self-built runtime boundaries drifting

   Mitigation: all upper layers depend on OURO RuntimeAdapter and OURO data models only; provider, tool, session storage, and hook boundaries stay explicit.

2. Event schema instability

   Mitigation: include schema_version early and write event compatibility tests.

3. Plugin hooks becoming too powerful

   Mitigation: hook inputs are controlled views and hook outputs are structured merge requests.

4. Local session file storage corruption

   Mitigation: JSONL append for messages/events, atomic replace for state files, rebuildable indexes.

5. Non-interactive mode hanging on approval

   Mitigation: Print / JSON modes fail clearly when approval is required unless explicit auto-approval is configured.

6. First phase growing into Desktop or SDK work

   Mitigation: keep Desktop, SDK, and RPC as consumers of core contracts, not first-phase deliverables.

---

## Plan Backlog

Create follow-up plans when phase 1 begins to split further:

- Desktop basic application plan.
- Official summary compaction plugin plan.
- Minimal local memory plugin plan.
- MCP lifecycle command/UI plan.
- Session branch and replay plan.
- Resource and attachment management plan.
- SDK/RPC plan.
- Subagent official plugin plan.

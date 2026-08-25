# Changelog

This project records user-visible and framework-contract changes in this file.

## Unreleased

### Changed

- OpenAI-compatible requests now merge the base system prompt and all system-role messages into one
  leading system message after semantic-envelope projection, preserving tagged scaffold content
  while supporting endpoints that reject multiple system messages.
- Context compaction now shares the Host-owned cancellation lifecycle: cancellation is terminal
  across strategy fallback, async and worker callbacks receive task cancellation, cooperative
  in-process handlers receive an SDK cancellation context, and manual compaction reports an
  aborted Run without activating a late summary.
- Provider HTTP transports now re-read environment and operating-system proxy settings for each
  request, reuse connection pools per observed network profile, and fall back from a failed system
  proxy route to the direct network before any response output is emitted.
- Transient provider failures now default to at most five attempts with a one-second initial
  exponential backoff, while retaining the existing eight-second per-delay ceiling.
- The no-command `ouro` entry point now starts the full-screen TUI; the line-oriented Host moved
  to `ouro interactive` and reports the available help, quit, and default-TUI entry points at
  startup. The transitional `ouro tui` command was removed.
- CLI enum arguments now use shell-native hyphenated values, fixed Agent runtime constraints are
  no longer exposed as mutable options, and TUI aliases remain available only when they provide a
  prefix-distinct alternative; the erroneous `/quite` alias was removed.
- All TUI menu pages now share one title, filter, grouped-list, and interaction-footer
  structure. Root help uses the same searchable menu as contextual help, and editable and
  multi-select menus keep state-specific bottom key reminders without duplicating menu chrome.
- Conversation text now consistently permits drag-range selection but rejects full-message
  selection from both double-click and triple-click across historical and live content.
- Live and completed message reasoning labels now use sentence case (`Thinking` / `Thought`).
- TUI conversation chrome now keeps the active workspace right-aligned in the header while
  truncating the Session title first, preserves identical geometry and wrapping when streamed
  assistant blocks freeze into history (including after tool rows), and renders failed Run status
  and failure details with one semantic tone.
- Streaming reasoning now renders through an append-only strip buffer: completed thinking lines
  render exactly once through the same │-bordered definition the immutable history freezes with
  (live and frozen reasoning are now cell-identical), and only the trailing partial line
  re-renders per flush, so long unbounded thinking no longer slows streaming quadratically.
- Decouple LocalApplication, RuntimeSupervisor, AgentService, CLI, and TUI startup from persisted
  Session history. Recovery now runs on the explicitly opened Session and its bounded Agent root;
  primary forks own independent Agent trees even when their lineage root was trashed, while the
  local Session catalog is invalidated in O(1) and rebuilt only on an explicit catalog query.
- All OURO builtin plugins are now enabled by default when no explicit user activation override
  exists. Existing explicit enable/disable choices remain authoritative.
- Plugin trust requirements are now derived once by the Host from runtime, permissions, and
  contributions, projected as explicit `not_required` / `missing` / `granted` application state,
  enforced by activation and selected-version changes, and consumed directly by TUI actions.

### Added

- TUI `/workspace <path>` selection for new-session drafts, with inline history/freeform input,
  immutable created-session workspaces, and a persistent twenty-entry recent-workspace list that
  records each terminal startup directory.
- CLI coverage for Session tag replacement, plugin default permissions, and the complete mutable
  Agent dispatch policy (agent/model allowlists, execution modes, and capability ceilings).
- OpenAI-compatible reasoning decoding now accepts both DeepSeek-style `reasoning_content` and the
  bare `reasoning` response field used by OpenRouter-style vendors, for streaming deltas and
  non-streaming messages alike (string or block-array shapes, `reasoning_content` first), with
  tool-call continuation replay still canonicalized to `reasoning_content`.
- Ten restrained, persistent TUI accent palettes selectable and keyboard-previewable from the
  Actions menu, with live synchronization across CSS chrome and Rich-text status content in a
  frontend-owned preference file.
- Textual 8.2 / Rich 15 full-screen TUI foundation behind the default `ouro` entry point, with
  terminal-native styling,
  coalesced streaming Markdown, canonical Runtime activity/tool projection, session and model
  pickers, queued running input, approvals, AskUser answers, MCP startup status, abort, command
  palette, shortcuts, and explicit application resource cleanup.
- Builtin `dev.ouro.agent_context` plugin with request-time, broad-to-specific `AGENTS.md`
  discovery across user, OURO home, Git-root, and workspace scopes; strict UTF-8 and bounded
  context assembly; and the unchanged runtime-facts injection from the renamed System Information
  builtin.
- Builtin `dev.ouro.goal_mode` plugin with `/goal` create/status/pause/resume/replace/cancel
  lifecycle commands, explicit model create/read/settle tools, durable Goal state, bounded-Run
  continuation, canonical token accounting, optional user-owned budgets, and Plan-mode claim
  arbitration.
- Native OURO plugin system with strict manifests, local/Git/archive installation, immutable
  package storage, separate code-trust and capability grants, declarative/in-process/worker
  runtimes, owner-atomic contributions, plugin Skills/MCP/background services, generation-safe
  durable Run metadata, dependency resolution, compatibility reports, application DTOs, and CLI.
- Typed plugin Run continuation policies with generation-frozen durable checkpoints, deterministic
  conflict/failure pausing, idempotent follow-up queueing, provenance-bearing synthetic inputs,
  permission-gated command input requests, and canonical/unique-alias Interactive slash routing.
- Host-owned plugin tool execution context with workspace/path/cancellation services, plus
  owner-atomic restrictive tool policies for approval escalation and denial across in-process and
  worker runtimes. Policy failures are fail-closed and modified approval arguments are rechecked.
- Global provider-response usage ledger for agent and compaction requests, with date/route/session
  filters, grouped token and partial-cost summaries, audited corrections, selective resets, and CLI.
- Typed application configuration facade shared by CLI hosts.
- Anthropic Messages protocol adapter, provider adapter registry, and reviewed multi-endpoint CLI
  templates for OpenAI, Anthropic, Gemini, DeepSeek, Qwen, KIMI, GLM, OpenRouter, and Mistral.
- Provider retry policy with observable retry events and structured-output capability checks.
- Provider-neutral reasoning intent/capability contracts, declarative native payload profiles,
  custom endpoint presets, and strict pre-request effort validation.
- Session-scoped Interactive `/thinking` controls with Endpoint-aware choices, durable isolation,
  atomic model compatibility checks, and request-local adapter binding.
- Route-scoped reasoning continuation for OpenAI-compatible tool turns and opaque signed
  Anthropic thinking blocks.
- Tool permission modes, workspace path policy, and bounded tool output.
- Tool-controlled `auto`/`deny` permissions for indispensable Harness control tools, isolated from
  user overrides, Session approval modes, and restrictive tool policies.
- Request-local text and image resource projection from canonical Session resource references.
- Interactive next-message attachments with `/attach`, `/attachments`, and `/detach`, reusing the
  canonical resource projection and preserving the draft when endpoint capability validation fails.
- Cursor-based incremental Session message and event queries.
- Cross-platform CI, branch coverage enforcement, and security/contribution guidance.

### Changed

- TUI reasoning blocks now show a live elapsed counter, retain each model call's completed thinking
  time after tool continuations, and append the effective Endpoint plus total duration after every
  Run. Session reload restores these facts from messages and lightweight Run history without
  replaying high-volume text or reasoning delta events.
- The TUI status line now identifies the active model by Endpoint name only; Provider identity
  remains available in model selection surfaces without consuming persistent status space.
- TUI model and approval selectors now remain available during an active Session Run. Model
  changes bind at the next provider request, while approval/AFK policy changes bind at the next
  tool preflight without rewriting an in-flight request or an already-created approval.
- The TUI status line right edge now exclusively presents prepared context usage, follows live
  theme previews, and remains empty until that context data exists; transient details and tool
  counts stay out of this slot.
- TUI context usage now renders as a compact theme-colored progress bar without redundant labels
  or approximation marks, and highlights utilization above 80% with the warning tone.
- Simplified the TUI composer surface by removing its focused-line fill and hiding scrollbar chrome
  while preserving text scrolling.
- Right-clicking an arbitrary TUI page selection now copies its plain text through the system
  clipboard boundary, clears the selection highlight, and shows a themed success notification only
  after a successful copy; Composer right-click paste remains unchanged, and failed copies retain
  the selection for retry.
- Long Plan confirmation documents now scroll independently from the fixed approval controls, so
  actions and keyboard hints remain visible while Page Up/Page Down or the mouse wheel reads the
  Plan; the keyboard-first document viewport hides scrollbar chrome to match other flat TUI panels.
- The TUI now recognizes builtin `ExitPlanMode` input by canonical tool provenance and presents a
  keyboard-first Plan approval workflow instead of raw request JSON. The panel shows only the Plan
  body with preserved line breaks in a scrollable view, provides action and implementation-option
  prompts, and collects structured revision feedback through the Composer.
- TUI notifications now appear in the upper-right with equal top and right insets, the same
  restrained panel surface, single-cell side rails, and theme-aware accent used by OURO's composer
  and menus; warning and error rails retain muted semantic colors without changing the geometry.
- Selecting a Skill from the TUI `/skill` argument menu now always opens an instruction editor
  with a trailing space and inline instruction hint, including Skills that do not declare a custom
  `argument-hint`.
- The TUI queued-input extension now renders queued messages directly without a redundant panel
  title or count, while preserving per-item order and FOLLOW/GUIDE semantics.
- The TUI activity line now places the lowercase effective Session approval preset between activity
  and model, separates status items with bullets, and uses a restrained warning color for automatic
  and unattended AFK operation.
- TUI slash commands now focus on conversation actions: model and explicit Skill selection use the
  inline argument menu, `/skill` creates canonical durable Skill activations, and persistent tool,
  MCP, Skill-management, and general configuration workflows live exclusively under Actions.
- The TUI welcome page now presents System information before its reduced command, action, and
  Session tips; removes the generic prompt and Health row; and uses regular-weight detail labels.
  During an active Run, the Composer placeholder moves follow-up and guidance key hints to the
  point where those actions are relevant, then restores the default prompt when the Run ends.
- Starting `ouro tui` now labels the active terminal tab `OURO` through the standard terminal-title
  control sequence, while non-interactive or unsupported output remains unaffected.
- TUI context usage now follows the latest prepared Provider-context estimate used by `/context`
  and compaction, rather than substituting provider billing usage that may exclude cached input.
- TUI conversations now render through a viewport-sized transcript surface, so short dialogue
  starts at the top while overflowing dialogue retains tail-following and manual scroll behavior.
- New Run extension Catalogs now rebase on the current Host tool-registry revision, so MCP tools
  discovered by background connections reach subsequent model requests while existing Run leases
  keep their original immutable execution contract.
- Runtime URL redaction now treats malformed authorities in untrusted tool output as opaque data,
  so embedded web scripts cannot break observer delivery, disable TUI live events, or fail the
  active tool batch while diagnostics are being projected.
- TUI status and context information now remain anchored at the bottom while queued input extends
  the Composer with matching panel chrome and balanced vertical spacing. Ready uses the active
  theme color, the Session picker omits internal route identities and truncates long titles, and
  `/thinking` completion derives the same Endpoint-aware choices as the action menu.
- Windows terminals now preserve Shift+Enter newlines even when their VT input reports the key as
  plain Enter; the platform-specific normalization remains isolated in the Textual driver adapter.
- Auto-start MCP servers now connect under background owner tasks without delaying Host startup;
  live starting/connected/reconnecting/failed state and atomic tool projection share the Manager's
  single source of truth. The TUI now reconciles that live in-process snapshot and updates connection
  and discovered-tool status without requiring a restart or configuration action; normal connection
  labels use the active TUI theme accent while failures retain their error color.
- Streaming provider HTTP connections no longer apply the request timeout as a passive SSE read
  deadline, so long reasoning gaps do not become false model connection failures.
- Tool execution now enforces `ToolResult` identity at the executor and recovery boundaries, turns
  handler/contract failures into model-readable results, and closes interrupted exchanges from
  failed or aborted Runs before the next model request.
- TUI assistant turns render provider-visible reasoning as plain text and final answers through
  independent Markdown streams. Reasoning appears immediately while the model is still running,
  transitions from `THINKING` to `THOUGHT` when the phase completes, and is restored from canonical
  assistant message history without mixing it into the final answer. Thinking output is rendered
  verbatim instead of being parsed as Markdown, so code fences and markup are shown as-is.
- Tool approval now uses a focus-owning choice panel with a visible selection, arrow-key navigation,
  Enter confirmation, and Y/N shortcuts. The Composer is disabled while approval is pending, so
  approval keystrokes cannot be submitted as follow-up user messages.
- TUI approvals and questions now cover the complete synchronous subagent tree. Descendant
  interactions are projected by their canonical delegation/child/run/request identities and routed
  through AgentService, so a parent in `waiting_child` cannot hide an approval while queued input
  remains pending.
- Parallel tool approvals are now projected as an ordered queue and resolved with their explicit
  Run and approval identifiers. Approving one tool advances directly to the next pending tool
  instead of losing the remaining requests or resolving a different tool than the panel displays.
- TUI conversation rendering now preserves canonical event order across model rounds. Tool rows
  seal the preceding assistant segment, so reasoning or text emitted after tool completion appears
  after that tool row rather than being appended above it.
- TUI submission now gives immediate queued feedback and idempotently reconciles `RunResult.events`
  with the live event sink, avoiding silent responses when observer delivery is delayed. The TUI no
  longer shadows Textual's private `_closing` lifecycle state, and `/` now opens a filterable,
  keyboard-driven command popup backed by the same command catalog as dispatch and help.
- TUI running-session input now uses a durable queue view below the Composer instead of optimistically
  inserting unconsumed text into the conversation. Enter submits a follow-up for the next Run boundary,
  Ctrl+Enter submits guidance for the next model-turn boundary, and canonical message identities place
  consumed input into the transcript in Runtime event order, including after process recovery.
- TUI streaming is now protected by a blocking-provider regression test that observes the first
  Markdown chunk before the Run completes. Built-in AskUser and common plugin input requests now
  share a keyboard-first question form with single-select, multi-select, multi-question, and
  free-form answers; unknown plugin request shapes degrade to bounded free-form prompts.
- Runtime Host-sink workers now register their mailbox state before task creation, so Textual's
  eager asyncio task factory cannot start a consumer against an incomplete registry and silently
  terminate live delivery. Independent bounded mailboxes and sink timeouts remain intact. The TUI
  yields after crossing each event into Textual and renders each available prefix as one coalesced
  Markdown and activity update.
- Builtin command tools now launch the already-selected shell executable directly with an argv
  contract. This removes the redundant platform-shell layer that could leave Windows pipe/process
  ownership unsettled, mark a completed PowerShell command as timed out, and keep its TUI row in
  `running` despite valid output.
- Plugin inventories now list user plugins before ambient builtins. Run generation snapshots remain
  dependency-aware and place user plugins before unrelated builtins, eliminating accidental ordering
  changes when builtin identities are renamed.
- Plugin Session inputs now model producer provenance and model-visible role independently.
  Synthetic remains the default, while commands such as `/goal` creation/replacement can project
  the current explicit user intent as `user` without losing plugin execution-contract attribution.
- Goal mode now gives replacement requests an explicit exception to the unfinished-Goal scaffold,
  requires `UpdateGoal` before terminal responses, and frames continuation after an unclosed Run as
  a nonterminal audit rather than a recap.
- Canonical Session, Run, tool-batch, compaction, billing, resource, configuration, and plugin
  records now cross a strict versioned persistence boundary. Missing, malformed, unsupported, and
  future schema versions fail deterministically instead of being inferred from payload shape.
- Goal, Plan, and Todo state now use the Host-owned, Session-scoped extension-state contract, so
  branch, export, trash, File, and SQLite behavior share the same lifecycle and version checks.
- Run/Session and tool-batch state changes now use shared pure transition rules. Runtime execution
  ownership, terminal settlement, queued-input draining, Agent settlement, and Catalog cleanup have
  one application boundary, and Host extensions bind atomically during composition.
- Shell stdout and stderr now share a hard retention budget at the pipe-consumption boundary while
  excess output continues to drain. Timeout and abort use bounded EOF waits and explicit process-tree
  cleanup.
- Local atomic I/O and file locking now live in shared infrastructure, and the recursive package
  dependency matrix enforces allowed edges across the complete `ouro` package tree.
- Local transport and plugin worker shutdown now close every owned stream, pipe, worker descendant,
  and Windows process-tree terminator explicitly. Resource and unraisable-finalizer warnings fail the
  test suite instead of being accepted as cleanup noise.
- Provider adapters now expose one delta-only tool-call stream contract. Complete non-streaming
  responses are normalized at the adapter boundary; the internal complete `tool_call` event,
  text-only interaction aliases, incomplete legacy plugin tool contracts, and the no-op Toolkit
  `include_dirs` argument were removed.
- Reworked the default Agent system prompt into a compact, capability-neutral contract for user
  intent, autonomous completion, evidence-based verification, open inquiry across sensitive subjects,
  proportionate action-based risk judgment, narrow refusals, and concise communication.
- `CreateGoal` now preserves the exact user request and requires a compact six-field Goal Contract:
  outcome, verification, constraints, boundaries, iteration strategy, and blocked conditions.
  Goal scaffolds, status output, persistence, and terminal audit prompts use that contract without
  introducing a requirements graph or evidence database.
- `CreateGoal` now explicitly forbids autonomous model invocation: only a current user's explicit
  Goal creation or replacement request authorizes the approval-governed tool. `/goal replace`
  delegates through the same tool path with the exact prior Goal identity.
- `/goal <request>` now submits an attributed creation request to the current Session Agent;
  every new Goal receives its identity and controller activation exclusively through the
  approval-governed `CreateGoal` tool instead of a parallel command transition path.
- Interactive plugin commands can declare `requires_session` in their Manifest. The Host now
  enforces that precondition and lazily binds a Session before a first `/goal` or `/plan` command,
  while stateless commands preserve the existing no-Session behavior.
- Run-bound continuation controller leases now survive controller release until settlement, and
  same-controller replace can atomically rotate an activation without allowing stale work to
  affect the replacement.
- Subagent dispatch now uses one model tool: `SpawnAgent(sync)` durably resolves the original
  ToolCall with the child result, while `SpawnAgent(async)` returns a handle and automatically
  delivers the result at the next safe boundary. The model-facing `WaitAgents` tool and configurable
  delivery policy were removed.
- The model-facing `ListAgents` polling tool was removed. Subagent results are pushed to the
  parent automatically, while subagent state remains available to Hosts through the retained
  `AgentService` list/read interfaces for future UI use. `SpawnAgent` and `SendAgentMessage`
  tool results and the async completion delivery are now concise Markdown instead of JSON —
  internal run/session ids stay in the structured payload — and the async results state that
  the subagent proactively pushes a notification when it completes.
- Subagent dispatch no longer has a root active-child quota or a write-capability quota. Every
  validated spawn enters the ordinary RuntimeSupervisor scheduler, whose shared concurrency limit
  and FIFO determine execution; concurrent write failures remain ordinary tool results.
- Interactive terminal input no longer blocks the async runtime. The root interaction inbox routes
  sync and async subagent approvals/questions through the main window, while the terminal renders
  only the primary agent trajectory and keeps subagent replies/tool calls in their own streams.
- Interactive stdin now has one line-reader owner. A newly arrived subagent interaction redirects
  that reader to an explicit answer prompt instead of starting a competing read; blank AskUser
  answers are rejected, and submitted request identities are suppressed until the canonical inbox
  projection catches up.
- Delegation constraints now persist only independent runtime constraints such as workspace and
  external-path access; effective tool references remain the single source for tool, write,
  network, and MCP capability.
- `SpawnAgent` no longer accepts canonical message ids or projects parent history and compaction
  summaries. A child's text context is its frozen AgentDefinition system prompt plus the
  parent-generated task, under the OURO platform instructions.
- Session approval policy now resolves Host-controlled permissions uniformly for built-in, plugin,
  and MCP tools: `inherit` uses the tool mode, while `auto`, `approval`, and `deny` apply globally.
- Trusted plugin tool permissions now use the same semantics as built-ins without source-based
  downgrades; the authorized manifest is authoritative and SDK contributions must match it.
- Removed the legacy 0.1 plugin bridge: root-level `plugin.json` packages are no longer accepted, the
  `PluginLoader`/`LegacyPluginManifest` compatibility layer and the `legacy` flags on install records and
  application DTOs are gone, and queued Session inputs with missing fields are dropped as invalid instead of
  being completed from older shapes.
- Plugin startup now isolates broken desired roots, verifies immutable package bytes before every
  execution, validates credential slots end to end, negotiates the worker protocol without sending
  secrets in JSON-RPC, restores the previous generation after failed activation/configuration, and
  keeps CLI JSON output free of in-process plugin stdout. Repeated enable is generation-idempotent,
  and resumable Runs retain their exact generation against ordinary replacement operations.
- Plugin digest selection now validates the enabled dependency graph atomically; active
  dependencies cannot be disabled out from under dependants, shared data can only be purged with
  the final installed digest, and Host shutdown drains Runtime generation leases before owners.
- Interactive keeps accepting plugin commands, follow-ups, and steering while a plugin-started Run
  chain is active; plugin inputs retain their producer identity and never impersonate user messages.
- The public plugin Hook event set now contains only events with real runtime dispatch; `after_run`
  is observational, while semantic post-Run control uses the dedicated continuation policy API.
- Interactive terminal rendering now uses semantic status adapters: MCP startup and context
  compaction own one replaceable line, while each tool call owns one keyed row that is updated in
  place from argument generation through completion. Tool arguments are visible only while approval
  is pending, and redirected output emits one final row per tool.
- Provider configuration now separates user instance id, product-level provider type, and wire
  protocol; runtime adapter dispatch depends only on the registered protocol.
- Endpoint reasoning configuration now separates activation, effort, visible output, continuation,
  and native encoding. The former boolean reasoning/thinking-level fields are no longer accepted.
- Provider creation now has one `add` entry point: integrated provider types load their complete
  template automatically, while compatible types infer their protocol and start without endpoints.
  The former `configure`, `custom`, and user-selectable `--protocol` paths were removed; id
  conflicts direct users to explicit `update` or `replace` commands.
- The internal Faux provider is omitted from user-facing provider catalogs, defaults, selection,
  and connection checks.
- Configuration, Provider, Skills, and runtime composition CLI paths now use application services
  instead of infrastructure managers.
- Resource content remains immutable and content-addressed; model materialization no longer
  requires embedding large payloads in canonical message journals.

### Fixed

- Follow-up Runs of synchronously dispatched subagents now reach the parent context. Each
  result's delivery policy is chosen at finalization time: the first result keeps the
  spawn-time contract (a parked synchronous SpawnAgent resumes its ToolCall), while later
  follow-up results push at the next safe boundary unless the parent Run is aborted/failed.
  Previously these follow-up results stayed pending forever and never reached the parent.
- Fix a deadlock where an async subagent result delivery held the per-delegation lock while
  waiting for the parent Run it had created: when that Run messaged the same subagent, the
  `SendAgentMessage` call blocked on the same lock and the parent Run never settled, leaving
  the tool call stuck in running. Result finalization now holds the lock only for bounded
  critical sections, delivery routing and Run waits happen outside it, and an in-flight
  guard keeps watcher/reconcile deliveries exactly-once.
- Harden the local store read path against Windows sharing races: reads of atomically
  replaced session, run, delegation, and resource JSON now retry transient
  `PermissionError`/sharing-violation failures within a bounded window, mirroring the
  existing write-side replace tolerance.
- Isolate incompatible persisted Run and Delegation records during startup recovery so one old
  Session cannot prevent OURO from launching. Runtime execution remains strict, while Session
  removal now resolves and atomically trashes the complete root/subagent tree without deserializing
  Run or Delegation journals for scope. Genuinely live Runs receive the canonical abort transition
  before lock acquisition; stale unfinished records are discarded with the tree.
- Scope child-to-delegation lookup to its owning root and remove global Delegation scan ports, so an
  incompatible historical Agent record cannot fail a model turn in a newly created Session.
- Check plugin durable-generation references from Run records only, via a dedicated
  `load_runs` store port, instead of loading complete Session snapshots; plugin lifecycle
  operations no longer read (or fail on) unrelated Session journals.
- Allow Interactive `/model provider/endpoint` before the first prompt and atomically persist that
  selection when the new Session is created.
- Release the Session run lock before draining follow-up inputs after approval or AskUser
  continuation, and keep Interactive alive when a typed continuation error occurs.
- Preserve concurrent Session metadata changes when selecting models or applying Session mutations
  by rereading and saving inside the repository mutation boundary.

## 0.1.0

- Initial local-first agent runtime, Session/Run persistence, tool/HITL loop, Provider adapters,
  MCP client, Skills management, resource storage, compaction, and runtime supervision.

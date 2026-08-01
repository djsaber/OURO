# Changelog

This project records user-visible and framework-contract changes in this file.

## Unreleased

### Added

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

# Changelog

This project records user-visible and framework-contract changes in this file.

## Unreleased

### Added

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
- Hard-deny tool permissions, workspace path policy, and bounded tool output.
- Request-local text and image resource projection from canonical Session resource references.
- Cursor-based incremental Session message and event queries.
- Cross-platform CI, branch coverage enforcement, and security/contribution guidance.

### Changed

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

- Allow Interactive `/model provider/endpoint` before the first prompt and atomically persist that
  selection when the new Session is created.
- Release the Session run lock before draining follow-up inputs after approval or AskUser
  continuation, and keep Interactive alive when a typed continuation error occurs.
- Preserve concurrent Session metadata changes when selecting models or applying Session mutations
  by rereading and saving inside the repository mutation boundary.

## 0.1.0

- Initial local-first agent runtime, Session/Run persistence, tool/HITL loop, Provider adapters,
  MCP client, Skills management, resource storage, compaction, and runtime supervision.

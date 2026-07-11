# OURO module architecture review — 2026-07-11

## Scope

This review covered every production package under `ouro/`, its import graph, public package
exports, large classes/functions, broad exception boundaries, architecture tests, and current
development documentation. The review followed `docs/OURO_AGENT_编码准则_EN.md` and preserved
the in-progress per-session model-selection behavior.

## Findings and changes

### Corrected ownership

1. Model discovery and persisted route resolution lived in `providers/catalog.py`, while catalog
   interfaces and `AvailableModel` lived in core. These concepts exist because a session selects a
   runtime route, so they were moved to `application/models.py`.
2. The model catalog previously imported the concrete provider factory by default. The composition
   root now injects the adapter builder; application no longer depends on provider implementations.
3. `ouro.providers` re-exported core contracts for import convenience. Those exports were removed;
   the package surface now represents only concrete provider adapters and construction.
4. `SessionRunLock` and descriptor-level OS locking lived in the session record store module. They
   moved to `sessions/locks.py`; `local_files.py` retains aggregate persistence and recovery.

### Split mixed interface responsibilities

`cli/interactive.py` combined the host loop, slash commands, event rendering, approval input, and
AskUser input. It is now split into:

- `interactive.py`: host loop and active-session coordination;
- `interactive_commands.py`: command parsing/query formatting;
- `interactive_io.py`: streamed output and suspended-run human interaction.

The split also fixed `/skills` accidentally returning `session_id=None`, which silently discarded
the active interactive session selection.

### Extracted a cohesive runtime collaborator

Tool-call request handling formed a complete unit across before/after hooks, executor invocation,
events, and persisted assistant/tool messages. It moved from `RuntimeService` to
`application/tool_calls.py`. Approval and AskUser resume remain on `RuntimeService` because they
share the outer transaction: session lock, abort registry, model continuation, run hooks, and
follow-up draining.

### Strengthened boundaries

Architecture tests now additionally prove that application does not import CLI/providers and that
the session model catalog is application-owned. No internal import cycles were found after the
refactor.

## Reviewed and intentionally retained

- `RuntimeService` remains a large facade. Its remaining operations share one durable run
  transaction; decomposing by method would create pass-through services and callback cycles.
- `application/compaction.py` remains one module. Summary strategy and context preparation share
  fallback, projection, record, budget, and persistence invariants. There is no independent change
  axis yet.
- `ProviderConfigManager` remains the provider configuration transaction boundary. Supporting
  codecs, data models, endpoint merge rules, secret resolution, and runtime option construction are
  already separated into focused modules.
- The explicit single-method protocols used by `InnerLoopPorts` remain. They document and type-check
  the core loop's effect surface; replacing them with generic callables or empty service objects
  would be less clear.
- `LocalSessionStore` recovery stays beside local persistence. Recovery interprets that backend's
  append-only files and atomic-write guarantees; extracting a generic recovery service would leak
  storage details through a larger interface.
- CLI parser and provider commands remain long but cohesive declarative/command surfaces. File size
  alone is not a reason to introduce a parser package or command-class hierarchy.

## Residual risks and future triggers

- `RuntimeService.run`, manual compaction, and approval continuation are long transactional methods.
  Extract only if a second host/use case needs independently reusable transaction commands or if
  `RuntimeSupervisor` introduces a new ownership boundary.
- `application/compaction.py` should split strategy from preparation only when another strategy
  family or persistence backend evolves independently.
- Provider configuration should gain an application facade before Desktop directly consumes it;
  Desktop must not call `ProviderConfigManager` as a UI data service.
- Several broad exception catches remain at deliberate isolation boundaries (plugins, hooks,
  providers, optional compaction route, interactive rendering). New broad catches require the same
  explicit boundary justification.

## Verification

Final repository gates:

- `pytest`: 259 passed;
- `ruff check`: passed;
- `ruff format --check`: 115 files already formatted;
- `mypy`: no issues in 81 source files (using the repository `.venv`);
- `compileall`: passed;
- `git diff --check`: passed.

# OURO Agent Built-in Summary Compaction

- Date: 2026-07-11
- Status: Accepted / implemented
- Supersedes: the truncation-only and official-summary-plugin decisions in the 2026-07-07
  Phase 1 specifications and the 2026-07-11 Phase 2 extension specification

## 1. Decision

OURO uses LLM-generated summary compaction as its built-in context compaction behavior. The
previous recent-history truncation strategy is removed and is not a fallback. A compaction
failure is visible and prevents an invalid or silently incomplete provider request.

The built-in implementation is selected through the same typed strategy contract that future
built-in and plugin-provided strategies use. Plugins may contribute alternative strategies, but
the host retains planning, validation, persistence, locking, event emission, and final context
budget enforcement.

## 2. Invariants

- Canonical `messages.jsonl` history is append-only and is never deleted or overwritten by
  compaction.
- A completed compaction is stored as a separate immutable record and activated by an explicit
  session pointer.
- Model context is projected as the active summary followed by canonical messages not covered by
  that summary.
- The active projection remains in effect across model rounds, later runs, process restarts, and
  session reloads until a later compaction is activated.
- Later compactions are incremental: they summarize the previous active summary plus newly
  covered canonical messages.
- A compaction cut never splits an assistant tool call from its tool results and never covers the
  latest open user turn.
- Automatic and manual compaction share one application service and differ only by trigger and
  force semantics.
- Summary text is synthetic model context, not a canonical user-visible conversation message.
- Plugins return a proposed artifact. They cannot mutate runtime/session objects or persist and
  activate their own records.

## 3. Layering

### Core

`ouro.core.compaction` owns typed records, strategy contracts, the strategy registry, coverage
planning, lineage validation, and context projection. It performs no provider or filesystem I/O.

`ouro.core.context.ContextBuilder` only composes a final context view, estimates its size, and
reports hard-budget overflow. It does not discard messages.

### Application

`ouro.application.compaction` owns the built-in LLM strategy and compaction coordination. It
invokes the provider through `RuntimeAdapter`, validates strategy output, persists records through
`SessionStore`, activates records, and emits lifecycle events.

The inner loop receives context through an application-provided context-preparation port before
every provider round. This allows tool results produced during a run to trigger compaction without
putting persistence or provider logic into core context construction.

### Persistence

Each session adds `compactions.jsonl`. `SessionRecord.active_compaction_id` identifies the active
immutable record. Record append happens before the session pointer is atomically updated, so a
crash can leave an unreferenced record but cannot publish a missing record.

## 4. Policy

The default automatic trigger is 80% of the model input capacity, where input capacity is the
context window minus the reserved model output tokens. Compaction targets 60% to provide
hysteresis and avoid compacting again on the next turn.

The planner keeps a recent tail and covers only complete older turns. If the fixed prompt/tools,
the generated summary, or the protected latest turn cannot fit the hard input budget, OURO raises
a context-overflow error rather than truncating data.

## 5. Built-in LLM Strategy

The built-in strategy uses `ouro.prompts.SUMMARIZATION_PROMPT`, sends no tools, and bypasses the
normal agent loop so summary generation cannot recursively trigger compaction or create canonical
conversation messages. Long inputs are summarized incrementally in bounded chunks.

The `compaction.model` configuration may select a dedicated `provider_id` and `endpoint_id` and
may override request-scoped temperature, output-token limit, thinking enablement, and thinking
level. These overrides are applied to a copied model view used only by summary requests; the
session runtime adapter and all other model calls remain unchanged.

The session runtime endpoint is the mandatory fallback route. It is used when no dedicated route
is configured, the configured provider/endpoint cannot be resolved or constructed, its context
cannot fit a complete summary chunk, its parameters are incompatible with endpoint capabilities,
or its model request/output fails. The same request-scoped parameter overrides are applied to the
fallback model view. A fallback retries the complete compaction input rather than continuing from
a partial summary produced by a failed route.

Only the `<summary1>...</summary1>` payload is activated. Analysis/reasoning output is not placed
in later model context. The record stores model identity, prompt version/hash, token estimates,
provider usage, selected endpoint, route, and fallback reason when applicable.

Configuration is persisted atomically in the shared `config/config.json` file. Example:

```json
{
  "compaction": {
    "model": {
      "provider_id": "glm",
      "endpoint_id": "summary",
      "temperature": 0.1,
      "max_output_tokens": 4096,
      "thinking_enabled": false,
      "thinking_level": "off"
    }
  }
}
```

`ouro config compaction` inspects this configuration. The same command with `--provider`,
`--endpoint`, and model parameter flags persists updates; `--runtime-endpoint` clears only the
dedicated route and `--clear` clears all compaction model overrides.

## 6. Manual Command

`/compact` invokes the same coordinator with `trigger=manual` and `force=true`. It acquires the
same session operation lock as normal runs, appends no user or assistant message, and returns a
no-op result when no complete older turn is eligible.

## 7. Events and UI

The runtime persists and publishes:

- `compaction_started`
- `compaction_progress`
- `compaction_completed`
- `compaction_failed`
- `compaction_skipped`

Payloads identify automatic/manual trigger, strategy, compaction id, phase, coverage, token
estimates, and error information. UIs recover durable state from the session snapshot and then
follow the event stream; transient events are not the sole state source.

## 8. Plugin Replacement

`CompactionStrategyRegistry` rejects duplicate ids. The default strategy is
`builtin.llm_summary`. Plugin strategies use namespaced ids and become active only through
explicit configuration. An optional fallback may name another summary strategy, but no fallback
may select the removed truncation implementation.

## 9. Acceptance

- Automatic compaction activates a summary before a provider request crosses the configured
  trigger.
- The next model round and next process instance reuse that summary without another summary call.
- A later threshold crossing creates an incremental successor record.
- Manual `/compact` and automatic compaction produce equivalent records and events.
- Canonical messages and history/export views remain unchanged.
- Tool exchanges and the latest open turn are never split or covered.
- Built-in and plugin strategies pass the same host validation.
- Dedicated endpoint success, resolution failure, context overflow, and provider failure all use
  deterministic routing; fallback parameters remain isolated from normal session requests.
- Compaction model routing and parameter overrides survive process restart through `config.json`.
- Compaction failures are persisted, observable, and never silently fall back to truncation.

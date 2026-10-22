# Plan Built-in Plugin

## Overview

`dev.ouro.plan` implements Session-scoped Plan mode entirely through standard plugin
contributions. It does not add a Plan-specific branch to Runtime execution. The plugin owns the mode
lifecycle and planning restrictions while the framework continues to own persistence, tool
execution, approvals, resumable input, and Run scheduling.

## Plugin Metadata

| Field | Value |
|---|---|
| Plugin ID | `dev.ouro.plan` |
| Namespace | `plan` |
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
| Tool | `enter` / `EnterPlanMode` | Auto-approved, state-changing, exclusive-batch acquisition of Plan mode |
| Tool | `exit` / `ExitPlanMode` | Auto-approved, state-changing, exclusive-batch resumable Plan review; supports unattended resolution |
| Command | `/plan` | Enters, leaves, or reports Plan mode for the current Session |
| Session Controller | `mode` | Owns the Plan lifecycle and exclusively claims `ouro.session.primary_behavior` |
| Context Provider | `plan-scaffold` | Injects Plan instructions while the controller is active; failure mode `block` |
| Tool Policy | `plan-tool-gate` | Enforces read-only investigation and restricts writes to the designated Plan file |

Plan mode does not register a Turn Policy. Completion behavior is driven by the injected scaffold,
the tool policy, `ExitPlanMode`, and the framework's resumable tool protocol. Exact contribution
declarations are defined in [`definition.py`](definition.py) and
[`plan_plugin.py`](plan_plugin.py).

## Behavior

### Entering and Leaving Plan Mode

`EnterPlanMode` and `/plan on` acquire the same Session controller. `/plan status` reads the
current phase and Plan-file path. `/plan off` releases the controller and cancels any pending review
through the standard Session-control transition.

Activation creates a unique Markdown artifact under:

```text
<artifact-root>/sessions/<session-id>/plans/plan_<timestamp>_<suffix>.md
```

The Context Provider tells the Agent to investigate, design, verify, write that file, and call
`ExitPlanMode` before implementation. At each model request boundary it appends these instructions
as a persistent user scaffold with the `plan-context` semantic tag. Plan state leaves system
instructions and earlier conversation messages intact.

### Tool Restrictions

While Plan mode is active:

- read-only tools remain available;
- OURO's built-in `Write` and `Edit` tools may modify only the exact designated Plan file;
- all other state-changing or unknown-effect tools are denied;
- approval cannot override the Plan tool policy;
- shell, MCP, plugin, or other indirect write paths cannot be used to modify the Plan.

The policy reads the active controller and canonical target path. Planning restrictions do
not replace workspace policy, tool permissions, or normal execution safety checks.

### Review and Resumption

`ExitPlanMode` requires a readable, non-empty Plan file. It snapshots the Plan digest and may
publish one to three mutually exclusive implementation options. The Host resolves the resumable
request with `accept`, `revise`, or `reject`:

- acceptance releases Plan mode and returns the accepted Plan;
- revision returns feedback and keeps Plan mode active;
- rejection keeps Plan mode active;
- unattended resolution releases Plan mode under the configured AFK policy.

OURO owns the durable interaction checkpoint and committed tool result. Each resume receives a new
call-scoped state view. A changed or missing Plan file invalidates the review; repeated delivery is
handled by the native interaction and message identities.

## State and Boundaries

- Plan business state belongs to the `dev.ouro.plan` owner in the native Harness aggregate.
- The owner payload contains the Plan path (schema 2), with `branch_policy="reset"`.
- Mode activation comes from the native controller projection. Pending review data, digest,
  options and resume identity belong to the native tool interaction.
- Controller transitions, owner state and the corresponding tool result or command effects share
  one durable decision; callbacks only stage changes.
- The Plan Markdown file is a Host-owned artifact; plugin private data does not contain canonical
  Session state.
- Each Run uses its frozen Catalog execution contract. A tool invocation already in progress keeps
  its frozen activation/revision; later model rounds observe updated state.
- Normal review, rejection, AFK resolution, deactivation, and process recovery converge through the
  same resumable checkpoint and Session-controller release path.

## Source Layout

- [`definition.py`](definition.py): built-in definition and manifest.
- [`plan_plugin.py`](plan_plugin.py): tools, command, controller, Context Provider, Tool Policy, and
  resumable review.
- [`state.py`](state.py): versioned Plan state and persistence.
- [`interaction.py`](interaction.py): review options and Host-resolution parsing.

The implementation and manifest are the source of truth for tool descriptions, JSON schemas,
policy decisions, state transitions, and error text.

## Verification

```bash
uv run pytest tests/plugins/test_builtin_plan.py -q
```

Application and Runtime tests additionally cover Session claims, resumable tool recovery, AFK
resolution, frozen execution contracts, and tool-policy composition.

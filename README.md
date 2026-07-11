# OURO Agent

Local-first personal agent runtime.

Current first-phase runtime includes:

- `ouro` interactive stdin/stdout mode.
- `ouro run --print "prompt"` final-text mode, with reasoning deltas shown in dim italics like interactive mode.
- `ouro run --json "prompt"` machine-readable final JSON mode.
- Local readable session storage under `OURO_HOME` or `--home`.
- Session continuation with `ouro run --session <session_id> ...`.
- Interactive `/models` discovery and `/model <provider/endpoint>` switching, persisted per
  session through `current_model` and restored on later runs.
- FauxProvider for deterministic local development.
- Core-owned Provider contracts with outer-layer Faux and OpenAI-compatible implementations.
- Local provider and prompt configuration from `config/config.json`, with CLI overrides.
- Provider management CLI plus endpoint-level configuration for model parameters and capabilities.
- System prompt persistence through `ouro config prompt --set ...`.
- Skills CLI for listing available skills, showing full skill content, viewing search roots, and previewing the injected skills prompt.
- Interrupted local runs can be recovered as `recoverable`; corrupt session indexes are rebuilt.
- Multi-round runtime loop: tool results are fed back into the model until a final assistant response or suspend state.
- Dual-layer loop boundary: application `RuntimeService` orchestrates sessions, locks, aborts, and follow-up draining; application `RunLifecycle` owns model-loop persistence transitions; core `InnerAgentLoop` owns provider rounds and tool-result continuation for one active run.
- Application-owned model routing resolves persisted `provider/endpoint` selections; the `providers` package contains only concrete adapters, transports/codecs, and their construction factory.
- Interactive CLI concerns are separated into the host loop, slash-command handling, and streaming/HITL I/O; session file locking is isolated from session record persistence.
- Assistant tool-call messages are persisted before tool results, preserving OpenAI-compatible tool history.
- Provider usage is recorded on runs and included in JSON output.
- Existing active runs are returned instead of starting a second main run in the same session.
- Follow-up input received during an active run is persisted and processed serially after the current run completes. Queue entries are acknowledged only after their user message is durable, with queue-id deduplication across crash recovery.
- OS-backed session run locks prevent another process view from recovering a live active run as interrupted, while a stale lock file no longer blocks recovery after a process exits.
- Runtime events are persisted through an application event recorder that also isolates hook failures as events.
- Built-in Read / Write / Edit / Bash / AskUser tools.
- Interactive approval flow for approve, reject, modify arguments, AskUser answers, and aborting suspended HITL runs.
- Framework-level `ToolExecutionContext` for plugin tools, including cooperative abort helpers.
- Cooperative abort for active provider streams and long-running tool/subprocess work.
- Runtime Hooks with plugin context injection, failure events, plugin metadata, plugin command/context-provider registration, and MCP lifecycle framework.
- Standards-compatible Agent Skills discovery and native system-prompt injection through `ContextBuilder`; durable built-in LLM summary compaction preserves canonical history and complete tool exchanges.
- Skills use `SKILL.md` YAML frontmatter and are discovered from Ouro-native and common `.agents`, `.claude`, `.github`, `.copilot`, and `.codex` locations; `./skills` remains a legacy workspace search root.
- Runtime depends on a session storage protocol; `LocalSessionStore` is the default implementation.
- Phase 2 session foundation: typed/rebuildable catalog, full user/assistant text search, lineage,
  title state, archive/pin, atomic branch, read-only replay, and delete-to-trash through the shared
  `ApplicationFacade`.
- Session catalog updates are incremental and independently locked; dirty or corrupt indexes
  self-heal without making a canonical session write fail. `layout.json` rejects incompatible data
  versions instead of guessing a migration.

Development commands:

```bash
python -m pytest
python -m ruff check ouro tests
python -m ruff format --check ouro tests
python -m mypy ouro
python -m compileall -q ouro
```

Architecture and development references:

- [`docs/OURO_AGENT_开发架构.md`](docs/OURO_AGENT_开发架构.md) — current module boundaries and dependency rules.
- [`docs/OURO_AGENT_编码准则_EN.md`](docs/OURO_AGENT_编码准则_EN.md) — engineering rules used for implementation and review.
- [`dev/audits/2026-07-11-module-architecture-review.md`](dev/audits/2026-07-11-module-architecture-review.md) — repository-wide architecture review and refactor record.
- [`dev/specs/2026-07-11-session-core-foundation.md`](dev/specs/2026-07-11-session-core-foundation.md) — implemented session core contracts, invariants, and deferred boundaries.

CLI examples:

```bash
python -m ouro.cli.main --home ./.ouro-dev
# In interactive mode: /models, /model, /model <provider/endpoint>
python -m ouro.cli.main run --print --home ./.ouro-dev "hello"
python -m ouro.cli.main run --json --home ./.ouro-dev "hello"
python -m ouro.cli.main run --json --home ./.ouro-dev --session <session_id> "continue"
python -m ouro.cli.main session list --home ./.ouro-dev --search auth --no-archived --sort updated_desc
python -m ouro.cli.main session rename <session_id> "Auth refactor" --home ./.ouro-dev
python -m ouro.cli.main session archive <session_id> --home ./.ouro-dev
python -m ouro.cli.main session branch <session_id> <message_id> --home ./.ouro-dev
python -m ouro.cli.main session replay <session_id> --home ./.ouro-dev --json
python -m ouro.cli.main config check --home ./.ouro-dev
python -m ouro.cli.main config prompt --home ./.ouro-dev --set "Custom system prompt"
python -m ouro.cli.main provider list --home ./.ouro-dev
python -m ouro.cli.main provider set glm --home ./.ouro-dev --kind openai-compatible --endpoint default --model <model> --base-url <url> --api-path <path> --api-key-env <ENV_NAME>
python -m ouro.cli.main provider set glm --home ./.ouro-dev --kind openai-compatible --base-url <url> --api-key-env <ENV_NAME>
python -m ouro.cli.main provider endpoint set glm reasoning --home ./.ouro-dev --model <model> --base-url <url> --temperature 0.2 --context-window 200000 --max-output-tokens 16000 --tools --vision --streaming --reasoning --thinking-levels low high --thinking-level high --default
python -m ouro.cli.main provider use glm --home ./.ouro-dev
python -m ouro.cli.main provider endpoint list glm --home ./.ouro-dev
python -m ouro.cli.main provider show glm --home ./.ouro-dev
python -m ouro.cli.main provider check glm --endpoint reasoning --home ./.ouro-dev

python -m ouro.cli.main config compaction --home ./.ouro-dev --provider glm --endpoint reasoning --temperature 0.1 --no-thinking
python -m ouro.cli.main skills list --home ./.ouro-dev
python -m ouro.cli.main skills show pdf --home ./.ouro-dev
python -m ouro.cli.main skills roots --home ./.ouro-dev
python -m ouro.cli.main skills prompt --home ./.ouro-dev
python -m ouro.cli.main skills validate ./path/to/skill --json
python -m ouro.cli.main skills install ./path/to/skill --home ./.ouro-dev
python -m ouro.cli.main skills install github:owner/repo --skill skill-name --ref v1.0.0
python -m ouro.cli.main skills install https://github.com/owner/repo.git#skills/example
python -m ouro.cli.main skills disable skill-name --home ./.ouro-dev
python -m ouro.cli.main skills list --all --home ./.ouro-dev
python -m ouro.cli.main skills update skill-name --home ./.ouro-dev
python -m ouro.cli.main skills remove skill-name --home ./.ouro-dev
python -m ouro.cli.main run --json --provider <provider_id> --endpoint <endpoint_id> --api-key-env <ENV_NAME> "hello"
```

Example local config:

```json
{
  "prompts": {
    "system": "Custom system prompt"
  },
  "skills": {
    "enabled": true,
    "paths": [
      "shared-skills",
      {"path": ".team/skills", "source": "workspace"}
    ],
    "disabled": ["unsafe-community-skill"],
    "scan_agents": true,
    "scan_claude": true
  },
  "providers": {
    "default": "glm",
    "items": {
      "glm": {
        "provider_type": "openai-compatible",
        "provider_id": "glm",
        "credential_ref": "env:OPENAI_API_KEY",
        "base_url": "http://localhost:8000/v1",
        "api_path": "/chat/completions",
        "default_endpoint": "reasoning",
        "endpoints": {
          "reasoning": {
            "model_id": "model-name",
            "temperature": 0.2,
            "context_window": 200000,
            "max_output_tokens": 16000,
            "supports_tools": true,
            "supports_vision": true,
            "supports_streaming": true,
            "supports_structured_output": false,
            "supports_reasoning": true,
            "supported_thinking_levels": ["low", "high"],
            "default_thinking_level": "high"
          }
        },
        "timeout_seconds": 60,
        "enabled": true
      }
    }
  }
}
```

Each skill is a directory containing `SKILL.md`. Ouro reads the standard `name`,
`description`, `license`, `compatibility`, `metadata`, and experimental `allowed-tools`
frontmatter fields, preserves unknown community fields, and treats bundled files as on-demand
resources. Ouro-specific enablement and search-path policy belongs in the central config above;
per-skill `skill.json` manifests are not used.

`skills install` accepts a local skill/collection or a Git repository. Use `--skill NAME`
to select from a collection, `--all` to install the collection, and `--scope workspace` to
install under `./.ouro/skills`. Ouro validates before copying, rejects symbolic links, deploys
through a same-filesystem staging directory, and records source/update metadata only in the
central config. `skills remove` never deletes skills from external `.agents`, `.claude`,
`.github`, `.copilot`, or `.codex` roots.

`provider_id` is the provider/supplier id; OURO does not use a separate local alias id
in Phase 1. An endpoint is a concrete model/API target owned by that provider. Temperature,
context and output limits, multimodal/tool/stream/reasoning capabilities, and thinking levels
belong to the endpoint rather than the provider. The legacy `provider set --model --base-url`
shortcut updates the default endpoint for compatibility.

Provider-level `base_url` and `api_path` act as defaults for all endpoints of that provider.
Endpoints may omit either field and inherit the provider value; an explicit endpoint value
always wins. `api_path` defaults per provider kind (e.g. `/chat/completions` for
`openai-compatible`).

When using `provider set`, `--endpoint` identifies the default endpoint to create or update;
`--model` is only meaningful when `--endpoint` is provided. Omitting `--endpoint` creates a
provider with no endpoints, which is useful when endpoints will be added later with
`provider endpoint set`.

Creating a provider without an endpoint does not switch the runtime default. Configure its
default endpoint first, then use `provider use PROVIDER_ID`; disabling the active provider or
removing its final endpoint safely falls back to the built-in Faux provider.

Provider configuration always uses the nested `providers.default`, `providers.items`, and
`endpoints` shape. Provider-level `base_url` and `api_path` are defaults for endpoints, not
a shorthand for creating a default endpoint.

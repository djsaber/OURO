# OURO Agent

Local-first personal agent runtime.

Current first-phase runtime includes:

- `ouro` interactive stdin/stdout mode.
- `ouro run --print "prompt"` final-text mode.
- `ouro run --json "prompt"` machine-readable final JSON mode.
- Local readable session storage under `OURO_HOME` or `--home`.
- Session continuation with `ouro run --session <session_id> ...`.
- FauxProvider for deterministic local development.
- First-pass OpenAI-compatible Provider boundary and CLI configuration.
- Local provider configuration from `config/config.json`, with multiple provider entries and CLI overrides.
- Provider management CLI for list, show, set, use, remove, and connection checks.
- Interrupted local runs can be recovered as `recoverable`; corrupt session indexes are rebuilt.
- Multi-round runtime loop: tool results are fed back into the model until a final assistant response or suspend state.
- Dual-layer loop boundary: application `RuntimeService` owns the outer session/run lifecycle, locks, aborts, and follow-up queue; core `InnerAgentLoop` owns provider rounds and tool-result continuation for one active run.
- Assistant tool-call messages are persisted before tool results, preserving OpenAI-compatible tool history.
- Provider usage is recorded on runs and included in JSON output.
- Existing active runs are returned instead of starting a second main run in the same session.
- Follow-up input received during an active run is persisted and processed serially after the current run completes.
- Session run locks prevent another process view from recovering a live active run as interrupted.
- Runtime events are persisted through an application event recorder that also isolates hook failures as events.
- Built-in Read / Write / Edit / Bash / AskUser tools.
- Interactive approval flow for approve, reject, modify arguments, AskUser answers, and aborting suspended HITL runs.
- Framework-level `ToolExecutionContext` for plugin tools, including cooperative abort helpers.
- Cooperative abort for active provider streams and long-running tool/subprocess work.
- Runtime Hooks with context injection and failure events, plugin metadata, plugin command/context-provider registration, MCP lifecycle framework, and Skills metadata loading.
- Runtime depends on a session storage protocol; `LocalSessionStore` is the default implementation.

Development commands:

```bash
python -m pytest
python -m ruff check ouro tests
python -m ruff format --check ouro tests
python -m mypy ouro
python -m compileall -q ouro
```

CLI examples:

```bash
python -m ouro.cli.main --home ./.ouro-dev
python -m ouro.cli.main run --print --home ./.ouro-dev "hello"
python -m ouro.cli.main run --json --home ./.ouro-dev "hello"
python -m ouro.cli.main run --json --home ./.ouro-dev --session <session_id> "continue"
python -m ouro.cli.main config check --home ./.ouro-dev
python -m ouro.cli.main provider list --home ./.ouro-dev
python -m ouro.cli.main provider set glm --home ./.ouro-dev --kind openai-compatible --model <model> --base-url <url> --api-key-env <ENV_NAME> --default
python -m ouro.cli.main provider show glm --home ./.ouro-dev
python -m ouro.cli.main provider check glm --home ./.ouro-dev
python -m ouro.cli.main run --json --provider openai-compatible --base-url <url> --model <model> --api-key-env <ENV_NAME> "hello"
```

Example local config:

```json
{
  "providers": {
    "default": "glm",
    "items": {
      "glm": {
        "provider_type": "openai-compatible",
        "provider_id": "glm",
        "default_model": "model-name",
        "base_url": "http://localhost:8000/v1",
        "credential_ref": "env:OPENAI_API_KEY",
        "timeout_seconds": 60,
        "enabled": true
      }
    }
  }
}
```

`provider_id` is the provider/supplier id; OURO does not use a separate local alias id
in Phase 1.

The legacy single `provider` object is still read for compatibility. New writes use the
`providers.default` plus `providers.items` shape.

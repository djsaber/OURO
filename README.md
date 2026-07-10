# OURO Agent

Local-first personal agent runtime.

Current first-phase runtime includes:

- `ouro` interactive stdin/stdout mode.
- `ouro run --print "prompt"` final-text mode.
- `ouro run --json "prompt"` machine-readable final JSON mode.
- Local readable session storage under `OURO_HOME` or `--home`.
- Session continuation with `ouro run --session <session_id> ...`.
- FauxProvider for deterministic local development.
- Core-owned Provider contracts with outer-layer Faux and OpenAI-compatible implementations.
- Local provider and prompt configuration from `config/config.json`, with CLI overrides.
- Provider management CLI for list, show, set, use, remove, and connection checks.
- System prompt persistence through `ouro config prompt --set ...`.
- Skills CLI for listing available skills, showing full skill content, viewing search roots, and previewing the injected skills prompt.
- Interrupted local runs can be recovered as `recoverable`; corrupt session indexes are rebuilt.
- Multi-round runtime loop: tool results are fed back into the model until a final assistant response or suspend state.
- Dual-layer loop boundary: application `RuntimeService` orchestrates sessions, locks, aborts, and follow-up draining; application `RunLifecycle` owns model-loop persistence transitions; core `InnerAgentLoop` owns provider rounds and tool-result continuation for one active run.
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
- Standards-compatible Agent Skills discovery and progressive context injection through `ContextBuilder`; history truncation preserves assistant tool-call/tool-result boundaries.
- Skills use `SKILL.md` YAML frontmatter and are discovered from Ouro-native and common `.agents`, `.claude`, `.github`, `.copilot`, and `.codex` locations; `./skills` remains a legacy workspace search root.
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
python -m ouro.cli.main config prompt --home ./.ouro-dev --set "Custom system prompt"
python -m ouro.cli.main provider list --home ./.ouro-dev
python -m ouro.cli.main provider set glm --home ./.ouro-dev --kind openai-compatible --model <model> --base-url <url> --api-key-env <ENV_NAME> --default
python -m ouro.cli.main provider show glm --home ./.ouro-dev
python -m ouro.cli.main provider check glm --home ./.ouro-dev
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
python -m ouro.cli.main run --json --provider openai-compatible --base-url <url> --model <model> --api-key-env <ENV_NAME> "hello"
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
in Phase 1.

The legacy single `provider` object is still read for compatibility. New writes use the
`providers.default` plus `providers.items` shape.

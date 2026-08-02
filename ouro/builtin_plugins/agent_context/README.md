# Agent Context builtin

`dev.ouro.agent_context` supplies request-scoped context that is useful across Agent tasks:

- reusable user and project instructions from canonical `AGENTS.md` files;
- the runtime facts formerly supplied by `dev.ouro.system_information`.

Instruction discovery deliberately implements only the product-neutral `AGENTS.md` convention. It
does not treat `CLAUDE.md`, `GEMINI.md`, `MEMORY.md`, `SYSTEM.md`, or `SKILL.md` as aliases because
those names have different authority and lifecycle semantics. Skills remain owned by `ouro.skills`.

For each model request, sources are loaded in this order:

1. `<user-home>/.agents/AGENTS.md`;
2. `OURO_HOME/AGENTS.md`;
3. every `AGENTS.md` from the Git root to the Session workspace.

Later, more specific project documents take semantic precedence but do not erase broader content.
Without a Git root, only the workspace-level project document is considered. Descendant rules are
not scanned eagerly; the injected contract tells the Agent to inspect target ancestors before it
works below the current workspace. Empty documents are ignored, documents must be UTF-8, duplicate
canonical paths are loaded once, and combined document bytes may not exceed 32 KiB.

The two concerns remain separate Context Providers. `agent-instructions` runs before `runtime-facts`,
so `<ouro-system-information>` retains its existing text, request-time refresh behavior, and position
at the end of the complete system prompt. Neither provider creates a canonical Session message.

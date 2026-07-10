# ruff: noqa: E501
"""Reusable OURO prompt templates."""

from collections.abc import Sequence
from html import escape
from typing import Any

DEFAULT_SYSTEM_PROMPT = """You are OURO, a local-first personal agent runtime.

Operate as a careful, capable collaborator. Keep answers concise and useful, make assumptions
explicit, and ask for missing information when proceeding would be risky. Use tools only when they
directly help the task, preserve the user's files and intent, and explain meaningful changes after
you make them. Prefer small, maintainable steps over clever detours.
"""


AGENT_SKILLS_PROMPT_TEMPLATE = """
Agent Skills

<skills_instructions>
The following skills provide specialized instructions for specific tasks.
When a task matches a skill's description, read the full SKILL.md at its listed location before proceeding.
Resolve relative file references against the skill directory, which is the parent directory of SKILL.md.
Load bundled scripts, references, and assets only when the skill instructions require them.
</skills_instructions>

<available_skills>
{{ skills }}
</available_skills>
"""


AGENT_SKILLS_PROMPT_TEMPLATE_KIMI = """
## Skills

Skills are reusable, composable capabilities that enhance your abilities. Each skill is a self-contained directory with a `SKILL.md` file that contains instructions, examples, and/or reference material.

## What are skills?

Skills are modular extensions that provide:

- Specialized knowledge: Domain-specific expertise (e.g., PDF processing, data analysis)
- Workflow patterns: Best practices for common tasks
- Tool integrations: Pre-configured tool chains for specific operations
- Reference material: Documentation, templates, and examples

## Available skills

Skills are grouped by scope (`Project`, `User`, `Extra`, `Built-in`) so you can tell where each came from. When the user refers to "the skill in this project" or "the user-scope skill", use the scope heading to disambiguate. When multiple scopes define a skill with the same name, the more specific scope takes precedence: **Project overrides User overrides Extra overrides Built-in**.

{{ skills }}

## How to use skills

Identify the skills that are likely to be useful for the tasks you are currently working on, read the `SKILL.md` file for detailed instructions, guidelines, scripts and more.

Only read skill details when needed to conserve the context window.
"""


def render_agent_skills_prompt(skills: Sequence[Any]) -> str:
    enabled_skills = [
        skill
        for skill in skills
        if skill.enabled and not getattr(skill, "disable_model_invocation", False)
    ]
    if not enabled_skills:
        return ""
    available_lines: list[str] = []
    for skill in sorted(enabled_skills, key=lambda item: item.name):
        description = getattr(skill, "description", "") or "(no description)"
        available_lines.extend(
            [
                "  <skill>",
                f"    <name>{escape(str(skill.name))}</name>",
                f"    <description>{escape(str(description))}</description>",
                f"    <location>{escape(str(skill.location))}</location>",
                "  </skill>",
            ]
        )
    return AGENT_SKILLS_PROMPT_TEMPLATE.replace("{{ skills }}", "\n".join(available_lines))


def render_agent_skills_prompt_kimi(skills: Sequence[Any]) -> str:
    r"""
    EXAMPLE:

    ### User
    - coding-standards
      - Path: C:\Users\artur\.kimi\skills\coding-standards\SKILL.md
      - Description: Python coding standards and best practices for AI coding agents
    - tooling
      - Path: C:\Users\artur\.kimi\skills\tooling\SKILL.md
      - Description: Python development tooling configuration and best practices

    ### Built-in
    - kimi-cli-help
      - Path: C:\Users\artur\AppData\Roaming\Code\User\globalStorage\moonshot-ai.kimi-code\bin\kimi\_internal\kimi_cli\skills\kimi-cli-help\SKILL.md
      - Description: Answer Kimi Code CLI usage, configuration, and troubleshooting questions. Use when user asks about Kimi Code CLI installation, setup, configuration, slash commands, keyboard shortcuts, MCP integration, providers, environment variables, how something works internally, or any questions about Kimi Code CLI itself.
    - skill-creator
      - Path: C:\Users\artur\AppData\Roaming\Code\User\globalStorage\moonshot-ai.kimi-code\bin\kimi\_internal\kimi_cli\skills\skill-creator\SKILL.md
      - Description: Guide for creating effective skills. This skill should be used when users want to create a new skill (or update an existing skill) that extends Kimi's capabilities with specialized knowledge, workflows, or tool integrations.

    """
    enabled_skills = [
        skill
        for skill in skills
        if skill.enabled and not getattr(skill, "disable_model_invocation", False)
    ]
    if not enabled_skills:
        return ""

    scope_order = {"Project": 0, "User": 1, "Extra": 2, "Built-in": 3}

    def _source_to_scope(source: str) -> str:
        return {
            "workspace": "Project",
            "user": "User",
            "plugin": "Built-in",
        }.get(source, "Extra")

    grouped: dict[str, list[Any]] = {}
    for skill in enabled_skills:
        scope = _source_to_scope(getattr(skill, "source", ""))
        grouped.setdefault(scope, []).append(skill)

    lines: list[str] = []
    for index, scope in enumerate(
        sorted(grouped, key=lambda scope_name: scope_order.get(scope_name, 99))
    ):
        if index > 0:
            lines.append("")
        lines.append(f"### {scope}")
        for skill in sorted(grouped[scope], key=lambda item: item.name):
            description = getattr(skill, "description", "") or "(no description)"
            lines.extend(
                [
                    f"- {skill.name}",
                    f"  - Path: {skill.location}",
                    f"  - Description: {description}",
                ]
            )

    return AGENT_SKILLS_PROMPT_TEMPLATE_KIMI.replace("{{ skills }}", "\n".join(lines))


__all__ = [
    "DEFAULT_SYSTEM_PROMPT",
    "AGENT_SKILLS_PROMPT_TEMPLATE",
    "AGENT_SKILLS_PROMPT_TEMPLATE_KIMI",
    "render_agent_skills_prompt",
    "render_agent_skills_prompt_kimi",
]
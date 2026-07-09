"""Reusable OURO prompt templates."""

from collections.abc import Sequence
from typing import Any

DEFAULT_SYSTEM_PROMPT = """You are OURO Agent, a local-first personal agent runtime.

Operate as a careful, capable collaborator. Keep answers concise and useful, make assumptions
explicit, and ask for missing information when proceeding would be risky. Use tools only when they
directly help the task, preserve the user's files and intent, and explain meaningful changes after
you make them. Prefer small, maintainable steps over clever detours.
"""


AGENT_SKILLS_PROMPT_HEADER = "Execute a skill within the main conversation"


def render_agent_skills_prompt(skills: Sequence[Any]) -> str:
    enabled_skills = [skill for skill in skills if skill.enabled]
    if not enabled_skills:
        return ""
    available_lines = [
        f'"{skill.skill_id}": {_skill_summary(skill)}'
        for skill in sorted(enabled_skills, key=lambda item: item.skill_id)
    ]
    return "\n\n".join(
        [
            AGENT_SKILLS_PROMPT_HEADER,
            "\n".join(
                [
                    "<skills_instructions>",
                    "When users ask you to perform tasks, check if any of the available skills "
                    "below can help complete the task more effectively. Skills provide "
                    "specialized capabilities and domain knowledge.",
                    "",
                    "How to use skills:",
                    "- Invoke skills using this tool with the skill name only (no arguments)",
                    '- When you invoke a skill, you will see <command-message>The "{name}" '
                    "skill is loading</command-message>",
                    "- The skill's prompt will expand and provide detailed instructions on how "
                    "to complete the task",
                    "- Examples:",
                    '  - `command: "pdf"` - invoke the pdf skill',
                    '  - `command: "xlsx"` - invoke the xlsx skill',
                    '  - `command: "ms-office-suite:pdf"` - invoke using fully qualified name',
                    "",
                    "Important:",
                    "- Only use skills listed in <available_skills> below",
                    "- Do not invoke a skill that is already running",
                    "- Do not use this tool for built-in CLI commands (like /help, /clear, etc.)",
                    "</skills_instructions>",
                ]
            ),
            "\n".join(["<available_skills>", *available_lines, "</available_skills>"]),
        ]
    )


def _skill_summary(skill: Any) -> str:
    description = getattr(skill, "description", "")
    if description:
        return str(description)
    return str(getattr(skill, "name", ""))


__all__ = [
    "AGENT_SKILLS_PROMPT_HEADER",
    "DEFAULT_SYSTEM_PROMPT",
    "render_agent_skills_prompt",
]

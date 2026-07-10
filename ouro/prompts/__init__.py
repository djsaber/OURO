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


AGENT_SKILLS_PROMPT_HEADER = "Agent Skills"


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
        available_lines.extend(
            [
                "  <skill>",
                f"    <name>{escape(str(skill.name))}</name>",
                f"    <description>{escape(_skill_summary(skill))}</description>",
                f"    <location>{escape(str(skill.location))}</location>",
                "  </skill>",
            ]
        )
    return "\n\n".join(
        [
            AGENT_SKILLS_PROMPT_HEADER,
            "\n".join(
                [
                    "<skills_instructions>",
                    "The following skills provide specialized instructions for specific tasks.",
                    "When a task matches a skill's description, read the full SKILL.md at its "
                    "listed location before proceeding.",
                    "Resolve relative file references against the skill directory, which is the "
                    "parent directory of SKILL.md.",
                    "Load bundled scripts, references, and assets only when the skill instructions "
                    "require them.",
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

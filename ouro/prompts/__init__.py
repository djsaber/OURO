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


SUMMARIZATION_PROMPT = """
## Primary Prompt

Your task is to create a detailed summary of the conversation to date, paying close attention to the user's explicit requests and your previous actions. This summary must comprehensively capture technical details, code patterns, and architectural decisions essential for continuing development work without losing context.

## Analysis Process

Before delivering the final summary, wrap your analysis in `<analysis>` tags to organize your reasoning and ensure all necessary points are covered. During this process:

Analyze every message and section of the conversation in chronological order. For each section, thoroughly identify:
- The user's explicit requests and intent
- Your approach to handling the request
- Key decisions, technical concepts, and code patterns
- Specific details such as:
  - File names
  - Complete code snippets
  - Function signatures
  - File edits
  - Errors encountered and how you fixed them

Pay special attention to specific user feedback, especially when the user told you to do something differently. Scrutinize technical accuracy and completeness, addressing every required element comprehensively.

## Summary Structure

Your summary must include the following sections:

### 1. Primary Requests and Intent

Capture all explicit user requests and intent in detail.

### 2. Key Technical Concepts

List all important technical concepts, technologies, and frameworks discussed.

### 3. Files and Code Sections

Enumerate specific files and code sections examined, modified, or created. Focus on recent messages; include complete code snippets where applicable, and summarize why reading or editing this file was important.

### 4. Errors and Fixes

List all errors encountered and how you resolved them. Pay special attention to specific user feedback, especially when the user told you to do something differently.

### 5. Problem Resolution

Document resolved issues and any ongoing troubleshooting.

### 6. All User Messages

List all user messages that are not tool results. These are critical for understanding shifts in user feedback and intent.

### 7. Pending Tasks

Outline any pending tasks you were explicitly asked to handle.

### 8. Current Work

Describe in detail the specific work in progress immediately prior to this summary request, with particular attention to the most recent messages from the user and assistant. Include file names and code snippets where applicable.

### 9. Optional Next Steps

List next steps relevant to your most recent work.

**Important:** Ensure this step aligns directly with the user's explicit requests and the tasks you were working on prior to this summary. If your previous task has concluded, only list next steps if they clearly fit the user's request. Do not begin addressing unrelated requests without user confirmation.

If next steps are included, provide verbatim direct quotes from the recent conversation that accurately show what task you were handling and where you stopped. This should be word-for-word to prevent any misinterpretation of the task.

## Output Format Example (XML)

```xml
<analysis>
  [Your thought process, ensuring comprehensive and accurate coverage of all points]
</analysis>

<summary1>
  1. Primary Requests and Intent:
  [Detailed description]

  2. Key Technical Concepts:
  - [Concept 1]
  - [Concept 2]
  - [...]

  3. Files and Code Sections:
  - [File name 1]
  - [Summary of why this file matters]
  - [Summary of changes made to this file, if any]
  - [Important code snippet]
  - [File name 2]
  - [Important code snippet]
  - [...]

  4. Errors and Fixes:
  - [Detailed description of Error 1]:
  - [How you fixed it]
  - [User feedback on the error, if any]
  - [...]

  5. Problem Resolution:
  [Description of resolved issues and ongoing troubleshooting]

  6. All User Messages:
  - [Detailed non-tool user message]
  - [...]

  7. Pending Tasks:
  - [Task 1]
  - [Task 2]
  - [...]

  8. Current Work:
  [Precise description of current work]

  9. Optional Next Steps:
  [Optional next actions]

</summary1>
""".strip()


__all__ = [
    "DEFAULT_SYSTEM_PROMPT",
    "AGENT_SKILLS_PROMPT_TEMPLATE",
    "AGENT_SKILLS_PROMPT_TEMPLATE_KIMI",
    "render_agent_skills_prompt",
    "render_agent_skills_prompt_kimi",
    "SUMMARIZATION_PROMPT",
]

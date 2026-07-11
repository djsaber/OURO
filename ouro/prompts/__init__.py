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
CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.

- Do NOT use Read, Write, Edit, Bash, or ANY other tool.
- You already have all the context you need in the conversation above.
- Tool calls will be REJECTED and will waste your only turn — you will fail the task.
- Your entire response must be plain text: an <analysis> block followed by a <summary> block.

Your task is to create a detailed summary of the conversation so far, paying close attention to the user's explicit requests and your previous actions. This summary should be thorough in capturing technical details, code patterns, and architectural decisions that would be essential for continuing development work without losing context.

Before providing your final summary, wrap your analysis in <analysis> tags to organize your thoughts and ensure you've covered all necessary points. In your analysis process:

1. Chronologically analyze each message and section of the conversation. For each section thoroughly identify:
   - The user's explicit requests and intents
   - Your approach to addressing the user's requests
   - Key decisions, technical concepts and code patterns
   - Specific details like:
     - file names
     - full code snippets
     - function signatures
     - file edits
   - Errors that you ran into and how you fixed them
   - Pay special attention to specific user feedback that you received, especially if the user told you to do something differently.
2. Double-check for technical accuracy and completeness, addressing each required element thoroughly.

Your summary should include the following sections:

1. Primary Request and Intent: Capture all of the user's explicit requests and intents in detail
2. Key Technical Concepts: List all important technical concepts, technologies, and frameworks discussed.
3. Files and Code Sections: Enumerate specific files and code sections examined, modified, or created. Pay special attention to the most recent messages and include full code snippets where applicable and include a summary of why this file read or edit is important.
4. Errors and fixes: List all errors that you ran into, and how you fixed them. Pay special attention to specific user feedback that you received, especially if the user told you to do something differently.
5. Problem Solving: Document problems solved and any ongoing troubleshooting efforts.
6. All user messages: List ALL user messages that are not tool results. These are critical for understanding the users' feedback and changing intent.
7. Pending Tasks: Outline any pending tasks that you have explicitly been asked to work on.
8. Current Work: Describe in detail precisely what was being worked on immediately before this summary request, paying special attention to the most recent messages from both user and assistant. Include file names and code snippets where applicable.
9. Optional Next Step: List the next step that you will take that is related to the most recent work you were doing. IMPORTANT: ensure that this step is DIRECTLY in line with the user's most recent explicit requests, and the task you were working on immediately before this summary request. If your last task was concluded, then only list next steps if they are explicitly in line with the users request. Do not start on tangential requests or really old requests that were already completed without confirming with the user first.
   If there is a next step, include direct quotes from the most recent conversation showing exactly what task you were working on and where you left off. This should be verbatim to ensure there's no drift in task interpretation.

Here's an example of how your output should be structured:

<example>
<analysis>
[Your thought process, ensuring all points are covered thoroughly and accurately]
</analysis>

<summary>
1. Primary Request and Intent:
   [Detailed description]

2. Key Technical Concepts:
   - [Concept 1]
   - [Concept 2]
   - [...]

3. Files and Code Sections:
   - [File Name 1]
      - [Summary of why this file is important]
      - [Summary of the changes made to this file, if any]
      - [Important Code Snippet]
   - [File Name 2]
      - [Important Code Snippet]
   - [...]

4. Errors and fixes:
    - [Detailed description of error 1]:
      - [How you fixed the error]
      - [User feedback on the error if any]
    - [...]

5. Problem Solving:
   [Description of solved problems and ongoing troubleshooting]

6. All user messages:
    - [Detailed non tool use user message]
    - [...]

7. Pending Tasks:
   - [Task 1]
   - [Task 2]
   - [...]

8. Current Work:
   [Precise description of current work]

9. Optional Next Step:
   [Optional Next step to take]

</summary>
</example>

Please provide your summary based on the conversation so far, following this structure and ensuring precision and thoroughness in your response.

There may be additional summarization instructions provided in the included context. If so, remember to follow these instructions when creating your summary. Examples of instructions include:

<example>
## Compact Instructions

When summarizing the conversation focus on typescript code changes and also remember the mistakes you made and how you fixed them.
</example>

<example>
# Summary instructions

When you are using compact - please focus on test output and code changes. Include file reads verbatim.
</example>

REMINDER: Do NOT call any tools. Respond with plain text only — an <analysis> block followed by a <summary> block. Tool calls will be rejected and you will fail the task.
""".strip()


__all__ = [
    "DEFAULT_SYSTEM_PROMPT",
    "AGENT_SKILLS_PROMPT_TEMPLATE",
    "AGENT_SKILLS_PROMPT_TEMPLATE_KIMI",
    "render_agent_skills_prompt",
    "render_agent_skills_prompt_kimi",
    "SUMMARIZATION_PROMPT",
]

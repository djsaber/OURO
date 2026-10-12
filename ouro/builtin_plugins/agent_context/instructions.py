"""Render instruction facts into the Agent Context system-prompt contribution."""

from ouro.sdk.plugins.environment import AgentInstructionDocument


def render_agent_instructions(
    documents: tuple[AgentInstructionDocument, ...],
) -> str:
    sections = [
        "AGENTS.md documents contain reusable instructions from the user. They are additive to "
        "the system prompt. Project instructions apply to the directory tree rooted at their "
        "source directory; when instructions conflict, the later and more specific source takes "
        "precedence. Before working below the current workspace, check for a more specific "
        "AGENTS.md in the target file's ancestor directories."
    ]
    for document in documents:
        sections.append(
            f"Instructions from: {document.path}\nScope: {document.scope}\n\n{document.content}"
        )
    return "\n\n--- agent-instructions-source ---\n\n".join(sections)

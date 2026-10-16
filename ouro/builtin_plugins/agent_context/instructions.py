"""Render instruction facts into the Agent Context system-prompt contribution."""

from ouro.sdk.plugins.environment import AgentInstructionDocument


def render_agent_instructions(
    documents: tuple[AgentInstructionDocument, ...],
) -> str:
    sections = [
        "AGENTS.md files contain the user's working instructions. Follow them within their "
        "scope, subject to higher-priority instructions and the user's current request. "
        "Project instructions apply to files under their source directory; "
        "when these documents conflict, the later and more specific source takes "
        "precedence. Before working below the current workspace, check for a more specific "
        "AGENTS.md in the target file's ancestor directories."
    ]
    for document in documents:
        sections.append(
            f"Instructions from: {document.path}\nScope: {document.scope}\n\n{document.content}"
        )
    return "\n\n---\n\n".join(sections)

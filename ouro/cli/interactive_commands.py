from __future__ import annotations

from dataclasses import dataclass

from ouro.application.runtime import RuntimeService
from ouro.core.errors import PersistenceError
from ouro.skills import discover_available_skills


@dataclass(frozen=True, slots=True)
class CommandResult:
    session_id: str | None
    output: str = ""
    should_exit: bool = False


async def handle_command(
    line: str,
    runtime: RuntimeService,
    session_id: str | None,
) -> CommandResult:
    command, _, argument = line.partition(" ")
    command = command.lower()
    argument = argument.strip()

    if command == "/help":
        return CommandResult(session_id=session_id, output=_help_text())
    if command == "/exit":
        return CommandResult(session_id=session_id, should_exit=True)
    if command == "/new":
        return CommandResult(session_id=None, output="Started a new session.")
    if command == "/sessions":
        return CommandResult(session_id=session_id, output=_sessions_text(runtime))
    if command == "/open":
        return _open_session(runtime, argument)
    if command == "/models":
        return _available_models(runtime, session_id)
    if command == "/model":
        return _model_command(runtime, session_id, argument)
    if command == "/config":
        return CommandResult(session_id=session_id, output=f"OURO_HOME={runtime.store.root}")
    if command == "/export":
        return _export_session(runtime, session_id)
    if command == "/abort":
        return await _abort_session(runtime, session_id)
    if command == "/compact":
        return await _compact_session(runtime, session_id)
    if command == "/history":
        return _session_history(runtime, session_id)
    if command == "/events":
        return _session_events(runtime, session_id)
    if command == "/skills":
        return _available_skills(runtime, session_id)
    return CommandResult(session_id=session_id, output=f"Unknown command: {command}")


def _help_text() -> str:
    return "\n".join(
        [
            "Commands:",
            "/help - Show commands.",
            "/new - Start a new session.",
            "/sessions - List local sessions.",
            "/open <session_id> - Continue a session.",
            "/models - List configured model endpoints.",
            "/model - Show the current session model.",
            "/model <provider/endpoint> - Switch the current session model.",
            "/config - Show active local config path.",
            "/export - Show active session data path.",
            "/history - Show current session messages.",
            "/events - Show current session events.",
            "/skills - List available skills.",
            "/follow <prompt> - Queue at the next run boundary (default).",
            "/steer <prompt> - Queue at the next model-turn boundary.",
            "/abort - Abort the active request when supported.",
            "/compact - Summarize older context for the active session.",
            "/exit - Quit.",
        ]
    )


def _sessions_text(runtime: RuntimeService) -> str:
    sessions = runtime.sessions.catalog().entries
    if not sessions:
        return "No sessions."
    lines = ["Sessions:"]
    for session in sessions:
        marker = "*" if session.status != "idle" else "-"
        title = session.title or "New session"
        lines.append(f"{marker} {session.session_id} {session.status} {title}")
    return "\n".join(lines)


def _available_models(runtime: RuntimeService, session_id: str | None) -> CommandResult:
    models = runtime.available_models()
    if not models:
        return CommandResult(session_id=session_id, output="No configured models available.")
    current = runtime.current_model(session_id)
    lines = ["Available models:"]
    for model in models:
        marker = "*" if model.key == current.key else "-"
        display = f" ({model.display_name})" if model.display_name else ""
        lines.append(f"{marker} {model.key} -> {model.model_id}{display}")
    return CommandResult(session_id=session_id, output="\n".join(lines))


def _model_command(
    runtime: RuntimeService,
    session_id: str | None,
    selector: str,
) -> CommandResult:
    if not selector:
        return CommandResult(session_id=session_id, output=runtime.current_model(session_id).key)
    if session_id is None:
        return CommandResult(
            session_id=None,
            output="No active session. Start a conversation before switching models.",
        )
    runtime.switch_session_model(session_id, selector)
    model = runtime.current_model(session_id)
    return CommandResult(
        session_id=session_id,
        output=f"Switched model: {model.key} ({model.model_id})",
    )


def _open_session(runtime: RuntimeService, session_id: str) -> CommandResult:
    if not session_id:
        return CommandResult(session_id=None, output="Usage: /open <session_id>")
    try:
        snapshot = runtime.sessions.open(session_id)
    except PersistenceError:
        return CommandResult(session_id=None, output=f"Session not found: {session_id}")
    return CommandResult(
        session_id=snapshot.session.session_id,
        output=f"Opened session: {snapshot.session.session_id}",
    )


def _export_session(runtime: RuntimeService, session_id: str | None) -> CommandResult:
    if session_id is None:
        return CommandResult(session_id=None, output="No active session.")
    return CommandResult(
        session_id=session_id,
        output=str(runtime.store.sessions_dir / session_id),
    )


def _session_history(runtime: RuntimeService, session_id: str | None) -> CommandResult:
    if session_id is None:
        return CommandResult(session_id=None, output="No active session.")
    try:
        snapshot = runtime.sessions.open(session_id)
    except PersistenceError:
        return CommandResult(session_id=session_id, output=f"Session not found: {session_id}")
    if not snapshot.messages:
        return CommandResult(session_id=session_id, output="No messages in this session.")
    lines = ["History:"]
    for message in snapshot.messages:
        text = message.text_content.replace("\n", " ")
        lines.append(f"[{message.role}] {text[:120]}")
    return CommandResult(session_id=session_id, output="\n".join(lines))


def _session_events(runtime: RuntimeService, session_id: str | None) -> CommandResult:
    if session_id is None:
        return CommandResult(session_id=None, output="No active session.")
    try:
        snapshot = runtime.sessions.open(session_id)
    except PersistenceError:
        return CommandResult(session_id=session_id, output=f"Session not found: {session_id}")
    if not snapshot.events:
        return CommandResult(session_id=session_id, output="No events in this session.")
    lines = ["Events:"]
    for event in snapshot.events:
        lines.append(f"[{event.event_type}] run={event.run_id} seq={event.sequence}")
    return CommandResult(session_id=session_id, output="\n".join(lines))


def _available_skills(runtime: RuntimeService, session_id: str | None) -> CommandResult:
    try:
        catalog = discover_available_skills(home=runtime.store.root)
    except Exception as exc:  # noqa: BLE001
        return CommandResult(session_id=session_id, output=f"Could not list skills: {exc}")
    if not catalog:
        return CommandResult(session_id=session_id, output="No skills available.")
    lines = ["Available skills:"]
    for skill in sorted(catalog, key=lambda item: item.name):
        state = "enabled" if skill.enabled else "disabled"
        lines.append(f"- {skill.name} [{skill.source}] [{state}] {skill.location}")
    return CommandResult(session_id=session_id, output="\n".join(lines))


async def _abort_session(runtime: RuntimeService, session_id: str | None) -> CommandResult:
    if session_id is None:
        return CommandResult(session_id=None, output="No active session.")
    try:
        result = await runtime.abort_run(session_id=session_id)
    except ValueError:
        return CommandResult(session_id=session_id, output="No active running request.")
    if result.run.status == "aborted":
        return CommandResult(
            session_id=result.session.session_id,
            output=f"Aborted run: {result.run.run_id}",
        )
    return CommandResult(
        session_id=result.session.session_id,
        output="No active running request.",
    )


async def _compact_session(runtime: RuntimeService, session_id: str | None) -> CommandResult:
    if session_id is None:
        return CommandResult(session_id=None, output="No active session.")
    result = await runtime.compact(session_id=session_id)
    if result.skipped or result.compaction is None:
        return CommandResult(
            session_id=result.session.session_id,
            output="No complete older turn is eligible for compaction.",
        )
    return CommandResult(
        session_id=result.session.session_id,
        output=(
            f"Active compaction: {result.compaction.compaction_id} "
            f"({len(result.compaction.covered_message_ids)} messages covered)."
        ),
    )

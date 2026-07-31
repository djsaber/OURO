from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from ouro.application.runtime import RuntimeService
from ouro.cli.context_output import (
    NO_PREPARED_CONTEXT,
    render_context_snapshot,
    render_context_status,
)
from ouro.config import BUILTIN_FAUX_PROVIDER_ID
from ouro.core.approvals import SESSION_APPROVAL_MODES, SessionApprovalMode
from ouro.core.errors import PersistenceError
from ouro.core.interactions import SessionInteractionMode
from ouro.skills import discover_available_skills

if TYPE_CHECKING:
    from ouro.application.plugins import PluginService
    from ouro.core.session_controls import AttributedSessionInput

_APPROVAL_MODE_CHOICES = "|".join(SESSION_APPROVAL_MODES)


@dataclass(frozen=True, slots=True)
class CommandResult:
    session_id: str | None
    output: str = ""
    should_exit: bool = False
    approval_mode: SessionApprovalMode | None = None
    interaction_mode: SessionInteractionMode | None = None
    initial_model: str | None = None
    session_inputs: tuple[AttributedSessionInput, ...] = ()


async def handle_command(
    line: str,
    runtime: RuntimeService,
    session_id: str | None,
    approval_mode: SessionApprovalMode = "inherit",
    initial_model: str | None = None,
    plugins: PluginService | None = None,
    interaction_mode: SessionInteractionMode = "interactive",
) -> CommandResult:
    command, _, argument = line.partition(" ")
    command = command.lower()
    argument = argument.strip()

    if command == "/help":
        return CommandResult(session_id=session_id, output=_help_text(plugins))
    if command == "/exit":
        return CommandResult(session_id=session_id, should_exit=True)
    if command == "/new":
        return CommandResult(
            session_id=None,
            output="Started a new session.",
            approval_mode="inherit",
            interaction_mode="interactive",
            initial_model=runtime.current_model().key,
        )
    if command == "/sessions":
        return CommandResult(session_id=session_id, output=_sessions_text(runtime))
    if command == "/open":
        return _open_session(runtime, argument)
    if command == "/models":
        return _available_models(runtime, session_id, initial_model)
    if command == "/model":
        return _model_command(runtime, session_id, initial_model, argument)
    if command == "/approval-mode":
        return _approval_mode_command(runtime, session_id, approval_mode, argument)
    if command == "/afk":
        return _afk_command(
            runtime,
            session_id,
            approval_mode,
            interaction_mode,
            argument,
        )
    if command == "/config":
        home = runtime.environment.home
        output = f"OURO_HOME={home}" if home is not None else "OURO_HOME is unavailable."
        return CommandResult(session_id=session_id, output=output)
    if command == "/export":
        return _export_session(runtime, session_id)
    if command == "/abort":
        return await _abort_session(runtime, session_id)
    if command == "/compact":
        return await _compact_session(runtime, session_id)
    if command == "/context":
        return _session_context(runtime, session_id, argument)
    if command == "/control":
        return await _session_control(runtime, session_id, argument)
    if command == "/history":
        return _session_history(runtime, session_id)
    if command == "/events":
        return _session_events(runtime, session_id)
    if command == "/skills":
        return _available_skills(runtime, session_id)
    plugin_result = await _plugin_command(
        plugins,
        command.removeprefix("/"),
        argument,
        runtime=runtime,
        session_id=session_id,
        workspace=runtime.default_workspace,
        approval_mode=approval_mode,
        initial_model=initial_model,
        interaction_mode=interaction_mode,
    )
    if plugin_result is not None:
        return plugin_result
    return CommandResult(session_id=session_id, output=f"Unknown command: {command}")


def _help_text(plugins: PluginService | None = None) -> str:
    lines = [
        "Commands:",
        "/help - Show commands.",
        "/new - Start a new session.",
        "/sessions - List local sessions.",
        "/open <session_id> - Continue a session.",
        "/models - List configured model endpoints.",
        "/model - Show the active or next-session model.",
        "/model <provider/endpoint> - Select the active or next-session model.",
        "/approval-mode - Show the current session approval mode.",
        f"/approval-mode <{_APPROVAL_MODE_CHOICES}> - Set the session approval mode.",
        "/afk [on|off] - Show or change the unattended AFK policy preset.",
        "/config - Show active local config path.",
        "/export - Show active session data path.",
        "/history - Show current session messages.",
        "/events - Show current session events.",
        "/skills - List available skills.",
        "/follow <prompt> - Queue at the next run boundary (default).",
        "/steer <prompt> - Queue at the next model-turn boundary.",
        "/abort - Abort the active request when supported.",
        "/compact - Summarize older context for the active session.",
        "/context [full] - Show the latest unified Provider context size or content.",
        "/control [status] - Show active Session controllers.",
        "/control release <activation-id|controller-id> [--force] - Release control.",
        "/control release --all [--force] - Release every active controller.",
        "/exit - Quit.",
    ]
    if plugins is not None:
        commands = plugins.list_commands()
        if commands:
            lines.append("Plugin commands:")
            for item in commands:
                description = f" - {item.description}" if item.description else ""
                if item.usage:
                    lines.append(f"/{item.command_id} {item.usage}{description}")
                else:
                    lines.append(f"/{item.command_id}{description}")
    return "\n".join(lines)


async def _plugin_command(
    plugins: PluginService | None,
    selector: str,
    argument: str,
    *,
    runtime: RuntimeService,
    session_id: str | None,
    workspace: str | None,
    approval_mode: SessionApprovalMode,
    initial_model: str | None,
    interaction_mode: SessionInteractionMode,
) -> CommandResult | None:
    if plugins is None:
        return None
    commands = plugins.list_commands()
    if ":" in selector:
        matches = [item for item in commands if item.command_id == selector]
    else:
        matches = [item for item in commands if item.name == selector]
    if not matches:
        return None
    if len(matches) > 1:
        choices = ", ".join(f"/{item.command_id}" for item in matches)
        return CommandResult(
            session_id=session_id,
            output=f"Ambiguous plugin command /{selector}; use one of: {choices}",
        )
    selected = matches[0]
    target_session_id = session_id
    if target_session_id is None and selected.requires_session:
        created = runtime.create_session(
            title=_plugin_session_title(selected.name, argument),
            initial_model=initial_model,
            approval_mode=approval_mode,
            interaction_mode=interaction_mode,
        )
        target_session_id = created.session_id
    executed = await plugins.execute_command(
        selected.command_id,
        argument,
        session_id=target_session_id,
        workspace=workspace,
    )
    return CommandResult(
        session_id=target_session_id,
        output=executed.output,
        session_inputs=tuple(executed.session_inputs),
    )


def _plugin_session_title(command_name: str, argument: str) -> str:
    normalized = " ".join(argument.split())
    return normalized[:60] if normalized else f"/{command_name}"


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


def _available_models(
    runtime: RuntimeService,
    session_id: str | None,
    initial_model: str | None,
) -> CommandResult:
    models = runtime.available_models()
    if not models:
        return CommandResult(session_id=session_id, output="No configured models available.")
    current = (
        runtime.current_model(session_id)
        if session_id is not None
        else runtime.resolve_model(initial_model or runtime.current_model().key)
    )
    lines = ["Available models:"]
    for model in models:
        marker = "*" if model.key == current.key else "-"
        display = f" ({model.display_name})" if model.display_name else ""
        lines.append(f"{marker} {model.key} -> {model.model_id}{display}")
    return CommandResult(session_id=session_id, output="\n".join(lines))


def _model_command(
    runtime: RuntimeService,
    session_id: str | None,
    initial_model: str | None,
    selector: str,
) -> CommandResult:
    if not selector:
        current = (
            runtime.current_model(session_id)
            if session_id is not None
            else runtime.resolve_model(initial_model or runtime.current_model().key)
        )
        output = (
            "No configured model selected."
            if current.provider_id == BUILTIN_FAUX_PROVIDER_ID
            else current.key
        )
        return CommandResult(
            session_id=session_id,
            output=output,
            initial_model=current.key if session_id is None else None,
        )
    if session_id is None:
        model = runtime.resolve_model(selector)
        return CommandResult(
            session_id=None,
            output=f"Selected model for new session: {model.key} ({model.model_id})",
            initial_model=model.key,
        )
    runtime.switch_session_model(session_id, selector)
    model = runtime.current_model(session_id)
    return CommandResult(
        session_id=session_id,
        output=f"Switched model: {model.key} ({model.model_id})",
    )


def _approval_mode_command(
    runtime: RuntimeService,
    session_id: str | None,
    current_mode: SessionApprovalMode,
    value: str,
) -> CommandResult:
    if not value:
        mode = (
            runtime.sessions.open(session_id, recover=False).session.approval_mode
            if session_id is not None
            else current_mode
        )
        return CommandResult(session_id=session_id, output=mode, approval_mode=mode)
    if value not in SESSION_APPROVAL_MODES:
        return CommandResult(
            session_id=session_id,
            output=f"Usage: /approval-mode <{_APPROVAL_MODE_CHOICES}>",
        )
    mode = cast(SessionApprovalMode, value)
    if session_id is not None:
        runtime.set_session_approval_mode(session_id, mode)
    return CommandResult(
        session_id=session_id,
        output=f"Approval mode: {mode}",
        approval_mode=mode,
    )


def _afk_command(
    runtime: RuntimeService,
    session_id: str | None,
    approval_mode: SessionApprovalMode,
    interaction_mode: SessionInteractionMode,
    value: str,
) -> CommandResult:
    if session_id is not None:
        session = runtime.sessions.open(session_id, recover=False).session
        approval_mode = session.approval_mode
        interaction_mode = session.interaction_mode
    enabled = approval_mode == "auto" and interaction_mode == "unattended"
    if not value:
        return CommandResult(
            session_id=session_id,
            output=f"AFK mode: {'on' if enabled else 'off'}",
            approval_mode=approval_mode,
            interaction_mode=interaction_mode,
        )
    normalized = value.lower()
    if normalized not in {"on", "off"}:
        return CommandResult(session_id=session_id, output="Usage: /afk <on|off>")
    enabled = normalized == "on"
    next_approval: SessionApprovalMode = "auto" if enabled else "inherit"
    next_interaction: SessionInteractionMode = "unattended" if enabled else "interactive"
    if session_id is not None:
        runtime.set_session_afk_mode(session_id, enabled=enabled)
    return CommandResult(
        session_id=session_id,
        output=f"AFK mode: {'on' if enabled else 'off'}",
        approval_mode=next_approval,
        interaction_mode=next_interaction,
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
        approval_mode=snapshot.session.approval_mode,
        interaction_mode=snapshot.session.interaction_mode,
    )


def _export_session(runtime: RuntimeService, session_id: str | None) -> CommandResult:
    if session_id is None:
        return CommandResult(session_id=None, output="No active session.")
    location = runtime.environment.session_location(session_id)
    if location is None:
        return CommandResult(
            session_id=session_id,
            output="This Session backend has no directly accessible data directory.",
        )
    return CommandResult(session_id=session_id, output=str(location))


def _session_history(runtime: RuntimeService, session_id: str | None) -> CommandResult:
    if session_id is None:
        return CommandResult(session_id=None, output="No active session.")
    try:
        page = runtime.sessions.message_page(session_id, limit=100)
    except PersistenceError:
        return CommandResult(session_id=session_id, output=f"Session not found: {session_id}")
    if not page.messages:
        return CommandResult(session_id=session_id, output="No messages in this session.")
    lines = ["History:"]
    for message in page.messages:
        text = message.text_content.replace("\n", " ")
        lines.append(f"[{message.role}] {text[:120]}")
    if page.next_cursor is not None:
        lines.append("History truncated to the first 100 messages.")
    return CommandResult(session_id=session_id, output="\n".join(lines))


def _session_events(runtime: RuntimeService, session_id: str | None) -> CommandResult:
    if session_id is None:
        return CommandResult(session_id=None, output="No active session.")
    try:
        page = runtime.sessions.event_page(session_id, limit=100)
    except PersistenceError:
        return CommandResult(session_id=session_id, output=f"Session not found: {session_id}")
    if not page.events:
        return CommandResult(session_id=session_id, output="No events in this session.")
    lines = ["Events:"]
    for event in page.events:
        lines.append(f"[{event.event_type}] run={event.run_id} seq={event.sequence}")
    if page.next_cursor is not None:
        lines.append("Events truncated to the first 100 records.")
    return CommandResult(session_id=session_id, output="\n".join(lines))


def _available_skills(runtime: RuntimeService, session_id: str | None) -> CommandResult:
    home = runtime.environment.home
    if home is None:
        return CommandResult(
            session_id=session_id,
            output="Skill discovery is unavailable without a deployment home.",
        )
    try:
        catalog = discover_available_skills(home=home)
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


async def _session_control(
    runtime: RuntimeService,
    session_id: str | None,
    argument: str,
) -> CommandResult:
    if session_id is None:
        return CommandResult(session_id=None, output="No active session.")
    parts = argument.split()
    if not parts or parts == ["status"]:
        activations = runtime.session_control_activations(session_id)
        if not activations:
            return CommandResult(
                session_id=session_id,
                output="No active Session controllers.",
            )
        lines = ["Active Session controllers:"]
        for activation in activations:
            claims = ", ".join(f"{claim.domain}:{claim.access}" for claim in activation.claims)
            lines.append(
                f"- {activation.activation_id} {activation.controller_id} "
                f"[{activation.phase}] claims={claims}"
            )
        return CommandResult(session_id=session_id, output="\n".join(lines))
    if parts[0] != "release":
        return CommandResult(
            session_id=session_id,
            output=(
                "Usage: /control [status] | /control release "
                "<activation-id|controller-id|--all> [--force]"
            ),
        )
    force = "--force" in parts[1:]
    selectors = [item for item in parts[1:] if item != "--force"]
    if len(selectors) != 1:
        return CommandResult(
            session_id=session_id,
            output=("Usage: /control release <activation-id|controller-id|--all> [--force]"),
        )
    activations = runtime.session_control_activations(session_id)
    selector = selectors[0]
    if selector == "--all":
        targets = list(activations)
    else:
        targets = [
            activation
            for activation in activations
            if activation.activation_id == selector or activation.controller_id == selector
        ]
    if not targets:
        return CommandResult(
            session_id=session_id,
            output=f"Session controller not found: {selector}",
        )
    if len(targets) > 1 and selector != "--all":
        return CommandResult(
            session_id=session_id,
            output=f"Ambiguous Session controller selector: {selector}",
        )
    outputs: list[str] = []
    for activation in targets:
        try:
            result = await runtime.release_session_control(
                session_id=session_id,
                activation_id=activation.activation_id,
                force=force,
            )
        except RuntimeError as exc:
            outputs.append(f"{activation.controller_id}: {exc}")
        else:
            outputs.append(f"{activation.controller_id}: {result.output}")
    return CommandResult(session_id=session_id, output="\n".join(outputs))


def _session_context(
    runtime: RuntimeService,
    session_id: str | None,
    argument: str,
) -> CommandResult:
    if session_id is None:
        return CommandResult(session_id=None, output="No active session.")
    if argument not in {"", "full"}:
        return CommandResult(
            session_id=session_id,
            output="Usage: /context [full]",
        )
    if argument == "full":
        snapshot = runtime.sessions.context(session_id)
        output = (
            render_context_snapshot(snapshot)
            if snapshot is not None
            else NO_PREPARED_CONTEXT
        )
    else:
        status = runtime.sessions.context_status(session_id)
        output = render_context_status(status) if status is not None else NO_PREPARED_CONTEXT
    return CommandResult(session_id=session_id, output=output)

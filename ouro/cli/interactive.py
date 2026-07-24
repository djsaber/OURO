from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path

from ouro.application.events import RuntimeEventSink
from ouro.application.plugins import PluginService
from ouro.application.queued_inputs import QueuedInputMode
from ouro.application.runtime import RuntimeService
from ouro.cli.interactive_commands import handle_command
from ouro.cli.interactive_io import (
    AgentInteractionHost,
    InputFunc,
    InteractiveOutput,
    OutputFunc,
    PrefetchedInput,
    handle_run_result,
    read_interactive_input,
)
from ouro.cli.run import (
    RuntimeProviderOptions,
    build_default_runtime,
)
from ouro.core.approvals import SessionApprovalMode
from ouro.core.errors import OuroError
from ouro.core.events import EventRecord
from ouro.core.interactions import SessionInteractionMode
from ouro.core.results import RunResult
from ouro.core.session_controls import AttributedSessionInput
from ouro.deployment import SessionBackend
from ouro.local_application import LocalApplication

_CYAN = "\x1b[36m"
_RESET = "\x1b[0m"


async def run_interactive(
    *,
    home: str | Path | None = None,
    provider_options: RuntimeProviderOptions | None = None,
    system_prompt: str | None = None,
    approval_mode: SessionApprovalMode = "inherit",
    interaction_mode: SessionInteractionMode = "interactive",
    session_backend: SessionBackend | None = None,
    runtime: RuntimeService | None = None,
    plugins: PluginService | None = None,
    input_func: InputFunc = input,
    output_func: OutputFunc = print,
) -> int:
    output = InteractiveOutput(output_func)
    owned_application: LocalApplication | None = None
    owns_runtime = runtime is None
    if runtime is None:
        try:
            runtime = build_default_runtime(
                home,
                provider_options=provider_options,
                system_prompt=system_prompt,
                session_backend=session_backend,
            )
            owned_application = LocalApplication.from_runtime(runtime, home=home)
            runtime.add_event_sink(_primary_session_event_sink(runtime, output))
            await output.start_mcp_connection(owned_application.mcp.startup_configs)
            await owned_application.start()
            await output.finish_mcp_connection(owned_application.mcp.startup_records)
            await output.report_startup_diagnostics(owned_application.startup_report)
        except OuroError as exc:
            await output.close()
            if owned_application is not None:
                await owned_application.close()
            elif runtime is not None:
                await runtime.close()
            output_func(f"Error: {exc}")
            return 1
        except BaseException:
            await output.close()
            if owned_application is not None:
                await owned_application.close()
            elif runtime is not None:
                await runtime.close()
            raise
    else:
        runtime.add_event_sink(_primary_session_event_sink(runtime, output))

    try:
        return await _run_interactive_session(
            runtime,
            output=output,
            input_func=input_func,
            output_func=output_func,
            approval_mode=approval_mode,
            interaction_mode=interaction_mode,
            plugins=(owned_application.plugins if owned_application is not None else plugins),
            agents=(owned_application.agents if owned_application is not None else None),
        )
    finally:
        await output.close()
        if owned_application is not None:
            await owned_application.close()
        elif owns_runtime:
            await runtime.close()


async def _run_interactive_session(
    runtime: RuntimeService,
    *,
    output: InteractiveOutput,
    input_func: InputFunc,
    output_func: OutputFunc,
    approval_mode: SessionApprovalMode,
    interaction_mode: SessionInteractionMode,
    plugins: PluginService | None = None,
    agents: AgentInteractionHost | None = None,
) -> int:
    session_id: str | None = None
    current_approval_mode = approval_mode
    current_interaction_mode = interaction_mode
    initial_model = runtime.current_model().key
    plugin_run_task: asyncio.Task[RunResult] | None = None
    output_func("OURO interactive. Type /help for commands, /exit to quit.")

    while True:
        if plugin_run_task is not None and plugin_run_task.done():
            try:
                result = plugin_run_task.result()
                session_id = await handle_run_result(
                    result,
                    runtime=runtime,
                    input_func=input_func,
                    output_func=output_func,
                    output=output,
                    agents=agents,
                )
                current_approval_mode = runtime.store.load_session_record(session_id).approval_mode
                current_interaction_mode = runtime.store.load_session_record(
                    session_id
                ).interaction_mode
            except OuroError as exc:
                output_func(f"Error: {exc}")
            plugin_run_task = None
        try:
            raw_line = await read_interactive_input(
                f"{_CYAN}ouro>{_RESET} ",
                root_session_id=_interaction_root_session_id(runtime, session_id),
                agents=agents,
                input_func=input_func,
                output_func=output_func,
                output=output,
            )
        except EOFError:
            await _stop_plugin_run(runtime, plugin_run_task, session_id)
            output_func("")
            return 0

        if plugin_run_task is not None and plugin_run_task.done():
            try:
                result = plugin_run_task.result()
                consumes_line = result.run.status in {"waiting_approval", "waiting_user"} or (
                    result.run.status == "waiting_child" and agents is not None
                )
                session_id = await handle_run_result(
                    result,
                    runtime=runtime,
                    input_func=(
                        PrefetchedInput(raw_line, input_func) if consumes_line else input_func
                    ),
                    output_func=output_func,
                    output=output,
                    agents=agents,
                )
                current_approval_mode = runtime.store.load_session_record(session_id).approval_mode
                current_interaction_mode = runtime.store.load_session_record(
                    session_id
                ).interaction_mode
                plugin_run_task = None
                if consumes_line:
                    continue
            except OuroError as exc:
                output_func(f"Error: {exc}")
                plugin_run_task = None

        line = raw_line.strip()
        if not line:
            continue

        queue_mode: QueuedInputMode = "follow_up"
        if line.startswith("/steer "):
            queue_mode = "steer"
            line = line.removeprefix("/steer ").strip()
        elif line.startswith("/follow "):
            line = line.removeprefix("/follow ").strip()

        if line.startswith("/"):
            try:
                command_result = await handle_command(
                    line,
                    runtime,
                    session_id,
                    current_approval_mode,
                    initial_model,
                    plugins,
                    interaction_mode=current_interaction_mode,
                )
            except OuroError as exc:
                output_func(f"Error: {exc}")
                continue
            session_id = command_result.session_id
            if command_result.approval_mode is not None:
                current_approval_mode = command_result.approval_mode
            if command_result.interaction_mode is not None:
                current_interaction_mode = command_result.interaction_mode
            if command_result.initial_model is not None:
                initial_model = command_result.initial_model
            if command_result.output:
                output_func(command_result.output)
            if command_result.should_exit:
                await _stop_plugin_run(runtime, plugin_run_task, session_id)
                return 0
            if command_result.session_inputs:
                if session_id is None:
                    first = command_result.session_inputs[0]
                    created = runtime.create_session(
                        title=first.title or _interactive_title(first.prompt),
                        initial_model=initial_model,
                        approval_mode=current_approval_mode,
                        interaction_mode=current_interaction_mode,
                    )
                    session_id = created.session_id
                if plugin_run_task is None:
                    plugin_run_task = asyncio.create_task(
                        _submit_plugin_inputs(
                            runtime,
                            command_result.session_inputs,
                            session_id,
                        ),
                        name=f"ouro-plugin-input-{session_id}",
                    )
                else:
                    for session_input in command_result.session_inputs:
                        await runtime.submit_plugin_input(
                            session_input,
                            session_id=session_id,
                        )
            continue

        try:
            await output.start_waiting()
            result = await runtime.run(
                line,
                session_id=session_id,
                initial_model=initial_model if session_id is None else None,
                queue_mode=queue_mode,
                approval_mode=current_approval_mode if session_id is None else None,
                interaction_mode=current_interaction_mode if session_id is None else None,
            )
            # Preserve the durable Session identity even when approval or AskUser
            # continuation fails after the initial RunResult has been returned.
            session_id = result.session.session_id
            if plugin_run_task is not None and any(
                event.event_type in {"follow_up_input_queued", "steering_input_queued"}
                for event in result.events
            ):
                continue
            session_id = await handle_run_result(
                result,
                runtime=runtime,
                input_func=input_func,
                output_func=output_func,
                output=output,
                agents=agents,
            )
            current_approval_mode = runtime.store.load_session_record(session_id).approval_mode
            current_interaction_mode = runtime.store.load_session_record(
                session_id
            ).interaction_mode
        except OuroError as exc:
            await output.close()
            output_func(f"Error: {exc}")
            continue


async def _submit_plugin_inputs(
    runtime: RuntimeService,
    session_inputs: tuple[AttributedSessionInput, ...],
    session_id: str,
) -> RunResult:
    result: RunResult | None = None
    for session_input in session_inputs:
        result = await runtime.submit_plugin_input(session_input, session_id=session_id)
    assert result is not None
    return result


async def _stop_plugin_run(
    runtime: RuntimeService,
    task: asyncio.Task[RunResult] | None,
    session_id: str | None,
) -> None:
    if task is None:
        return
    if session_id is not None:
        for _ in range(3):
            if task.done():
                break
            with suppress(ValueError):
                await runtime.abort_run(session_id=session_id)
            await asyncio.sleep(0)
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
    except TimeoutError:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    except Exception:
        return


def _interactive_title(prompt: str) -> str:
    normalized = " ".join(prompt.split())
    return normalized[:60] if normalized else "New session"


def _interaction_root_session_id(
    runtime: RuntimeService,
    session_id: str | None,
) -> str | None:
    if session_id is None:
        return None
    session = runtime.store.load_session_record(session_id)
    return session.root_session_id or session.session_id


def _primary_session_event_sink(
    runtime: RuntimeService,
    output: InteractiveOutput,
) -> RuntimeEventSink:
    """Limit the terminal transcript to primary Session execution events."""

    visible_sessions: dict[str, bool] = {}

    async def handle_event(event: EventRecord) -> None:
        visible = visible_sessions.get(event.session_id)
        if visible is None:
            session = runtime.store.load_session_record(event.session_id)
            visible = session.session_kind == "primary"
            visible_sessions[event.session_id] = visible
        if visible:
            await output.handle_event(event)

    return handle_event

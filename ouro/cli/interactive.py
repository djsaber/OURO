from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path

from ouro.application.events import RuntimeEventSink
from ouro.application.plugins import PluginService
from ouro.application.queued_inputs import QueuedInputMode
from ouro.application.runtime import RuntimeService
from ouro.application.session_models import SessionUndoResult
from ouro.application.skill_activations import SkillInvocationRequest
from ouro.application.supervisor_models import HostInputReceipt
from ouro.cli.interactive_attachments import InteractiveAttachmentDraft
from ouro.cli.interactive_commands import handle_command
from ouro.cli.interactive_input import InputFunc, PromptFunc, write_terminal_prompt
from ouro.cli.interactive_io import (
    AgentInteractionHost,
    InteractionCoordinator,
    InteractiveOutput,
    OutputFunc,
)
from ouro.cli.run import (
    RuntimeProviderOptions,
    build_default_runtime,
)
from ouro.core.approvals import SessionApprovalMode
from ouro.core.errors import OuroError
from ouro.core.events import EventRecord
from ouro.core.interactions import SessionInteractionMode
from ouro.core.messages import SkillActivationContent
from ouro.core.reasoning import ReasoningRequest
from ouro.core.results import RunResult
from ouro.core.session_controls import AttributedSessionInput, AttributedSessionInputBatch
from ouro.deployment import SessionBackend, TaskBackend
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
    task_backend: TaskBackend | None = None,
    runtime: RuntimeService | None = None,
    plugins: PluginService | None = None,
    input_func: InputFunc = input,
    output_func: OutputFunc = print,
    prompt_func: PromptFunc | None = None,
) -> int:
    output = InteractiveOutput(output_func)
    resolved_prompt_func = prompt_func or (
        write_terminal_prompt if input_func is input and output_func is print else output_func
    )
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
            owned_application = LocalApplication.from_runtime(
                runtime, home=home, task_backend=task_backend
            )
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
            prompt_func=resolved_prompt_func,
            output_func=output_func,
            approval_mode=approval_mode,
            interaction_mode=interaction_mode,
            plugins=(owned_application.plugins if owned_application is not None else plugins),
            agents=(owned_application.agents if owned_application is not None else None),
        )
    finally:
        await runtime.drain_events()
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
    prompt_func: PromptFunc,
    output_func: OutputFunc,
    approval_mode: SessionApprovalMode,
    interaction_mode: SessionInteractionMode,
    plugins: PluginService | None = None,
    agents: AgentInteractionHost | None = None,
) -> int:
    session_id: str | None = None
    current_approval_mode = approval_mode
    current_interaction_mode = interaction_mode
    current_reasoning_request = ReasoningRequest()
    initial_model = runtime.current_model().key
    plugin_run_task: asyncio.Task[RunResult] | None = None
    attachments = InteractiveAttachmentDraft()
    restored_input: SessionUndoResult | None = None
    coordinator = InteractionCoordinator(
        runtime=runtime,
        output=output,
        input_func=input_func,
        output_func=output_func,
        prompt_func=prompt_func,
        agents=agents,
    )
    output_func(
        "OURO interactive mode. Type /help for commands or /quit to exit. "
        "Run 'ouro' without a command to use the TUI."
    )

    try:
        while True:
            if plugin_run_task is not None and plugin_run_task.done():
                try:
                    result = plugin_run_task.result()
                    session_id = await coordinator.resolve_run(result)
                    (
                        current_approval_mode,
                        current_interaction_mode,
                        current_reasoning_request,
                    ) = _session_preferences(
                        runtime,
                        session_id,
                    )
                except OuroError as exc:
                    await _report_runtime_error(runtime, output, output_func, exc)
                plugin_run_task = None
            try:
                raw_line = await coordinator.read_prompt(
                    _interactive_prompt(attachments.count),
                    root_session_id=_interaction_root_session_id(runtime, session_id),
                )
            except EOFError:
                await _stop_plugin_run(runtime, plugin_run_task, session_id)
                output_func("")
                return 0
            except OuroError as exc:
                await _report_runtime_error(runtime, output, output_func, exc)
                continue

            if plugin_run_task is not None and plugin_run_task.done():
                try:
                    result = plugin_run_task.result()
                    consumes_line = result.run.status in {"waiting_approval", "waiting_user"} or (
                        result.run.status == "waiting_child" and agents is not None
                    )
                    session_id = await coordinator.resolve_run(
                        result,
                        prefetched_line=raw_line if consumes_line else None,
                    )
                    (
                        current_approval_mode,
                        current_interaction_mode,
                        current_reasoning_request,
                    ) = _session_preferences(
                        runtime,
                        session_id,
                    )
                    plugin_run_task = None
                    if consumes_line:
                        continue
                except OuroError as exc:
                    await _report_runtime_error(runtime, output, output_func, exc)
                    plugin_run_task = None

            line = raw_line.strip()
            if not line and restored_input is None:
                continue

            queue_mode: QueuedInputMode = "follow_up"
            if line.startswith("/steer "):
                queue_mode = "steer"
                line = line.removeprefix("/steer ").strip()
            elif line.startswith("/follow "):
                line = line.removeprefix("/follow ").strip()

            skill_invocation = None
            if restored_input is not None and not line.startswith("/"):
                line = line or restored_input.text_content
                activation = next(
                    (
                        block
                        for block in restored_input.input.content
                        if isinstance(block, SkillActivationContent)
                    ),
                    None,
                )
                if activation is not None:
                    skill_invocation = SkillInvocationRequest(name=activation.name, arguments=line)
            if line.startswith("/"):
                try:
                    previous_session_id = session_id
                    command_result = await handle_command(
                        line,
                        runtime,
                        session_id,
                        current_approval_mode,
                        initial_model,
                        plugins,
                        reasoning_request=current_reasoning_request,
                        interaction_mode=current_interaction_mode,
                        attachments=attachments,
                        queue_mode=queue_mode,
                    )
                except OuroError as exc:
                    await _report_runtime_error(runtime, output, output_func, exc)
                    continue
                session_id = command_result.session_id
                if session_id != previous_session_id:
                    attachments.retain_for_session(session_id)
                    restored_input = None
                if command_result.restored_input is not None:
                    restored_input = command_result.restored_input
                    (
                        current_approval_mode,
                        current_interaction_mode,
                        current_reasoning_request,
                    ) = _session_preferences(runtime, restored_input.session.session_id)
                if command_result.run_result is not None:
                    session_id = await coordinator.resolve_run(command_result.run_result)
                if command_result.approval_mode is not None:
                    current_approval_mode = command_result.approval_mode
                if command_result.interaction_mode is not None:
                    current_interaction_mode = command_result.interaction_mode
                if command_result.initial_model is not None:
                    initial_model = command_result.initial_model
                if command_result.reasoning_request is not None:
                    current_reasoning_request = command_result.reasoning_request
                if command_result.output:
                    output_func(command_result.output)
                if command_result.should_exit:
                    await _stop_plugin_run(runtime, plugin_run_task, session_id)
                    return 0
                if command_result.accepted_inputs:
                    if plugin_run_task is None:
                        plugin_run_task = asyncio.create_task(
                            _consume_command_inputs(runtime, command_result.accepted_inputs),
                            name=f"ouro-plugin-input-{session_id}",
                        )
                    else:
                        await _consume_command_inputs(runtime, command_result.accepted_inputs)
                elif command_result.session_inputs or command_result.session_input_batches:
                    if session_id is None:
                        first = (
                            command_result.session_inputs[0]
                            if command_result.session_inputs
                            else command_result.session_input_batches[0].inputs[0]
                        )
                        created = runtime.create_session(
                            title=first.title or _interactive_title(first.prompt),
                            initial_model=initial_model,
                            reasoning_request=current_reasoning_request,
                            approval_mode=current_approval_mode,
                            interaction_mode=current_interaction_mode,
                        )
                        session_id = created.session_id
                    if plugin_run_task is None:
                        plugin_run_task = asyncio.create_task(
                            _submit_plugin_inputs(
                                runtime,
                                command_result.session_inputs,
                                command_result.session_input_batches,
                                session_id,
                            ),
                            name=f"ouro-plugin-input-{session_id}",
                        )
                    else:
                        if command_result.session_input_batches:
                            for batch in command_result.session_input_batches:
                                await runtime.submit_plugin_input_batch(
                                    batch,
                                    session_id=session_id,
                                )
                        else:
                            for session_input in command_result.session_inputs:
                                await runtime.submit_plugin_input(
                                    session_input,
                                    session_id=session_id,
                                )
                if command_result.skill_invocation is None:
                    continue
                skill_invocation = command_result.skill_invocation
                line = skill_invocation.arguments

            try:
                await output.start_waiting()
                resource_refs = attachments.resource_refs(session_id)
                result = await runtime.run(
                    line,
                    skill_invocation=skill_invocation,
                    session_id=session_id,
                    initial_model=initial_model if session_id is None else None,
                    initial_reasoning_request=(
                        current_reasoning_request if session_id is None else None
                    ),
                    queue_mode=queue_mode,
                    approval_mode=current_approval_mode if session_id is None else None,
                    interaction_mode=current_interaction_mode if session_id is None else None,
                    resource_refs=resource_refs,
                )
                # Preserve the durable Session identity even when approval or AskUser
                # continuation fails after the initial RunResult has been returned.
                session_id = result.session.session_id
                restored_input = None
                if resource_refs:
                    attachments.clear()
                if plugin_run_task is not None and any(
                    event.event_type in {"follow_up_input_queued", "steering_input_queued"}
                    for event in result.events
                ):
                    continue
                session_id = await coordinator.resolve_run(result)
                (
                    current_approval_mode,
                    current_interaction_mode,
                    current_reasoning_request,
                ) = _session_preferences(
                    runtime,
                    session_id,
                )
            except OuroError as exc:
                await _report_runtime_error(runtime, output, output_func, exc)
                continue
    finally:
        await coordinator.aclose()


async def _report_runtime_error(
    runtime: RuntimeService,
    output: InteractiveOutput,
    output_func: OutputFunc,
    error: OuroError,
) -> None:
    """Render an error after all earlier lifecycle events have settled."""

    await runtime.drain_events()
    await output.reset()
    output_func(f"Error: {error}")


async def _consume_command_inputs(
    runtime: RuntimeService,
    receipts: tuple[HostInputReceipt, ...],
) -> RunResult:
    result: RunResult | None = None
    for receipt in receipts:
        result = await runtime.consume_host_input(receipt)
    assert result is not None
    return result


async def _submit_plugin_inputs(
    runtime: RuntimeService,
    session_inputs: tuple[AttributedSessionInput, ...],
    session_input_batches: tuple[AttributedSessionInputBatch, ...],
    session_id: str,
) -> RunResult:
    result: RunResult | None = None
    if session_input_batches:
        for batch in session_input_batches:
            result = await runtime.submit_plugin_input_batch(batch, session_id=session_id)
    else:
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


def _interactive_prompt(attachment_count: int) -> str:
    pending = f" [{attachment_count}]" if attachment_count else ""
    return f"{_CYAN}ouro{pending}>{_RESET} "


def _interaction_root_session_id(
    runtime: RuntimeService,
    session_id: str | None,
) -> str | None:
    if session_id is None:
        return None
    return runtime.sessions.open(session_id, recover=False).session.agent_root_session_id


def _session_preferences(
    runtime: RuntimeService,
    session_id: str,
) -> tuple[SessionApprovalMode, SessionInteractionMode, ReasoningRequest]:
    session = runtime.sessions.open(session_id, recover=False).session
    return session.approval_mode, session.interaction_mode, session.reasoning_request


def _primary_session_event_sink(
    runtime: RuntimeService,
    output: InteractiveOutput,
) -> RuntimeEventSink:
    """Limit the terminal transcript to primary Session execution events."""

    visible_sessions: dict[str, bool] = {}

    async def handle_event(event: EventRecord) -> None:
        visible = visible_sessions.get(event.session_id)
        if visible is None:
            session = runtime.sessions.open(event.session_id, recover=False).session
            visible = session.session_kind == "primary"
            visible_sessions[event.session_id] = visible
        if visible:
            await output.handle_event(event)

    return handle_event

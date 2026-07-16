from __future__ import annotations

from pathlib import Path

from ouro.application.queued_inputs import QueuedInputMode
from ouro.application.runtime import RuntimeService
from ouro.cli.interactive_commands import handle_command
from ouro.cli.interactive_io import (
    InputFunc,
    InteractiveOutput,
    OutputFunc,
    handle_run_result,
)
from ouro.cli.run import (
    RuntimeProviderOptions,
    build_default_runtime,
)
from ouro.core.approvals import SessionApprovalMode
from ouro.core.errors import OuroError
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
    session_backend: SessionBackend | None = None,
    runtime: RuntimeService | None = None,
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
                event_sink=output.handle_event,
                session_backend=session_backend,
            )
            owned_application = LocalApplication.from_runtime(runtime, home=home)
            await output.start_mcp_connection(owned_application.mcp.startup_configs)
            await owned_application.start()
            await output.finish_mcp_connection(owned_application.mcp.startup_records)
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
        runtime.add_event_sink(output.handle_event)

    try:
        return await _run_interactive_session(
            runtime,
            output=output,
            input_func=input_func,
            output_func=output_func,
            approval_mode=approval_mode,
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
) -> int:
    session_id: str | None = None
    current_approval_mode = approval_mode
    initial_model = runtime.current_model().key
    output_func("OURO interactive. Type /help for commands, /exit to quit.")

    while True:
        try:
            raw_line = input_func(f"{_CYAN}ouro>{_RESET} ")
        except EOFError:
            output_func("")
            return 0

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
                )
            except OuroError as exc:
                output_func(f"Error: {exc}")
                continue
            session_id = command_result.session_id
            if command_result.approval_mode is not None:
                current_approval_mode = command_result.approval_mode
            if command_result.initial_model is not None:
                initial_model = command_result.initial_model
            if command_result.output:
                output_func(command_result.output)
            if command_result.should_exit:
                return 0
            continue

        try:
            await output.start_waiting()
            result = await runtime.run(
                line,
                session_id=session_id,
                initial_model=initial_model if session_id is None else None,
                queue_mode=queue_mode,
                approval_mode=current_approval_mode if session_id is None else None,
            )
            # Preserve the durable Session identity even when approval or AskUser
            # continuation fails after the initial RunResult has been returned.
            session_id = result.session.session_id
            session_id = await handle_run_result(
                result,
                runtime=runtime,
                input_func=input_func,
                output_func=output_func,
                output=output,
            )
            current_approval_mode = runtime.store.load_session_record(session_id).approval_mode
        except OuroError as exc:
            await output.close()
            output_func(f"Error: {exc}")
            continue

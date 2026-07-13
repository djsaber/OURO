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
    build_default_mcp_runtime,
    build_default_runtime,
)
from ouro.core.approvals import SessionApprovalMode
from ouro.core.errors import OuroError
from ouro.mcp import MCPRuntimeResource

_CYAN = "\x1b[36m"
_RESET = "\x1b[0m"


async def run_interactive(
    *,
    home: str | Path | None = None,
    provider_options: RuntimeProviderOptions | None = None,
    system_prompt: str | None = None,
    approval_mode: SessionApprovalMode = "inherit",
    runtime: RuntimeService | None = None,
    input_func: InputFunc = input,
    output_func: OutputFunc = print,
) -> int:
    output = InteractiveOutput(output_func)
    owned_mcp: MCPRuntimeResource | None = None
    if runtime is None:
        try:
            runtime = build_default_runtime(
                home,
                provider_options=provider_options,
                system_prompt=system_prompt,
                event_sink=output.handle_event,
            )
            owned_mcp = build_default_mcp_runtime(runtime)
            await owned_mcp.start()
        except OuroError as exc:
            output_func(f"Error: {exc}")
            return 1
        for record in owned_mcp.startup_failures:
            output_func(
                f"Warning: MCP server {record.config.server_id} failed to start: "
                f"{record.error or 'unknown error'}"
            )
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
        if owned_mcp is not None:
            await owned_mcp.close()


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
                )
            except OuroError as exc:
                output_func(f"Error: {exc}")
                continue
            session_id = command_result.session_id
            if command_result.approval_mode is not None:
                current_approval_mode = command_result.approval_mode
            if command_result.output:
                output_func(command_result.output)
            if command_result.should_exit:
                return 0
            continue

        try:
            result = await runtime.run(
                line,
                session_id=session_id,
                queue_mode=queue_mode,
                approval_mode=current_approval_mode if session_id is None else None,
            )
        except OuroError as exc:
            output_func(f"Error: {exc}")
            continue
        session_id = await handle_run_result(
            result,
            runtime=runtime,
            input_func=input_func,
            output_func=output_func,
            output=output,
        )
        current_approval_mode = runtime.store.load_session(
            session_id,
            recover=False,
        ).session.approval_mode

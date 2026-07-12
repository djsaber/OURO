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
from ouro.cli.run import RuntimeProviderOptions, build_default_runtime
from ouro.core.errors import OuroError

_CYAN = "\x1b[36m"
_RESET = "\x1b[0m"


async def run_interactive(
    *,
    home: str | Path | None = None,
    provider_options: RuntimeProviderOptions | None = None,
    system_prompt: str | None = None,
    runtime: RuntimeService | None = None,
    input_func: InputFunc = input,
    output_func: OutputFunc = print,
) -> int:
    output = InteractiveOutput(output_func)
    if runtime is None:
        try:
            runtime = build_default_runtime(
                home,
                provider_options=provider_options,
                system_prompt=system_prompt,
                event_sink=output.handle_event,
            )
        except OuroError as exc:
            output_func(f"Error: {exc}")
            return 1
    else:
        runtime.add_event_sink(output.handle_event)

    session_id: str | None = None
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
                command_result = await handle_command(line, runtime, session_id)
            except OuroError as exc:
                output_func(f"Error: {exc}")
                continue
            session_id = command_result.session_id
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

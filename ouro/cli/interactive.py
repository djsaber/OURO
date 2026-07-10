from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ouro.application.runtime import RuntimeService
from ouro.cli.run import RuntimeProviderOptions, build_default_runtime
from ouro.core.errors import OuroError
from ouro.core.events import EventRecord
from ouro.core.results import RunResult

InputFunc = Callable[[str], str]
OutputFunc = Callable[[str], None]

# ANSI styling helpers. These are intentionally compact; terminals that do not
# support ANSI escape sequences will show the raw codes, which is acceptable
# for the interactive CLI target environment.
_DIM_ITALIC = "\x1b[2;3m"
_DIM = "\x1b[2m"
_YELLOW = "\x1b[33m"
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
    streamed_run_ids: set[str] = set()
    stream_state: dict[str, Any] = {
        "in_reasoning": False,
        "streamed_text": False,
    }

    def stream_event(event: EventRecord) -> None:
        if event.run_id is None:
            return
        if event.event_type == "model_text_delta":
            text = event.payload.get("text")
            if not isinstance(text, str) or not text:
                return
            streamed_run_ids.add(event.run_id)
            _stream_text_delta(text, stream_state, output_func)
        elif event.event_type == "model_reasoning_delta":
            reasoning = event.payload.get("reasoning_content")
            if not isinstance(reasoning, str) or not reasoning:
                return
            streamed_run_ids.add(event.run_id)
            _stream_reasoning_delta(reasoning, stream_state, output_func)
        elif event.event_type == "tool_call_requested":
            streamed_run_ids.add(event.run_id)
            _stream_tool_call(event.payload, stream_state, output_func)

    if runtime is None:
        try:
            runtime = build_default_runtime(
                home,
                provider_options=provider_options,
                system_prompt=system_prompt,
                event_sink=stream_event,
            )
        except OuroError as exc:
            output_func(f"Error: {exc}")
            return 1
    else:
        runtime.add_event_sink(stream_event)
    session_id: str | None = None
    output_func("OURO interactive. Type /help for commands, /exit to quit.")

    while True:
        try:
            raw_line = input_func("ouro> ")
        except EOFError:
            output_func("")
            return 0

        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("/"):
            command_result = await _handle_command(line, runtime, session_id)
            session_id = command_result.session_id
            if command_result.output:
                output_func(command_result.output)
            if command_result.should_exit:
                return 0
            continue

        result = await runtime.run(line, session_id=session_id)
        session_id = await _handle_run_result(
            result,
            runtime=runtime,
            input_func=input_func,
            output_func=output_func,
            streamed_run_ids=streamed_run_ids,
            stream_state=stream_state,
        )


class _CommandResult:
    def __init__(
        self,
        *,
        session_id: str | None,
        output: str = "",
        should_exit: bool = False,
    ) -> None:
        self.session_id = session_id
        self.output = output
        self.should_exit = should_exit


async def _handle_command(
    line: str,
    runtime: RuntimeService,
    session_id: str | None,
) -> _CommandResult:
    command, _, argument = line.partition(" ")
    command = command.lower()
    argument = argument.strip()

    if command == "/help":
        return _CommandResult(session_id=session_id, output=_help_text())
    if command == "/exit":
        return _CommandResult(session_id=session_id, should_exit=True)
    if command == "/new":
        return _CommandResult(session_id=None, output="Started a new session.")
    if command == "/sessions":
        return _CommandResult(session_id=session_id, output=_sessions_text(runtime))
    if command == "/open":
        return _open_session(runtime, argument)
    if command == "/model":
        model = runtime.adapter.model
        return _CommandResult(
            session_id=session_id,
            output=f"{model.provider_id}/{model.model_id}",
        )
    if command == "/config":
        return _CommandResult(session_id=session_id, output=f"OURO_HOME={runtime.store.root}")
    if command == "/export":
        return _export_session(runtime, session_id)
    if command == "/abort":
        return await _abort_session(runtime, session_id)
    return _CommandResult(session_id=session_id, output=f"Unknown command: {command}")


def _help_text() -> str:
    return "\n".join(
        [
            "Commands:",
            "/help - Show commands.",
            "/new - Start a new session.",
            "/sessions - List local sessions.",
            "/open <session_id> - Continue a session.",
            "/model - Show current model.",
            "/config - Show active local config path.",
            "/export - Show active session data path.",
            "/abort - Abort the active request when supported.",
            "/exit - Quit.",
        ]
    )


def _sessions_text(runtime: RuntimeService) -> str:
    sessions = runtime.sessions.list()
    if not sessions:
        return "No sessions."
    lines = ["Sessions:"]
    for session in sessions:
        marker = "*" if session.status != "idle" else "-"
        title = session.title or "New session"
        lines.append(f"{marker} {session.session_id} {session.status} {title}")
    return "\n".join(lines)


def _open_session(runtime: RuntimeService, session_id: str) -> _CommandResult:
    if not session_id:
        return _CommandResult(session_id=None, output="Usage: /open <session_id>")
    try:
        snapshot = runtime.sessions.open(session_id)
    except Exception:  # noqa: BLE001 - interactive command should report and continue.
        return _CommandResult(session_id=None, output=f"Session not found: {session_id}")
    return _CommandResult(
        session_id=snapshot.session.session_id,
        output=f"Opened session: {snapshot.session.session_id}",
    )


def _export_session(runtime: RuntimeService, session_id: str | None) -> _CommandResult:
    if session_id is None:
        return _CommandResult(session_id=session_id, output="No active session.")
    return _CommandResult(
        session_id=session_id,
        output=str(runtime.store.sessions_dir / session_id),
    )


async def _abort_session(runtime: RuntimeService, session_id: str | None) -> _CommandResult:
    if session_id is None:
        return _CommandResult(session_id=None, output="No active session.")
    try:
        result = await runtime.abort_run(session_id=session_id)
    except ValueError:
        return _CommandResult(session_id=session_id, output="No active running request.")
    if result.run.status == "aborted":
        return _CommandResult(
            session_id=result.session.session_id,
            output=f"Aborted run: {result.run.run_id}",
        )
    return _CommandResult(session_id=result.session.session_id, output="No active running request.")


async def _handle_run_result(
    result: RunResult,
    *,
    runtime: RuntimeService,
    input_func: InputFunc,
    output_func: OutputFunc,
    streamed_run_ids: set[str],
    stream_state: dict[str, Any],
) -> str:
    current = result
    for _ in range(16):
        if current.run.status == "completed":
            if current.run.run_id in streamed_run_ids:
                _finish_streamed_output(stream_state, output_func)
            elif current.final_text:
                output_func(current.final_text)
            return current.session.session_id
        if current.run.status == "waiting_approval":
            resolved = await _resolve_approval(
                current,
                runtime=runtime,
                input_func=input_func,
                output_func=output_func,
            )
            if resolved is None:
                return current.session.session_id
            current = resolved
            continue
        if current.run.status == "waiting_user":
            resolved = await _resolve_user_question(
                current,
                runtime=runtime,
                input_func=input_func,
                output_func=output_func,
            )
            if resolved is None:
                return current.session.session_id
            current = resolved
            continue
        if current.run.status == "failed":
            if current.run.run_id in streamed_run_ids:
                _finish_streamed_output(stream_state, output_func)
            output_func(f"Run failed: {current.run.error or 'unknown error'}")
            return current.session.session_id
        if current.run.status == "aborted":
            if current.run.run_id in streamed_run_ids:
                _finish_streamed_output(stream_state, output_func)
            output_func(f"Run aborted: {current.run.error or 'User aborted'}")
            return current.session.session_id
        output_func(f"Run stopped with status: {current.run.status}")
        return current.session.session_id

    output_func("Run stopped: interactive resolution limit reached.")
    return current.session.session_id


def _stream_text_delta(text: str, stream_state: dict[str, Any], output_func: OutputFunc) -> None:
    if stream_state["in_reasoning"]:
        _write_output(f"{_RESET}\n", output_func)
        stream_state["in_reasoning"] = False
    stream_state["streamed_text"] = True
    _write_output(text, output_func)


def _stream_reasoning_delta(
    reasoning: str, stream_state: dict[str, Any], output_func: OutputFunc
) -> None:
    if stream_state["streamed_text"]:
        _write_output("\n", output_func)
        stream_state["streamed_text"] = False
    if not stream_state["in_reasoning"]:
        _write_output(_DIM_ITALIC, output_func)
        stream_state["in_reasoning"] = True
    _write_output(reasoning, output_func)


def _stream_tool_call(
    payload: dict[str, Any], stream_state: dict[str, Any], output_func: OutputFunc
) -> None:
    if stream_state["in_reasoning"]:
        _write_output(f"{_RESET}\n", output_func)
        stream_state["in_reasoning"] = False
    if stream_state["streamed_text"]:
        _write_output("\n", output_func)
        stream_state["streamed_text"] = False
    tool_name = str(payload.get("tool_name") or "unknown")
    arguments = payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {}
    args_text = json.dumps(arguments, ensure_ascii=False)
    _write_output(
        f"{_DIM}>{_RESET} {_YELLOW}{tool_name}{_RESET}({_DIM}{args_text}{_RESET})\n",
        output_func,
    )


def _write_output(text: str, output_func: OutputFunc) -> None:
    if output_func is print:
        sys.stdout.write(text)
        sys.stdout.flush()
        return
    output_func(text)


def _finish_streamed_output(stream_state: dict[str, Any], output_func: OutputFunc) -> None:
    if stream_state["in_reasoning"]:
        _write_output(_RESET, output_func)
        stream_state["in_reasoning"] = False
    _write_output("\n", output_func)
    stream_state["streamed_text"] = False


async def _resolve_approval(
    result: RunResult,
    *,
    runtime: RuntimeService,
    input_func: InputFunc,
    output_func: OutputFunc,
) -> RunResult | None:
    invocation = _pending_invocation(result)
    tool_name = str(invocation.get("tool_name") or "unknown")
    # Tool arguments were already printed by _stream_tool_call when the
    # tool_call_requested event fired, so we only show the compact prompt here
    # to avoid repeating the same JSON block.
    output_func(f"Approval required: {tool_name}")

    while True:
        try:
            choice = input_func("Approve? [y]es/[n]o/[m]odify: ").strip().lower()
        except EOFError:
            output_func("")
            return None

        if choice in {"y", "yes"}:
            return await runtime.approve_tool(
                session_id=result.session.session_id,
                run_id=result.run.run_id,
                approved=True,
            )
        if choice in {"", "n", "no"}:
            reason = _input_optional(input_func, output_func, "Reason: ") or "User rejected"
            return await runtime.approve_tool(
                session_id=result.session.session_id,
                run_id=result.run.run_id,
                approved=False,
                reason=reason,
            )
        if choice in {"/abort", "abort"}:
            return await runtime.abort_run(
                session_id=result.session.session_id,
                run_id=result.run.run_id,
            )
        if choice in {"m", "modify"}:
            override = _input_json_object(input_func, output_func, "Arguments JSON: ")
            if override is None:
                continue
            return await runtime.approve_tool(
                session_id=result.session.session_id,
                run_id=result.run.run_id,
                approved=True,
                arguments_override=override,
            )
        output_func("Choose y, n, or m.")


async def _resolve_user_question(
    result: RunResult,
    *,
    runtime: RuntimeService,
    input_func: InputFunc,
    output_func: OutputFunc,
) -> RunResult | None:
    question = _pending_question(result)
    output_func(f"Question: {question}")
    try:
        answer = input_func("Answer: ")
    except EOFError:
        output_func("")
        return None
    if answer.strip().lower() in {"/abort", "abort"}:
        return await runtime.abort_run(
            session_id=result.session.session_id,
            run_id=result.run.run_id,
        )
    return await runtime.answer_user(
        session_id=result.session.session_id,
        run_id=result.run.run_id,
        answer=answer,
    )


def _pending_invocation(result: RunResult) -> dict[str, Any]:
    raw = result.run.metadata.get("pending_invocation")
    return raw if isinstance(raw, dict) else {}


def _pending_question(result: RunResult) -> str:
    raw_question = result.run.metadata.get("pending_question")
    if isinstance(raw_question, str) and raw_question:
        return raw_question
    invocation = _pending_invocation(result)
    arguments = invocation.get("arguments")
    if isinstance(arguments, dict) and isinstance(arguments.get("question"), str):
        return arguments["question"]
    return "Please provide the requested information."


def _input_optional(
    input_func: InputFunc,
    output_func: OutputFunc,
    prompt: str,
) -> str:
    try:
        return input_func(prompt).strip()
    except EOFError:
        output_func("")
        return ""


def _input_json_object(
    input_func: InputFunc,
    output_func: OutputFunc,
    prompt: str,
) -> dict[str, object] | None:
    try:
        raw = input_func(prompt)
    except EOFError:
        output_func("")
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        output_func(f"Invalid JSON: {exc.msg}")
        return None
    if not isinstance(parsed, dict):
        output_func("Arguments JSON must be an object.")
        return None
    return parsed


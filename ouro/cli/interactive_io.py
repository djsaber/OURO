from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import Any

from ouro.application.runtime import RuntimeService
from ouro.core.events import EventRecord
from ouro.core.results import RunResult

InputFunc = Callable[[str], str]
OutputFunc = Callable[[str], None]

_DIM_ITALIC = "\x1b[2;3m"
_DIM = "\x1b[2m"
_YELLOW = "\x1b[33m"
_RESET = "\x1b[0m"


class InteractiveOutput:
    """Render runtime events and track whether a run already streamed output."""

    def __init__(self, output_func: OutputFunc) -> None:
        self.output_func = output_func
        self.streamed_run_ids: set[str] = set()
        self.in_reasoning = False
        self.streamed_text = False

    def handle_event(self, event: EventRecord) -> None:
        if event.run_id is None:
            return
        if event.event_type == "model_text_delta":
            text = event.payload.get("text")
            if isinstance(text, str) and text:
                self.streamed_run_ids.add(event.run_id)
                self._stream_text_delta(text)
        elif event.event_type == "model_reasoning_delta":
            reasoning = event.payload.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                self.streamed_run_ids.add(event.run_id)
                self._stream_reasoning_delta(reasoning)
        elif event.event_type == "tool_call_requested":
            self.streamed_run_ids.add(event.run_id)
            self._stream_tool_call(event.payload)
        elif event.event_type == "compaction_started":
            trigger = str(event.payload.get("trigger") or "auto")
            self.output_func(f"Context {trigger} compaction in progress...")
        elif event.event_type == "compaction_completed":
            covered = int(event.payload.get("covered_message_count") or 0)
            self.output_func(f"Context compaction completed ({covered} messages covered).")
        elif event.event_type == "compaction_failed":
            error = event.payload.get("error") or "unknown error"
            self.output_func(f"Context compaction failed: {error}")

    def finish_run(self, run_id: str) -> bool:
        if run_id not in self.streamed_run_ids:
            return False
        if self.in_reasoning:
            self._write(_RESET)
            self.in_reasoning = False
        self._write("\n")
        self.streamed_text = False
        return True

    def _stream_text_delta(self, text: str) -> None:
        if self.in_reasoning:
            self._write(f"{_RESET}\n")
            self.in_reasoning = False
        self.streamed_text = True
        self._write(text)

    def _stream_reasoning_delta(self, reasoning: str) -> None:
        if self.streamed_text:
            self._write("\n")
            self.streamed_text = False
        if not self.in_reasoning:
            self._write(_DIM_ITALIC)
            self.in_reasoning = True
        self._write(reasoning)

    def _stream_tool_call(self, payload: dict[str, Any]) -> None:
        if self.in_reasoning:
            self._write(f"{_RESET}\n")
            self.in_reasoning = False
        if self.streamed_text:
            self._write("\n")
            self.streamed_text = False
        tool_name = str(payload.get("tool_name") or "unknown")
        arguments = payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {}
        args_text = json.dumps(arguments, ensure_ascii=False)
        self._write(f"{_DIM}>{_RESET} {_YELLOW}{tool_name}{_RESET}({_DIM}{args_text}{_RESET})\n")

    def _write(self, text: str) -> None:
        if self.output_func is print:
            sys.stdout.write(text)
            sys.stdout.flush()
            return
        self.output_func(text)


async def handle_run_result(
    result: RunResult,
    *,
    runtime: RuntimeService,
    input_func: InputFunc,
    output_func: OutputFunc,
    output: InteractiveOutput,
) -> str:
    current = result
    for _ in range(16):
        if current.run.status == "completed":
            if not output.finish_run(current.run.run_id) and current.final_text:
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
            output.finish_run(current.run.run_id)
            output_func(f"Run failed: {current.run.error or 'unknown error'}")
            return current.session.session_id
        if current.run.status == "aborted":
            output.finish_run(current.run.run_id)
            output_func(f"Run aborted: {current.run.error or 'User aborted'}")
            return current.session.session_id
        output_func(f"Run stopped with status: {current.run.status}")
        return current.session.session_id

    output_func("Run stopped: interactive resolution limit reached.")
    return current.session.session_id


async def _resolve_approval(
    result: RunResult,
    *,
    runtime: RuntimeService,
    input_func: InputFunc,
    output_func: OutputFunc,
) -> RunResult | None:
    invocation = _pending_invocation(result)
    output_func(f"Approval required: {invocation.get('tool_name') or 'unknown'}")

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
            if override is not None:
                return await runtime.approve_tool(
                    session_id=result.session.session_id,
                    run_id=result.run.run_id,
                    approved=True,
                    arguments_override=override,
                )
            continue
        output_func("Choose y, n, or m.")


async def _resolve_user_question(
    result: RunResult,
    *,
    runtime: RuntimeService,
    input_func: InputFunc,
    output_func: OutputFunc,
) -> RunResult | None:
    output_func(f"Question: {_pending_question(result)}")
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
    arguments = _pending_invocation(result).get("arguments")
    if isinstance(arguments, dict) and isinstance(arguments.get("question"), str):
        return arguments["question"]
    return "Please provide the requested information."


def _input_optional(input_func: InputFunc, output_func: OutputFunc, prompt: str) -> str:
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

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
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
_CLEAR_LINE = "\x1b[2K"
_MAX_TOOL_ARGUMENT_PREVIEW = 240


@dataclass
class _ToolCallPreview:
    tool_name: str = ""
    argument_chunks: list[str] = field(default_factory=list)
    argument_preview: str = ""
    arguments_truncated: bool = False

    def append(self, payload: dict[str, Any]) -> None:
        self.tool_name += _payload_text(payload, "name_delta")
        arguments_delta = _payload_text(payload, "arguments_delta")
        if not arguments_delta:
            return
        self.argument_chunks.append(arguments_delta)
        remaining = _MAX_TOOL_ARGUMENT_PREVIEW - len(self.argument_preview)
        if remaining > 0:
            self.argument_preview += arguments_delta[:remaining]
        if len(arguments_delta) > remaining:
            self.arguments_truncated = True

    @property
    def arguments(self) -> str:
        return "".join(self.argument_chunks)


class InteractiveOutput:
    """Render runtime events and track whether a run already streamed output."""

    def __init__(self, output_func: OutputFunc) -> None:
        self.output_func = output_func
        self.streamed_run_ids: set[str] = set()
        self.in_reasoning = False
        self.streamed_text = False
        self.tool_call_previews: dict[tuple[str, str, int], _ToolCallPreview] = {}
        self.active_preview_key: tuple[str, str, int] | None = None

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
        elif event.event_type == "model_tool_call_delta":
            self.streamed_run_ids.add(event.run_id)
            self._stream_tool_call_delta(event)
        elif event.event_type == "model_message_completed":
            tool_call_count = event.payload.get("tool_call_count")
            if isinstance(tool_call_count, int) and tool_call_count > 0:
                self._finish_tool_call_turn(event.run_id, event.turn_id)
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
        self._finish_incomplete_tool_calls(run_id)
        self._write("\n")
        self.streamed_text = False
        return True

    def _stream_text_delta(self, text: str) -> None:
        self._clear_active_preview()
        if self.in_reasoning:
            self._write(f"{_RESET}\n")
            self.in_reasoning = False
        self.streamed_text = True
        self._write(text)

    def _stream_reasoning_delta(self, reasoning: str) -> None:
        self._clear_active_preview()
        if self.streamed_text:
            self._write("\n")
            self.streamed_text = False
        if not self.in_reasoning:
            self._write(_DIM_ITALIC)
            self.in_reasoning = True
        self._write(reasoning)

    def _stream_tool_call_delta(self, event: EventRecord) -> None:
        if self.in_reasoning:
            self._write(f"{_RESET}\n")
            self.in_reasoning = False
        if self.streamed_text:
            self._write("\n")
            self.streamed_text = False
        index = event.payload.get("index")
        if not isinstance(index, int) or index < 0:
            return
        turn_id = event.turn_id or ""
        key = (event.run_id or "", turn_id, index)
        preview = self.tool_call_previews.setdefault(key, _ToolCallPreview())
        preview.append(event.payload)
        self.active_preview_key = key
        self._write(f"\r{_CLEAR_LINE}{self._render_tool_call(preview)}")

    def _finish_tool_call_turn(self, run_id: str, turn_id: str | None) -> None:
        keys = sorted(
            (
                key
                for key in self.tool_call_previews
                if key[0] == run_id and key[1] == (turn_id or "")
            ),
            key=lambda key: key[2],
        )
        if not keys:
            return
        self._clear_active_preview()
        self._end_streamed_content_line()
        for key in keys:
            preview = self.tool_call_previews.pop(key)
            self._write(f"{self._render_tool_call(preview, final=True)}\n")

    def _finish_incomplete_tool_calls(self, run_id: str) -> None:
        keys = sorted(
            (key for key in self.tool_call_previews if key[0] == run_id),
            key=lambda key: (key[1], key[2]),
        )
        if not keys:
            return
        self._clear_active_preview()
        self._end_streamed_content_line()
        for key in keys:
            preview = self.tool_call_previews.pop(key)
            self._write(f"{self._render_tool_call(preview)} {_DIM}(incomplete){_RESET}\n")

    def _clear_active_preview(self) -> None:
        if self.active_preview_key is None:
            return
        self._write(f"\r{_CLEAR_LINE}")
        self.active_preview_key = None

    def _end_streamed_content_line(self) -> None:
        if self.in_reasoning:
            self._write(f"{_RESET}\n")
            self.in_reasoning = False
        if self.streamed_text:
            self._write("\n")
            self.streamed_text = False

    @staticmethod
    def _render_tool_call(preview: _ToolCallPreview, *, final: bool = False) -> str:
        tool_name = preview.tool_name or "preparing tool"
        if final:
            arguments = _format_completed_arguments(preview.arguments)
        else:
            arguments = _single_line(preview.argument_preview)
            if preview.arguments_truncated:
                arguments += "…"
        return f"{_DIM}>{_RESET} {_YELLOW}{tool_name}{_RESET}({_DIM}{arguments}{_RESET})"

    def _write(self, text: str) -> None:
        if self.output_func is print:
            sys.stdout.write(text)
            sys.stdout.flush()
            return
        self.output_func(text)


def _payload_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    return value if isinstance(value, str) else ""


def _format_completed_arguments(arguments: str) -> str:
    if not arguments:
        return ""
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError:
        return _single_line(arguments)
    return json.dumps(parsed, ensure_ascii=False)


def _single_line(value: str) -> str:
    return value.replace("\r", "\\r").replace("\n", "\\n")


async def handle_run_result(
    result: RunResult,
    *,
    runtime: RuntimeService,
    input_func: InputFunc,
    output_func: OutputFunc,
    output: InteractiveOutput,
) -> str:
    current = result
    while True:
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
        if current.run.status == "paused":
            output.finish_run(current.run.run_id)
            pause = current.run.metadata.get("pause") or {}
            reason = pause.get("reason") if isinstance(pause, dict) else None
            kind = pause.get("kind") if isinstance(pause, dict) else None
            boundary = (
                "for context maintenance"
                if kind == "context_maintenance"
                else "at a soft budget boundary"
            )
            output_func(f"Run paused {boundary}: {reason or 'configured limit'}")
            return current.session.session_id
        output_func(f"Run stopped with status: {current.run.status}")
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
    batch = result.run.active_tool_batch
    if batch is not None:
        pending = next(
            (
                call.invocation
                for call in batch.calls
                if call.status in {"awaiting_approval", "waiting_user"}
                and call.invocation is not None
            ),
            None,
        )
        if pending is not None:
            return pending.model_dump(mode="json")
    raw = result.run.metadata.get("pending_invocation")
    return raw if isinstance(raw, dict) else {}


def _pending_question(result: RunResult) -> str:
    batch = result.run.active_tool_batch
    if batch is not None:
        waiting = next((call for call in batch.calls if call.status == "waiting_user"), None)
        if waiting is not None:
            if waiting.result is not None and waiting.result.content:
                return waiting.result.content
            question = waiting.request.arguments.get("question")
            if isinstance(question, str) and question:
                return question
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

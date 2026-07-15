from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from ouro.application.runtime import RuntimeService
from ouro.cli.interactive_output import InteractiveOutput, OutputFunc
from ouro.core.results import RunResult

InputFunc = Callable[[str], str]
_MAX_APPROVAL_ARGUMENT_PREVIEW = 240


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
            if not await output.finish_run(current.run.run_id) and current.final_text:
                output_func(current.final_text)
            return current.session.session_id
        if current.run.status == "waiting_approval":
            resolved = await _resolve_approval(
                current,
                runtime=runtime,
                input_func=input_func,
                output_func=output_func,
                output=output,
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
                output=output,
            )
            if resolved is None:
                return current.session.session_id
            current = resolved
            continue
        if current.run.status == "failed":
            await output.finish_run(current.run.run_id)
            output_func(f"Run failed: {current.run.error or 'unknown error'}")
            return current.session.session_id
        if current.run.status == "aborted":
            await output.finish_run(current.run.run_id)
            output_func(f"Run aborted: {current.run.error or 'User aborted'}")
            return current.session.session_id
        if current.run.status == "paused":
            await output.finish_run(current.run.run_id)
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
    output: InteractiveOutput,
) -> RunResult | None:
    await output.suspend_tools_for_prompt()
    invocation = _pending_invocation(result)
    output_func(_approval_request(invocation))

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
    output: InteractiveOutput,
) -> RunResult | None:
    await output.suspend_tools_for_prompt()
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


def _approval_request(invocation: dict[str, Any]) -> str:
    tool_name = _single_line(str(invocation.get("tool_name") or "unknown"))
    arguments = invocation.get("arguments")
    if not isinstance(arguments, dict):
        return f"Approval required: {tool_name}"
    preview = json.dumps(arguments, ensure_ascii=False)
    if len(preview) > _MAX_APPROVAL_ARGUMENT_PREVIEW:
        preview = preview[: _MAX_APPROVAL_ARGUMENT_PREVIEW - 1] + "…"
    return f"Approval required: {tool_name}({preview})"


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


def _single_line(value: str) -> str:
    return value.replace("\r", "\\r").replace("\n", "\\n")


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

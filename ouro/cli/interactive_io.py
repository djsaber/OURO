from __future__ import annotations

import asyncio
import json
from typing import Any, Literal, Protocol

from pydantic import JsonValue

from ouro.application.agent_models import AgentInteractionView, AgentTreeNotification
from ouro.application.runtime import RuntimeService
from ouro.cli.interactive_input import InputFunc, InteractiveInput, PromptFunc
from ouro.cli.interactive_output import InteractiveOutput, OutputFunc
from ouro.cli.plan_confirmation import (
    PlanConfirmationControl,
    collect_plan_confirmation,
    parse_builtin_plan_confirmation,
)
from ouro.core.errors import OuroError
from ouro.core.results import RunResult
from ouro.core.tool_batches import ToolBatchCall

_MAX_APPROVAL_ARGUMENT_PREVIEW = 240


class PrefetchedInput:
    """Present one already-read terminal line before falling back to the real input."""

    def __init__(self, value: str, fallback: InputFunc) -> None:
        self._value: str | None = value
        self._fallback = fallback

    def __call__(self, prompt: str) -> str:
        if self._value is not None:
            value = self._value
            self._value = None
            return value
        return self._fallback(prompt)


class AgentTreeSubscriptionPort(Protocol):
    async def get(self) -> AgentTreeNotification: ...

    def close(self) -> None: ...


class AgentInteractionHost(Protocol):
    def subscribe_agent_tree(self, root_session_id: str) -> AgentTreeSubscriptionPort: ...

    def list_agent_interactions(
        self,
        root_session_id: str,
    ) -> tuple[AgentInteractionView, ...]: ...

    async def resolve_agent_approval(
        self,
        delegation_id: str,
        *,
        child_session_id: str,
        child_run_id: str,
        tool_call_id: str,
        approval_id: str,
        approved: bool,
        reason: str = "User rejected",
        arguments_override: dict[str, object] | None = None,
    ) -> object: ...

    async def resolve_agent_input(
        self,
        delegation_id: str,
        *,
        child_session_id: str,
        child_run_id: str,
        tool_call_id: str,
        request_id: str,
        answer: JsonValue,
    ) -> object: ...

    async def interrupt_agent(
        self,
        delegation_id: str,
        *,
        reason: str = "Agent interrupted",
    ) -> object: ...


async def read_interactive_input(
    prompt: str,
    *,
    root_session_id: str | None,
    agents: AgentInteractionHost | None,
    input_func: InputFunc,
    prompt_func: PromptFunc,
    output_func: OutputFunc,
    output: InteractiveOutput,
) -> str:
    """Read the primary prompt while keeping background subagents observable."""

    if agents is None or root_session_id is None:
        return await InteractiveInput(input_func, prompt_func).read(prompt)

    subscription = agents.subscribe_agent_tree(root_session_id)
    terminal_input = InteractiveInput(input_func, prompt_func)
    resolved_requests: set[tuple[str, str, str]] = set()
    input_task: asyncio.Task[str] | None = None
    notification_task: asyncio.Task[AgentTreeNotification] | None = None
    try:
        if not await _resolve_pending_subagent_interactions(
            root_session_id,
            agents=agents,
            input_func=input_func,
            output_func=output_func,
            output=output,
            resolved_requests=resolved_requests,
        ):
            raise EOFError
        input_task = terminal_input.begin(prompt)
        while True:
            pending = tuple(
                interaction
                for interaction in agents.list_agent_interactions(root_session_id)
                if _interaction_identity(interaction) not in resolved_requests
            )
            if pending and not input_task.done():
                interaction = pending[0]
                identity = _interaction_identity(interaction)
                terminal_input.break_prompt()
                await _announce_subagent_interaction(
                    interaction,
                    output_func=output_func,
                    output=output,
                )
                terminal_input.replace_prompt(_subagent_input_prompt(interaction))
                answer = await terminal_input.finish(input_task)
                resolution = await _resolve_current_subagent_interaction(
                    interaction,
                    agents=agents,
                    root_session_id=root_session_id,
                    input_func=input_func,
                    output_func=output_func,
                    output=output,
                    announce=False,
                    initial_response=answer,
                )
                if resolution == "eof":
                    raise EOFError
                resolved_requests.add(identity)
                if not await _resolve_pending_subagent_interactions(
                    root_session_id,
                    agents=agents,
                    input_func=input_func,
                    output_func=output_func,
                    output=output,
                    resolved_requests=resolved_requests,
                ):
                    raise EOFError
                input_task = terminal_input.begin(prompt)
                continue
            if input_task.done():
                return await terminal_input.finish(input_task)

            notification_task = asyncio.create_task(
                subscription.get(),
                name=f"ouro-subagent-notification-{root_session_id}",
            )
            done, _ = await asyncio.wait(
                (input_task, notification_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if input_task in done:
                notification_task.cancel()
                await asyncio.gather(notification_task, return_exceptions=True)
                notification_task = None
                return await terminal_input.finish(input_task)
            try:
                notification_task.result()
            except StopAsyncIteration:
                return await terminal_input.finish(input_task)
            finally:
                notification_task = None
    finally:
        if notification_task is not None:
            notification_task.cancel()
            await asyncio.gather(notification_task, return_exceptions=True)
        if input_task is not None and not input_task.done():
            input_task.cancel()
            await asyncio.gather(input_task, return_exceptions=True)
        subscription.close()


async def handle_run_result(
    result: RunResult,
    *,
    runtime: RuntimeService,
    input_func: InputFunc,
    output_func: OutputFunc,
    output: InteractiveOutput,
    agents: AgentInteractionHost | None = None,
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
        if current.run.status == "waiting_child" and agents is not None:
            resolved = await _follow_waiting_subagents(
                current,
                runtime=runtime,
                agents=agents,
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


async def _follow_waiting_subagents(
    result: RunResult,
    *,
    runtime: RuntimeService,
    agents: AgentInteractionHost,
    input_func: InputFunc,
    output_func: OutputFunc,
    output: InteractiveOutput,
) -> RunResult | None:
    root_session_id = result.session.session_id
    root_run_id = result.run.run_id
    subscription = agents.subscribe_agent_tree(root_session_id)
    resolved_requests: set[tuple[str, str, str]] = set()
    try:
        while True:
            current = _durable_run_result(runtime, root_session_id, root_run_id)
            # Resolving a synchronous SpawnAgent schedules the same durable parent Run. Its
            # queued/running states are continuation progress, not Host boundaries.
            if current.run.status not in {"queued", "running", "waiting_child"}:
                return current

            pending = tuple(
                interaction
                for interaction in agents.list_agent_interactions(root_session_id)
                if _interaction_identity(interaction) not in resolved_requests
            )
            if current.run.status == "waiting_child" and pending:
                if not await _resolve_pending_subagent_interactions(
                    root_session_id,
                    agents=agents,
                    input_func=input_func,
                    output_func=output_func,
                    output=output,
                    resolved_requests=resolved_requests,
                ):
                    return None
                continue

            try:
                await subscription.get()
            except StopAsyncIteration as exc:
                raise OuroError(
                    "Subagent interaction subscription closed while the parent Run is waiting"
                ) from exc
    finally:
        subscription.close()


async def _resolve_pending_subagent_interactions(
    root_session_id: str,
    *,
    agents: AgentInteractionHost,
    input_func: InputFunc,
    output_func: OutputFunc,
    output: InteractiveOutput,
    resolved_requests: set[tuple[str, str, str]] | None = None,
) -> bool:
    resolved = resolved_requests if resolved_requests is not None else set()
    while True:
        pending = tuple(
            interaction
            for interaction in agents.list_agent_interactions(root_session_id)
            if _interaction_identity(interaction) not in resolved
        )
        if not pending:
            return True
        interaction = pending[0]
        identity = _interaction_identity(interaction)
        resolution = await _resolve_current_subagent_interaction(
            interaction,
            agents=agents,
            root_session_id=root_session_id,
            input_func=input_func,
            output_func=output_func,
            output=output,
        )
        if resolution == "eof":
            return False
        resolved.add(identity)


async def _resolve_current_subagent_interaction(
    interaction: AgentInteractionView,
    *,
    agents: AgentInteractionHost,
    root_session_id: str,
    input_func: InputFunc,
    output_func: OutputFunc,
    output: InteractiveOutput,
    announce: bool = True,
    initial_response: str | None = None,
) -> Literal["resolved", "stale", "eof"]:
    identity = _interaction_identity(interaction)
    try:
        handled = await _resolve_subagent_interaction(
            interaction,
            agents=agents,
            input_func=input_func,
            output_func=output_func,
            output=output,
            announce=announce,
            initial_response=initial_response,
        )
    except ValueError as exc:
        current = {
            _interaction_identity(item)
            for item in agents.list_agent_interactions(root_session_id)
        }
        if identity not in current:
            return "stale"
        raise OuroError(
            "Could not resolve subagent interaction "
            f"{interaction.delegation_id}/{interaction.request_id}: {exc}"
        ) from exc
    return "resolved" if handled else "eof"


async def _resolve_subagent_interaction(
    interaction: AgentInteractionView,
    *,
    agents: AgentInteractionHost,
    input_func: InputFunc,
    output_func: OutputFunc,
    output: InteractiveOutput,
    announce: bool = True,
    initial_response: str | None = None,
) -> bool:
    if interaction.kind == "approval":
        return await _resolve_subagent_approval(
            interaction,
            agents=agents,
            input_func=input_func,
            output_func=output_func,
            output=output,
            announce=announce,
            initial_response=initial_response,
        )
    return await _resolve_subagent_question(
        interaction,
        agents=agents,
        input_func=input_func,
        output_func=output_func,
        output=output,
        announce=announce,
        initial_response=initial_response,
    )


async def _announce_subagent_interaction(
    interaction: AgentInteractionView,
    *,
    output_func: OutputFunc,
    output: InteractiveOutput,
) -> None:
    await output.suspend_tools_for_prompt()
    if interaction.kind == "approval":
        output_func(_subagent_approval_request(interaction))
        return
    question = (
        _format_user_input_request(interaction.input_request)
        if interaction.input_request is not None
        else "Please provide the requested information."
    )
    output_func(f"Question from subagent {_subagent_label(interaction)}: {question}")


async def _resolve_subagent_approval(
    interaction: AgentInteractionView,
    *,
    agents: AgentInteractionHost,
    input_func: InputFunc,
    output_func: OutputFunc,
    output: InteractiveOutput,
    announce: bool,
    initial_response: str | None,
) -> bool:
    if announce:
        await output.suspend_tools_for_prompt()
        output_func(_subagent_approval_request(interaction))
    while True:
        try:
            raw_choice = initial_response
            initial_response = None
            if raw_choice is None:
                raw_choice = await _read_input(
                    input_func,
                    "Approve subagent tool? [y]es/[n]o/[m]odify/[a]bort subagent: ",
                )
            choice = raw_choice.strip().lower()
        except EOFError:
            output_func("")
            return False

        if choice in {"y", "yes"}:
            await _route_subagent_approval(interaction, agents=agents, approved=True)
            return True
        if choice in {"n", "no"}:
            reason = (
                await _input_optional(input_func, output_func, "Reason: ")
            ) or "User rejected"
            await _route_subagent_approval(
                interaction,
                agents=agents,
                approved=False,
                reason=reason,
            )
            return True
        if choice in {"a", "/abort", "abort"}:
            await agents.interrupt_agent(
                interaction.delegation_id,
                reason="User aborted subagent",
            )
            return True
        if choice in {"m", "modify"}:
            override = await _input_json_object(
                input_func,
                output_func,
                "Arguments JSON: ",
            )
            if override is not None:
                await _route_subagent_approval(
                    interaction,
                    agents=agents,
                    approved=True,
                    arguments_override=override,
                )
                return True
            continue
        output_func("Choose y, n, m, or a.")


async def _route_subagent_approval(
    interaction: AgentInteractionView,
    *,
    agents: AgentInteractionHost,
    approved: bool,
    reason: str = "User rejected",
    arguments_override: dict[str, object] | None = None,
) -> None:
    await agents.resolve_agent_approval(
        interaction.delegation_id,
        child_session_id=interaction.child_session_id,
        child_run_id=interaction.child_run_id,
        tool_call_id=interaction.tool_call_id,
        approval_id=interaction.request_id,
        approved=approved,
        reason=reason,
        arguments_override=arguments_override,
    )


async def _resolve_subagent_question(
    interaction: AgentInteractionView,
    *,
    agents: AgentInteractionHost,
    input_func: InputFunc,
    output_func: OutputFunc,
    output: InteractiveOutput,
    announce: bool,
    initial_response: str | None,
) -> bool:
    if announce:
        await output.suspend_tools_for_prompt()
    question = (
        _format_user_input_request(interaction.input_request)
        if interaction.input_request is not None
        else "Please provide the requested information."
    )
    if announce:
        output_func(f"Question from subagent {_subagent_label(interaction)}: {question}")
    while True:
        try:
            answer = initial_response
            initial_response = None
            if answer is None:
                answer = await _read_input(input_func, "Answer subagent: ")
        except EOFError:
            output_func("")
            return False
        if answer.strip():
            break
        output_func("Subagent answer cannot be empty.")
    if answer.strip().lower() in {"/abort", "abort"}:
        await agents.interrupt_agent(
            interaction.delegation_id,
            reason="User aborted subagent",
        )
        return True
    await agents.resolve_agent_input(
        interaction.delegation_id,
        child_session_id=interaction.child_session_id,
        child_run_id=interaction.child_run_id,
        tool_call_id=interaction.tool_call_id,
        request_id=interaction.request_id,
        answer=_decode_user_answer(answer),
    )
    return True


def _durable_run_result(
    runtime: RuntimeService,
    session_id: str,
    run_id: str,
) -> RunResult:
    snapshot = runtime.store.load_runtime_session(session_id)
    run = next((item for item in snapshot.runs if item.run_id == run_id), None)
    if run is None:
        raise OuroError(f"Run not found while waiting for subagents: {run_id}")
    final_text = next(
        (
            message.text_content
            for message in reversed(snapshot.messages)
            if message.run_id == run_id and message.role == "assistant"
        ),
        "",
    )
    return RunResult(session=snapshot.session, run=run, final_text=final_text)


def _interaction_identity(interaction: AgentInteractionView) -> tuple[str, str, str]:
    return (
        interaction.delegation_id,
        interaction.child_run_id,
        interaction.request_id,
    )


def _subagent_approval_request(interaction: AgentInteractionView) -> str:
    tool_name = _single_line(interaction.tool_name)
    invocation = (
        f"{tool_name}({interaction.arguments_preview})"
        if interaction.arguments_preview
        else tool_name
    )
    return f"Subagent {_subagent_label(interaction)} approval required: {invocation}"


def _subagent_label(interaction: AgentInteractionView) -> str:
    return f"{_single_line(interaction.agent_name)} ({_single_line(interaction.delegation_id)})"


def _subagent_input_prompt(interaction: AgentInteractionView) -> str:
    if interaction.kind == "approval":
        return "Approve subagent tool? [y]es/[n]o/[m]odify/[a]bort subagent: "
    return "Answer subagent: "


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
            choice = (
                await _read_input(input_func, "Approve? [y]es/[n]o/[m]odify: ")
            ).strip().lower()
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
            reason = (
                await _input_optional(input_func, output_func, "Reason: ")
            ) or "User rejected"
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
            override = await _input_json_object(
                input_func,
                output_func,
                "Arguments JSON: ",
            )
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
    waiting_call = _pending_user_call(result)
    try:
        request = (
            parse_builtin_plan_confirmation(
                waiting_call.invocation,
                waiting_call.input_request.data,
            )
            if waiting_call is not None and waiting_call.input_request is not None
            else None
        )
    except ValueError as exc:
        output_func(f"Invalid plan confirmation request: {exc}")
        request = None
    if request is not None:
        collected = await asyncio.to_thread(
            collect_plan_confirmation,
            request,
            input_func=input_func,
            output_func=output_func,
        )
        if collected is PlanConfirmationControl.EOF:
            output_func("")
            return None
        if collected is PlanConfirmationControl.ABORT:
            return await runtime.abort_run(
                session_id=result.session.session_id,
                run_id=result.run.run_id,
            )
        return await runtime.answer_user(
            session_id=result.session.session_id,
            run_id=result.run.run_id,
            answer=collected,
        )

    output_func(f"Question: {_pending_question(result)}")
    try:
        answer = await _read_input(input_func, "Answer: ")
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
        answer=_decode_user_answer(answer),
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
    waiting = _pending_user_call(result)
    if waiting is not None:
        if waiting.input_request is not None:
            return _format_user_input_request(waiting.input_request.data)
        if waiting.result is not None and waiting.result.content:
            return waiting.result.content
        question = waiting.request.arguments.get("question")
        if question is not None:
            return _format_user_input_request(question)
    raw_question = result.run.metadata.get("pending_question")
    if isinstance(raw_question, str) and raw_question:
        return raw_question
    arguments = _pending_invocation(result).get("arguments")
    if isinstance(arguments, dict) and isinstance(arguments.get("question"), str):
        return arguments["question"]
    return "Please provide the requested information."


def _pending_user_call(result: RunResult) -> ToolBatchCall | None:
    batch = result.run.active_tool_batch
    if batch is None:
        return None
    return next((call for call in batch.calls if call.status == "waiting_user"), None)


def _format_user_input_request(data: object) -> str:
    if isinstance(data, str):
        return data
    return json.dumps(data, ensure_ascii=False, indent=2)


def _decode_user_answer(answer: str) -> JsonValue:
    try:
        return json.loads(answer)
    except json.JSONDecodeError:
        return answer


async def _read_input(input_func: InputFunc, prompt: str) -> str:
    return await asyncio.to_thread(input_func, prompt)


async def _input_optional(
    input_func: InputFunc,
    output_func: OutputFunc,
    prompt: str,
) -> str:
    try:
        return (await _read_input(input_func, prompt)).strip()
    except EOFError:
        output_func("")
        return ""


def _single_line(value: str) -> str:
    return value.replace("\r", "\\r").replace("\n", "\\n")


async def _input_json_object(
    input_func: InputFunc,
    output_func: OutputFunc,
    prompt: str,
) -> dict[str, object] | None:
    try:
        raw = await _read_input(input_func, prompt)
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

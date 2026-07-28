from __future__ import annotations

import asyncio
import json
from typing import Any, Literal, Protocol

from pydantic import JsonValue

from ouro.application.agent_models import AgentInteractionView, AgentTreeNotification
from ouro.application.runtime import RuntimeService
from ouro.cli.ask_user import (
    AskUserControl,
    ask_user_answer_prompt,
    collect_ask_user_answers,
    render_ask_user_question,
)
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
from ouro.tools.builtin_ask_user import parse_builtin_ask_user_request

_MAX_APPROVAL_ARGUMENT_PREVIEW = 240

# Continuation-progress states for a durable parent Run: a synchronous SpawnAgent
# resolution reschedules the same parent Run, so these are not Host boundaries.
_PARENT_CONTINUATION_STATUSES = frozenset({"queued", "running", "waiting_child"})

_InteractionResolution = Literal["resolved", "stale", "eof"]


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


class InteractionCoordinator:
    """Serialize every terminal interaction for one interactive session.

    The terminal is a single serial device with two concurrent writers: the
    background event sink that renders the primary Run transcript (spinner, tool
    rows, streamed text) and the foreground prompts that collect approvals and
    answers. This coordinator is the sole owner of the foreground side. It:

    * routes every prompt through :meth:`InteractiveOutput.prompt_scope`, which
      pauses animation and buffers transcript events so no spinner frame ever
      overwrites an open prompt line;
    * keeps one shared ``resolved`` set and one long-lived agent-tree
      subscription so the main-prompt path and the ``waiting_child`` follower
      agree on what has already been handled and never double-surface an
      interaction across path transitions;
    * surfaces subagent interactions only while the primary Run is quiescent,
      letting an in-flight model turn stream to completion before the next
      subagent prompt is shown.
    """

    def __init__(
        self,
        *,
        runtime: RuntimeService,
        output: InteractiveOutput,
        input_func: InputFunc,
        output_func: OutputFunc,
        prompt_func: PromptFunc,
        agents: AgentInteractionHost | None,
    ) -> None:
        self._runtime = runtime
        self._output = output
        self._input_func = input_func
        self._output_func = output_func
        self._prompt_func = prompt_func
        self._agents = agents
        # Dedupe key: (delegation_id, child_run_id, request_id). An interaction
        # leaves the pending projection once resolved and never returns with the
        # same request id, so this grows only with the session's total
        # interaction count.
        self._resolved: set[tuple[str, str, str]] = set()
        self._subscription: AgentTreeSubscriptionPort | None = None
        self._subscribed_root: str | None = None

    async def aclose(self) -> None:
        """Release the shared agent-tree subscription held for this session."""

        if self._subscription is not None:
            self._subscription.close()
            self._subscription = None
            self._subscribed_root = None

    @property
    def _host(self) -> AgentInteractionHost:
        """The agent host, asserted present.

        Every subagent-resolution path is entered only after a caller has checked
        that ``self._agents`` is not None, so narrowing here keeps that invariant
        in one place instead of scattering per-call assertions.
        """

        assert self._agents is not None
        return self._agents

    # -- Main prompt ----------------------------------------------------------

    async def read_prompt(self, prompt: str, *, root_session_id: str | None) -> str:
        """Read the primary ``ouro>`` line while keeping subagents observable.

        A blocked terminal read cannot be cancelled, so when a subagent needs
        input while the user is at the main prompt the in-flight read is
        repurposed as the subagent answer instead of being abandoned.
        """

        if self._agents is None or root_session_id is None:
            return await InteractiveInput(self._input_func, self._prompt_func).read(prompt)

        subscription = self._subscription_for(root_session_id)
        terminal_input = InteractiveInput(self._input_func, self._prompt_func)
        input_task: asyncio.Task[str] | None = None
        notification_task: asyncio.Task[AgentTreeNotification] | None = None
        try:
            if not await self._drain_pending_subagents(root_session_id):
                raise EOFError
            input_task = terminal_input.begin(prompt)
            while True:
                pending = self._pending_interactions(root_session_id)
                if pending and not input_task.done():
                    interaction = pending[0]
                    if not await self._repurpose_read_for_subagent(
                        interaction,
                        root_session_id=root_session_id,
                        terminal_input=terminal_input,
                        input_task=input_task,
                    ):
                        raise EOFError
                    if not await self._drain_pending_subagents(root_session_id):
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

    async def _repurpose_read_for_subagent(
        self,
        interaction: AgentInteractionView,
        *,
        root_session_id: str,
        terminal_input: InteractiveInput,
        input_task: asyncio.Task[str],
    ) -> bool:
        """Turn the open main-prompt read into the answer for ``interaction``.

        Returns ``False`` on EOF. The whole handoff runs inside one prompt lease
        so the announcement, the replaced prompt, and the resolution all own the
        terminal exclusively.
        """

        async with self._output.transient_prompt_scope():
            terminal_input.break_prompt()
            self._announce_subagent(interaction)
            terminal_input.replace_prompt(_subagent_input_prompt(interaction))
            answer = await terminal_input.finish(input_task)
            resolution = await self._commit_subagent(
                interaction,
                root_session_id=root_session_id,
                announce=False,
                initial_response=answer,
            )
        return resolution != "eof"

    # -- Run-result state machine --------------------------------------------

    async def resolve_run(self, result: RunResult, *, prefetched_line: str | None = None) -> str:
        """Drive a RunResult to a terminal state, collecting any interactions.

        ``prefetched_line`` feeds the first interaction prompt with a line that
        was already read at the main prompt, so the user's keystroke is not lost
        when a completed background run turns out to need input.
        """

        input_func: InputFunc = (
            PrefetchedInput(prefetched_line, self._input_func)
            if prefetched_line is not None
            else self._input_func
        )
        current = result
        while True:
            status = current.run.status
            if status == "completed":
                if not await self._output.finish_run(current.run.run_id) and current.final_text:
                    self._output_func(current.final_text)
                return current.session.session_id
            if status == "waiting_approval":
                resolved = await self._resolve_run_approval(current, input_func)
                if resolved is None:
                    return current.session.session_id
                current = resolved
                continue
            if status == "waiting_user":
                resolved = await self._resolve_run_question(current, input_func)
                if resolved is None:
                    return current.session.session_id
                current = resolved
                continue
            if status == "waiting_child" and self._agents is not None:
                resolved = await self._follow_waiting_subagents(current, input_func)
                if resolved is None:
                    return current.session.session_id
                current = resolved
                continue
            if status == "failed":
                await self._output.finish_run(current.run.run_id)
                self._output_func(f"Run failed: {current.run.error or 'unknown error'}")
                return current.session.session_id
            if status == "aborted":
                await self._output.finish_run(current.run.run_id)
                self._output_func(f"Run aborted: {current.run.error or 'User aborted'}")
                return current.session.session_id
            if status == "paused":
                await self._output.finish_run(current.run.run_id)
                self._output_func(_paused_run_message(current))
                return current.session.session_id
            self._output_func(f"Run stopped with status: {status}")
            return current.session.session_id

    async def _follow_waiting_subagents(
        self,
        result: RunResult,
        input_func: InputFunc,
    ) -> RunResult | None:
        """Wait out a ``waiting_child`` parent Run, surfacing subagent prompts.

        Option B ordering: a subagent interaction is surfaced only while the
        parent is quiescent (``waiting_child``). When resolving one causes the
        parent to resume a model turn (``queued``/``running``), that turn is left
        to stream to completion; the loop simply waits for the next tree
        notification and re-evaluates.
        """

        root_session_id = result.session.session_id
        root_run_id = result.run.run_id
        subscription = self._subscription_for(root_session_id)
        while True:
            current = self._runtime.sessions.run_result(root_session_id, root_run_id)
            if current.run.status not in _PARENT_CONTINUATION_STATUSES:
                return current

            pending = self._pending_interactions(root_session_id)
            if current.run.status == "waiting_child" and pending:
                resolution = await self._commit_subagent(
                    pending[0],
                    root_session_id=root_session_id,
                    announce=True,
                    initial_response=None,
                    input_func=input_func,
                )
                if resolution == "eof":
                    return None
                continue

            try:
                await subscription.get()
            except StopAsyncIteration as exc:
                raise OuroError(
                    "Subagent interaction subscription closed while the parent Run is waiting"
                ) from exc

    # -- Subagent interaction resolution -------------------------------------

    def _subscription_for(self, root_session_id: str) -> AgentTreeSubscriptionPort:
        """Return the session's shared subscription, (re)opening it if needed."""

        if self._subscription is not None and self._subscribed_root == root_session_id:
            return self._subscription
        if self._subscription is not None:
            self._subscription.close()
        self._subscription = self._host.subscribe_agent_tree(root_session_id)
        self._subscribed_root = root_session_id
        return self._subscription

    def _pending_interactions(self, root_session_id: str) -> tuple[AgentInteractionView, ...]:
        interactions = self._host.list_agent_interactions(root_session_id)
        canonical_identities = {_interaction_identity(interaction) for interaction in interactions}
        self._resolved.intersection_update(canonical_identities)
        return tuple(
            interaction
            for interaction in interactions
            if _interaction_identity(interaction) not in self._resolved
        )

    async def _drain_pending_subagents(self, root_session_id: str) -> bool:
        """Resolve every currently pending subagent interaction, oldest first.

        Returns ``False`` on EOF. Each interaction is resolved inside its own
        prompt lease so buffered transcript events replay between prompts.
        """

        while True:
            pending = self._pending_interactions(root_session_id)
            if not pending:
                return True
            resolution = await self._commit_subagent(
                pending[0],
                root_session_id=root_session_id,
                announce=True,
                initial_response=None,
            )
            if resolution == "eof":
                return False

    async def _commit_subagent(
        self,
        interaction: AgentInteractionView,
        *,
        root_session_id: str,
        announce: bool,
        initial_response: str | None,
        input_func: InputFunc | None = None,
    ) -> _InteractionResolution:
        """Resolve one subagent interaction and record it as handled.

        A ``ValueError`` from the host means the interaction no longer matches the
        canonical child-run state. If it has also dropped out of the pending
        projection it was resolved elsewhere (``stale``); otherwise the mismatch
        is a real fault and is surfaced.
        """

        identity = _interaction_identity(interaction)
        try:
            handled = await self._resolve_subagent_interaction(
                interaction,
                input_func or self._input_func,
                announce=announce,
                initial_response=initial_response,
            )
        except ValueError as exc:
            current = {
                _interaction_identity(item)
                for item in self._host.list_agent_interactions(root_session_id)
            }
            if identity not in current:
                return "stale"
            raise OuroError(
                "Could not resolve subagent interaction "
                f"{interaction.delegation_id}/{interaction.request_id}: {exc}"
            ) from exc
        if not handled:
            return "eof"
        self._resolved.add(identity)
        return "resolved"

    async def _resolve_subagent_interaction(
        self,
        interaction: AgentInteractionView,
        input_func: InputFunc,
        *,
        announce: bool,
        initial_response: str | None,
    ) -> bool:
        if interaction.kind == "approval":
            return await self._resolve_subagent_approval(
                interaction,
                input_func,
                announce=announce,
                initial_response=initial_response,
            )
        return await self._resolve_subagent_question(
            interaction,
            input_func,
            announce=announce,
            initial_response=initial_response,
        )

    def _announce_subagent(self, interaction: AgentInteractionView) -> None:
        """Print the interaction header. Caller must already hold a prompt lease."""

        if interaction.kind == "approval":
            self._output_func(_subagent_approval_request(interaction))
            return
        request = (
            parse_builtin_ask_user_request(interaction.tool_name, interaction.input_request)
            if interaction.input_request is not None
            else None
        )
        if request is not None:
            rendered = render_ask_user_question(
                request.questions[0],
                index=0,
                total=len(request.questions),
            )
            self._output_func(
                f"Question from subagent {_subagent_label(interaction)}:\n{rendered}"
            )
            return
        question = (
            _format_user_input_request(interaction.input_request)
            if interaction.input_request is not None
            else "Please provide the requested information."
        )
        self._output_func(f"Question from subagent {_subagent_label(interaction)}: {question}")

    async def _resolve_subagent_approval(
        self,
        interaction: AgentInteractionView,
        input_func: InputFunc,
        *,
        announce: bool,
        initial_response: str | None,
    ) -> bool:
        approved: bool | None = None
        reason = "User rejected"
        arguments_override: dict[str, object] | None = None
        abort = False

        async with self._output.transient_prompt_scope():
            if announce:
                self._output_func(_subagent_approval_request(interaction))
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
                    self._output_func("")
                    return False

                if choice in {"y", "yes"}:
                    approved = True
                    break
                if choice in {"n", "no"}:
                    reason = (
                        await _input_optional(input_func, self._output_func, "Reason: ")
                    ) or "User rejected"
                    approved = False
                    break
                if choice in {"a", "/abort", "abort"}:
                    abort = True
                    break
                if choice in {"m", "modify"}:
                    override = await _input_json_object(
                        input_func,
                        self._output_func,
                        "Arguments JSON: ",
                    )
                    if override is not None:
                        approved = True
                        arguments_override = override
                        break
                    continue
                self._output_func("Choose y, n, m, or a.")

        if abort:
            await self._host.interrupt_agent(
                interaction.delegation_id,
                reason="User aborted subagent",
            )
            return True
        assert approved is not None
        await self._route_subagent_approval(
            interaction,
            approved=approved,
            reason=reason,
            arguments_override=arguments_override,
        )
        return True

    async def _resolve_subagent_question(
        self,
        interaction: AgentInteractionView,
        input_func: InputFunc,
        *,
        announce: bool,
        initial_response: str | None,
    ) -> bool:
        abort = False
        answer: JsonValue = None

        async with self._output.transient_prompt_scope():
            if announce:
                self._announce_subagent(interaction)
            request = (
                parse_builtin_ask_user_request(interaction.tool_name, interaction.input_request)
                if interaction.input_request is not None
                else None
            )
            if request is not None:
                collected = await asyncio.to_thread(
                    collect_ask_user_answers,
                    request,
                    input_func=input_func,
                    output_func=self._output_func,
                    initial_response=initial_response,
                    first_question_rendered=True,
                )
                if collected is AskUserControl.EOF:
                    self._output_func("")
                    return False
                if collected is AskUserControl.ABORT:
                    abort = True
                else:
                    answer = collected
            else:
                while True:
                    try:
                        raw_answer = initial_response
                        initial_response = None
                        if raw_answer is None:
                            raw_answer = await _read_input(input_func, "Answer subagent: ")
                    except EOFError:
                        self._output_func("")
                        return False
                    if raw_answer.strip():
                        break
                    self._output_func("Subagent answer cannot be empty.")
                if raw_answer.strip().lower() in {"/abort", "abort"}:
                    abort = True
                else:
                    answer = _decode_user_answer(raw_answer)

        if abort:
            await self._host.interrupt_agent(
                interaction.delegation_id,
                reason="User aborted subagent",
            )
            return True
        await self._host.resolve_agent_input(
            interaction.delegation_id,
            child_session_id=interaction.child_session_id,
            child_run_id=interaction.child_run_id,
            tool_call_id=interaction.tool_call_id,
            request_id=interaction.request_id,
            answer=answer,
        )
        return True

    async def _route_subagent_approval(
        self,
        interaction: AgentInteractionView,
        *,
        approved: bool,
        reason: str = "User rejected",
        arguments_override: dict[str, object] | None = None,
    ) -> None:
        await self._host.resolve_agent_approval(
            interaction.delegation_id,
            child_session_id=interaction.child_session_id,
            child_run_id=interaction.child_run_id,
            tool_call_id=interaction.tool_call_id,
            approval_id=interaction.request_id,
            approved=approved,
            reason=reason,
            arguments_override=arguments_override,
        )

    # -- Primary Run interaction resolution ----------------------------------

    async def _resolve_run_approval(
        self,
        result: RunResult,
        input_func: InputFunc,
    ) -> RunResult | None:
        approved: bool | None = None
        reason = "User rejected"
        arguments_override: dict[str, object] | None = None
        abort = False

        async with self._output.transient_prompt_scope():
            invocation = _pending_invocation(result)
            self._output_func(_approval_request(invocation))
            while True:
                try:
                    choice = (
                        (await _read_input(input_func, "Approve? [y]es/[n]o/[m]odify: "))
                        .strip()
                        .lower()
                    )
                except EOFError:
                    self._output_func("")
                    return None

                if choice in {"y", "yes"}:
                    approved = True
                    break
                if choice in {"", "n", "no"}:
                    reason = (
                        await _input_optional(input_func, self._output_func, "Reason: ")
                    ) or "User rejected"
                    approved = False
                    break
                if choice in {"/abort", "abort"}:
                    abort = True
                    break
                if choice in {"m", "modify"}:
                    override = await _input_json_object(
                        input_func,
                        self._output_func,
                        "Arguments JSON: ",
                    )
                    if override is not None:
                        approved = True
                        arguments_override = override
                        break
                    continue
                self._output_func("Choose y, n, or m.")

        if abort:
            return await self._runtime.abort_run(
                session_id=result.session.session_id,
                run_id=result.run.run_id,
            )
        assert approved is not None
        return await self._runtime.approve_tool(
            session_id=result.session.session_id,
            run_id=result.run.run_id,
            approved=approved,
            reason=reason,
            arguments_override=arguments_override,
        )

    async def _resolve_run_question(
        self,
        result: RunResult,
        input_func: InputFunc,
    ) -> RunResult | None:
        abort = False
        answer: JsonValue = None

        async with self._output.transient_prompt_scope():
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
                self._output_func(f"Invalid plan confirmation request: {exc}")
                request = None
            if request is not None:
                collected = await asyncio.to_thread(
                    collect_plan_confirmation,
                    request,
                    input_func=input_func,
                    output_func=self._output_func,
                )
                if collected is PlanConfirmationControl.EOF:
                    self._output_func("")
                    return None
                if collected is PlanConfirmationControl.ABORT:
                    abort = True
                else:
                    answer = collected
            else:
                try:
                    ask_user_request = (
                        parse_builtin_ask_user_request(
                            waiting_call.request.name,
                            waiting_call.input_request.data,
                        )
                        if waiting_call is not None and waiting_call.input_request is not None
                        else None
                    )
                except ValueError as exc:
                    self._output_func(f"Invalid AskUser request: {exc}")
                    ask_user_request = None
                if ask_user_request is not None:
                    ask_user_answer = await asyncio.to_thread(
                        collect_ask_user_answers,
                        ask_user_request,
                        input_func=input_func,
                        output_func=self._output_func,
                    )
                    if ask_user_answer is AskUserControl.EOF:
                        self._output_func("")
                        return None
                    if ask_user_answer is AskUserControl.ABORT:
                        abort = True
                    else:
                        answer = ask_user_answer
                else:
                    self._output_func(f"Question: {_pending_question(result)}")
                    try:
                        raw_answer = await _read_input(input_func, "Answer: ")
                    except EOFError:
                        self._output_func("")
                        return None
                    if raw_answer.strip().lower() in {"/abort", "abort"}:
                        abort = True
                    else:
                        answer = _decode_user_answer(raw_answer)

        if abort:
            return await self._runtime.abort_run(
                session_id=result.session.session_id,
                run_id=result.run.run_id,
            )
        return await self._runtime.answer_user(
            session_id=result.session.session_id,
            run_id=result.run.run_id,
            answer=answer,
        )


def _paused_run_message(result: RunResult) -> str:
    pause = result.run.metadata.get("pause") or {}
    reason = pause.get("reason") if isinstance(pause, dict) else None
    kind = pause.get("kind") if isinstance(pause, dict) else None
    boundary = (
        "for context maintenance" if kind == "context_maintenance" else "at a soft budget boundary"
    )
    return f"Run paused {boundary}: {reason or 'configured limit'}"


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
    request = (
        parse_builtin_ask_user_request(interaction.tool_name, interaction.input_request)
        if interaction.input_request is not None
        else None
    )
    if request is not None:
        return ask_user_answer_prompt(request.questions[0])
    return "Answer subagent: "


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

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ouro.application.harness import capture_session_harness
from ouro.application.plugin_models import PluginCommandExecutionView
from ouro.application.queued_inputs import QueuedInput, QueuedInputQueue
from ouro.application.session_controls import SessionControlCoordinator
from ouro.application.supervisor_models import HostInputReceipt
from ouro.application.usage import UsageService
from ouro.core.approvals import SessionApprovalMode
from ouro.core.base import new_id, utc_now
from ouro.core.callbacks import run_blocking
from ouro.core.events import EventRecord
from ouro.core.harness_calls import HarnessCallSnapshot, accept_state_changes
from ouro.core.interactions import SessionInteractionMode
from ouro.core.reasoning import ReasoningRequest
from ouro.core.results import RunResult
from ouro.core.run_drivers import RunDriverBinding
from ouro.core.session_commits import SessionCommit, SessionCommitStep
from ouro.core.session_controls import AttributedSessionInputBatch
from ouro.core.session_titles import fallback_session_title
from ouro.core.state import SessionRecord
from ouro.plugins.registry import PluginCommandRegistry
from ouro.plugins.session_controllers import PluginSessionControllerRegistry
from ouro.sessions import SessionStore

if TYPE_CHECKING:
    from ouro.application.plugins import PluginService
    from ouro.application.runtime import RuntimeService


class AmbiguousPluginCommandError(ValueError):
    """A short command name resolves to more than one contribution."""


@dataclass(frozen=True)
class PluginCommandSubmission:
    session_id: str | None
    execution: PluginCommandExecutionView

    @property
    def has_inputs(self) -> bool:
        return bool(
            self.execution.accepted_inputs
            or self.execution.session_input_batches
            or self.execution.session_inputs
        )


async def submit_plugin_command(
    plugins: PluginService,
    runtime: RuntimeService,
    selector: str,
    argument: str,
    *,
    session_id: str | None,
    workspace: str | None = None,
    initial_model: str | None = None,
    reasoning_request: ReasoningRequest | None = None,
    approval_mode: SessionApprovalMode = "inherit",
    interaction_mode: SessionInteractionMode = "interactive",
    on_session_created: Callable[[str], None] | None = None,
) -> PluginCommandSubmission | None:
    """Resolve and execute a command, preserving its target before input consumption.

    Stops settle before returning. Hosts may consume new input or continuation in
    their own foreground/background task without interpreting plugin semantics.
    """

    matches = [
        item
        for item in plugins.list_commands()
        if (item.command_id if ":" in selector else item.name) == selector
    ]
    if not matches:
        return None
    if len(matches) > 1:
        choices = ", ".join(f"/{item.command_id}" for item in matches)
        raise AmbiguousPluginCommandError(
            f"Ambiguous plugin command /{selector}; use one of: {choices}"
        )
    selected = matches[0]
    title = fallback_session_title(argument) or f"/{selected.name}"

    async def create_session(title: str) -> None:
        nonlocal session_id
        created: SessionRecord | None = None

        def persist() -> None:
            nonlocal created
            created = runtime.create_session(
                title=title,
                initial_model=initial_model,
                reasoning_request=reasoning_request,
                approval_mode=approval_mode,
                interaction_mode=interaction_mode,
                workspace=workspace,
            )

        try:
            await run_blocking(persist)
        finally:
            # A cancelled caller still learns the identity of its completed owned write.
            if created is not None:
                session_id = created.session_id
                if on_session_created is not None:
                    on_session_created(session_id)

    if session_id is None and selected.requires_session:
        await create_session(title)
    execution = await plugins.execute_command(
        selected.command_id,
        argument,
        session_id=session_id,
        workspace=workspace,
    )
    if execution.stop_requested_run_id is not None:
        assert session_id is not None
        await runtime.settle_run_abort(
            session_id=session_id,
            run_id=execution.stop_requested_run_id,
        )
    if session_id is None and (execution.session_inputs or execution.session_input_batches):
        first = (
            execution.session_inputs[0]
            if execution.session_inputs
            else execution.session_input_batches[0].inputs[0]
        )
        await create_session(first.title or title)
    return PluginCommandSubmission(session_id=session_id, execution=execution)


async def consume_plugin_command(
    runtime: RuntimeService,
    submission: PluginCommandSubmission,
) -> AsyncIterator[RunResult]:
    """Yield results through existing Runtime acceptance and FIFO boundaries."""

    execution = submission.execution
    if execution.run_action == "continue":
        assert submission.session_id is not None
        result = await runtime.continue_session(session_id=submission.session_id)
        if result is not None:
            yield result
        return
    if execution.accepted_inputs:
        for receipt in execution.accepted_inputs:
            yield await runtime.consume_host_input(receipt)
    elif execution.session_input_batches:
        assert submission.session_id is not None
        for batch in execution.session_input_batches:
            yield await runtime.submit_plugin_input_batch(batch, session_id=submission.session_id)
    else:
        for session_input in execution.session_inputs:
            yield await runtime.submit_plugin_input(session_input, session_id=submission.session_id)


async def execute_plugin_command(
    *,
    commands: PluginCommandRegistry,
    controllers: PluginSessionControllerRegistry,
    store: SessionStore | None,
    command_id: str,
    argument: str,
    session_id: str | None,
    workspace: str | None,
    usage_service: UsageService | None = None,
    captured_harness: tuple[SessionRecord, HarnessCallSnapshot] | None = None,
    select_driver: Callable[[str], RunDriverBinding] | None = None,
) -> PluginCommandExecutionView:
    """Accept one command's owner state, nested controllers and queued inputs together."""
    if captured_harness is None and store is not None and session_id is not None:
        captured_harness = await run_blocking(
            capture_session_harness, store, session_id, usage_service=usage_service
        )
    before, draft = captured_harness if captured_harness is not None else (None, None)
    result = await commands.execute(
        command_id,
        argument,
        session_id=session_id,
        workspace=workspace,
        harness=draft,
    )
    commit_request: SessionCommit | None = None
    controls = []
    receipts = []
    if result.attributed_control_transitions and before is None:
        raise ValueError("plugin command Session control transitions require a target Session")
    if before is not None:
        assert draft is not None and store is not None
        # Resolve every requested algorithm before starting nested controller callbacks.
        # Independent inputs remain separate FIFO units; explicit batches are indivisible.
        batches = result.attributed_session_input_batches or [
            AttributedSessionInputBatch(inputs=(item,)) for item in result.attributed_session_inputs
        ]
        selected_batches = []
        for batch in batches:
            driver_id = batch.inputs[0].driver_id
            if driver_id is not None and select_driver is None:
                raise ValueError("plugin command Driver selection is unavailable")
            driver = select_driver(driver_id) if driver_id is not None and select_driver else None
            selected_batches.append((batch, driver))
        coordinator = SessionControlCoordinator(store)
        draft = accept_state_changes(
            draft, [result.harness_change] if result.harness_change else []
        )
        for transition in result.attributed_control_transitions:
            draft, control = await coordinator.stage(
                transition,
                session=before,
                draft=draft,
                controllers=controllers,
                workspace=workspace,
            )
            controls.append(control)
        after = before
        queue = QueuedInputQueue(store)
        events = []
        for batch, driver in selected_batches:
            batch_id = new_id("batch")
            items = tuple(
                QueuedInput(
                    queue_id=new_id("queued"),
                    queued_at=utc_now(),
                    history_epoch=before.history_epoch,
                    source="plugin",
                    prompt=item.prompt,
                    driver=driver,
                    mode=item.mode,
                    title=item.title,
                    role=item.role,
                    scaffold=item.scaffold,
                    semantic_tag=item.semantic_tag,
                    plugin_id=item.plugin_id,
                    contribution_id=item.contribution_id,
                    generation_id=item.generation_id,
                    execution_contract_hash=item.execution_contract_hash,
                    caused_by_run_id=before.active_run_id,
                    batch_id=batch_id,
                    batch_index=index,
                    batch_size=len(batch.inputs),
                    coalesce_key=batch.coalesce_key,
                )
                for index, item in enumerate(batch.inputs)
            )
            after, superseded = queue.stage_batch(after, items)
            receipts.append(
                HostInputReceipt(
                    session_id=before.session_id,
                    queue_id=items[0].queue_id,
                    accepted_at=items[0].queued_at,
                )
            )
            events.append(
                EventRecord(
                    session_id=before.session_id,
                    run_id=before.active_run_id,
                    event_type=(
                        "session_input_batch_queued"
                        if result.attributed_session_input_batches
                        else "follow_up_input_queued"
                        if items[0].mode == "follow_up"
                        else "steering_input_queued"
                    ),
                    payload={
                        "queue_id": items[0].queue_id,
                        "source": "plugin",
                        "role": items[0].role,
                        "scaffold": items[0].scaffold,
                        "semantic_tag": items[0].semantic_tag,
                        "batch_id": batch_id,
                        "queue_ids": [item.queue_id for item in items],
                        "batch_size": len(items),
                        "mode": items[0].mode,
                        "plugin_id": items[0].plugin_id,
                        "contribution_id": items[0].contribution_id,
                        "coalesce_key": batch.coalesce_key,
                        "superseded_queue_ids": [item.queue_id for item in superseded],
                    },
                )
            )
        if (
            result.harness_change is not None
            or any(item.changed for item in controls)
            or batches
            or result.run_action is not None
        ):
            commit_request = SessionCommit(
                before=before,
                after=after,
                steps=(SessionCommitStep(harness=draft.state),),
                events=tuple(events),
            )
    parts = [result.output] if result.output else []
    parts.extend(item.output for item in controls if item.output)
    data = dict(result.data)
    if controls:
        data["session_controls"] = [item.model_dump(mode="json") for item in controls]
    view = PluginCommandExecutionView(
        command_id=result.command_id,
        run_action=result.run_action,
        output="\n".join(parts),
        data=data,
        command_output=result.output,
        command_data=dict(result.data),
        accepted_inputs=tuple(receipts),
        session_inputs=list(result.attributed_session_inputs) if before is None else [],
        session_input_batches=list(result.attributed_session_input_batches)
        if before is None
        else [],
    )
    if commit_request is not None:
        commit_request = commit_request.model_copy(
            update={"output": {"plugin_command": view.model_dump(mode="json")}}
        )
        try:
            committed = await run_blocking(
                coordinator.commit,
                commit_request,
                released_activation_ids=tuple(
                    identity for item in controls for identity in item.released_activation_ids
                ),
                stop_run_reason=f"Stopped by /{command_id}"
                if result.run_action == "stop"
                else None,
            )
        except asyncio.CancelledError:
            # run_blocking has settled the owned write before cancellation reaches us.
            # Report the accepted command when its durable decision already exists.
            recovered = await run_blocking(
                coordinator.store.load_commit,
                commit_request.before.session_id,
                commit_request.operation_id,
            )
            if recovered is None:
                raise
            committed = recovered
        view = PluginCommandExecutionView.model_validate(committed.output["plugin_command"])
        view = view.model_copy(
            update={"stop_requested_run_id": committed.output.get("stop_requested_run_id")}
        )
        if controls:
            data = dict(view.data)
            data["session_controls"] = [
                coordinator.with_cancellations(item, committed).model_dump(mode="json")
                for item in controls
            ]
            view = view.model_copy(update={"data": data})
    return view

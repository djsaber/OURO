from __future__ import annotations

import asyncio

from ouro.application.harness import capture_session_harness
from ouro.application.plugin_models import PluginCommandExecutionView
from ouro.application.queued_inputs import QueuedInput, QueuedInputQueue
from ouro.application.session_controls import SessionControlCoordinator
from ouro.application.supervisor_models import HostInputReceipt
from ouro.application.usage import UsageService
from ouro.core.base import new_id, utc_now
from ouro.core.callbacks import run_blocking
from ouro.core.events import EventRecord
from ouro.core.harness_calls import accept_state_changes
from ouro.core.session_commits import SessionCommit, SessionCommitStep
from ouro.core.session_controls import AttributedSessionInputBatch
from ouro.plugins.catalog import ExtensionCatalogLease
from ouro.sessions import SessionStore


async def execute_plugin_command(
    *,
    catalog: ExtensionCatalogLease,
    store: SessionStore | None,
    command_id: str,
    argument: str,
    session_id: str | None,
    workspace: str | None,
    usage_service: UsageService | None = None,
) -> PluginCommandExecutionView:
    """Accept one command's owner state, nested controllers and queued inputs together."""
    before, draft = (
        await run_blocking(capture_session_harness, store, session_id, usage_service=usage_service)
        if store is not None and session_id is not None
        else (None, None)
    )
    result = await catalog.commands.execute(
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
        coordinator = SessionControlCoordinator(store)
        draft = accept_state_changes(
            draft, [result.harness_change] if result.harness_change else []
        )
        for transition in result.attributed_control_transitions:
            draft, control = await coordinator.stage(
                transition,
                session=before,
                draft=draft,
                controllers=catalog.session_controllers,
                workspace=workspace,
            )
            controls.append(control)
        # Single inputs remain separate FIFO units; explicit batches remain indivisible.
        batches = result.attributed_session_input_batches or [
            AttributedSessionInputBatch(inputs=(item,)) for item in result.attributed_session_inputs
        ]
        after = before
        queue = QueuedInputQueue(store)
        events = []
        for batch in batches:
            batch_id = new_id("batch")
            items = tuple(
                QueuedInput(
                    queue_id=new_id("queued"),
                    queued_at=utc_now(),
                    history_epoch=before.history_epoch,
                    source="plugin",
                    prompt=item.prompt,
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
        if result.harness_change is not None or any(item.changed for item in controls) or batches:
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
        if controls:
            data = dict(view.data)
            data["session_controls"] = [
                coordinator.with_cancellations(item, committed).model_dump(mode="json")
                for item in controls
            ]
            view = view.model_copy(update={"data": data})
    return view

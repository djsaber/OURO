from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from ouro.application.agents.recovery import AgentRecoveryService
from ouro.application.contracts.execution import HostInputReceipt
from ouro.application.harness import capture_harness
from ouro.application.inputs.queue import QueuedInputQueue, same_input_submission
from ouro.application.inputs.requests import (
    IncomingRunInput,
    InputSessionSettings,
    PreparedUserInput,
    UserInputRequest,
)
from ouro.application.inputs.skills import SkillActivationService
from ouro.application.resources import ResourceService
from ouro.application.runs.lifecycle import maybe_active_run
from ouro.application.session_controls import require_available_controls
from ouro.application.sessions.editing import accept_message_edit, read_edit_boundary
from ouro.application.usage import UsageService
from ouro.core.base import new_id, utc_now
from ouro.core.callbacks import run_blocking
from ouro.core.errors import (
    PersistenceError,
    PromptRejectedError,
    ResourceAttachmentError,
    SessionBusyError,
)
from ouro.core.harness import HarnessState
from ouro.core.harness_calls import HarnessCallSnapshot
from ouro.core.input_content import InputFile, InputFileReference
from ouro.core.inputs import InputRecord, QueuedInput
from ouro.core.prompt_policies import (
    PromptAttachmentMetadata,
    PromptPolicyCall,
    PromptPolicyRegistry,
    PromptSkillActivationMetadata,
)
from ouro.core.reasoning import ReasoningRequest
from ouro.core.redaction import SensitiveValueRedactor
from ouro.core.run_drivers import RunDriverBinding
from ouro.core.session_commits import SessionCommitConflict
from ouro.core.session_controls import HostSessionInput
from ouro.core.session_ports import RuntimeSessionSnapshot, SessionStore
from ouro.core.session_titles import with_session_title
from ouro.core.state import RunRecord, SessionRecord

if TYPE_CHECKING:
    from ouro.application.contracts.base import Diagnostic
    from ouro.application.events import RuntimeEventRecorder
    from ouro.application.models.catalog import SessionModels
    from ouro.application.runtime.extensions import RuntimeCapabilities, RuntimeExtensions
    from ouro.application.sessions.recovery import SessionRecoveryService


class InputAdmissionService:
    """Own input receipts, initial publication, and pre-acceptance policy evaluation."""

    def __init__(
        self,
        store: SessionStore,
        *,
        models: SessionModels,
        queue: QueuedInputQueue,
        events: RuntimeEventRecorder,
        recovery: SessionRecoveryService,
        skill_activations: SkillActivationService,
        refresh_configuration: Callable[[], Awaitable[None]],
        prompt_policies: PromptPolicyRegistry,
        default_workspace: str | None,
        resources: ResourceService | None = None,
        usage_service: UsageService | None = None,
    ) -> None:
        self.store = store
        self.models = models
        self.queued_inputs = queue
        self.event_recorder = events
        self.recovery = recovery
        self.skill_activations = skill_activations
        self.refresh_configuration = refresh_configuration
        self.prompt_policies = prompt_policies
        self.default_workspace = default_workspace
        self.extensions: RuntimeExtensions | None = None
        self.reconcile_agent: (
            Callable[[SessionRecord], Awaitable[tuple[Diagnostic, ...]]] | None
        ) = None
        self.resources = resources
        self.usage = usage_service

    async def accept(self, request: UserInputRequest) -> tuple[HostInputReceipt, SessionRecord]:
        """Return the receipt and its Session from the same durable boundary."""
        session_id = request.session_id or request.new_session_id
        assert session_id is not None
        existing = await run_blocking(self._accepted_submission, session_id, request)
        if existing is not None:
            return existing
        await self.refresh_sources(request)
        if request.session_id is None:
            initial = await self.accept_new(request)
            if not isinstance(initial, PreparedUserInput):
                return initial
            initial.close()
            return _pending_submission(initial.session, initial.item)
        snapshot = await run_blocking(self.recovery.submission_snapshot, session_id)
        session = snapshot.session
        if (
            request.source_history
            and request.source_history.session_id != session.context_source_session_id
        ):
            raise ValueError("Input context belongs to another source session")
        if snapshot.pending_continuation is not None:
            raise SessionBusyError("Session has an unresolved terminal Run continuation")
        active = maybe_active_run(snapshot.runs, history_epoch=session.history_epoch)
        await self.validate_resources(session, request.input_resource_refs, request.attachments)
        if active is not None:
            request.session_settings.apply(session, executing=True)
        try:
            committed, item = await self._accept_existing(snapshot, active, request)
        except PersistenceError, OSError, SessionBusyError:
            # Resolve a lost response or competing retry from the same durable input identity.
            accepted = await run_blocking(self._accepted_submission, session_id, request)
            if accepted is None:
                raise
            return accepted
        if item.queue_id in committed.pending_input_ids:
            return _pending_submission(committed, item)
        # A competing consumer already bound the input to a Run.
        accepted = await run_blocking(self._accepted_submission, session_id, request)
        assert accepted is not None
        return accepted

    async def refresh_sources(self, request: UserInputRequest) -> None:
        if request.skill_invocations:
            await self.skill_activations.refresh(self.extensions)
        if self.extensions is not None:
            await self.extensions.synchronize()

    async def _prepare_initial(
        self,
        request: UserInputRequest,
        input_driver: RunDriverBinding | None,
    ) -> PreparedUserInput:
        selected_model = (
            self.models.resolve_model(request.initial_model)
            if request.initial_model is not None
            else self.models.catalog.default_model()
        )
        selected_reasoning = request.initial_reasoning_request or ReasoningRequest()
        self.models.reasoning_state(selected_model, selected_reasoning)
        candidate_session = SessionRecord(
            session_id=request.new_session_id or new_id("ses"),
            workspace=(
                str(Path(request.workspace).expanduser().resolve())
                if request.workspace is not None
                else self.default_workspace
            ),
            current_model=selected_model.key,
            reasoning_request=selected_reasoning,
            approval_mode=request.approval_mode or "inherit",
            interaction_mode=request.interaction_mode or "interactive",
        )
        candidate_session = with_session_title(candidate_session, request.title)
        if request.attachments:
            if self.resources is None:
                raise ResourceAttachmentError("File inputs require a configured ResourceService")
            await run_blocking(
                self.resources.validate_file_inputs,
                request.attachments,
                candidate_session.workspace,
            )
        return await self._prepare_user_input(
            request,
            candidate_session,
            input_driver,
            HarnessCallSnapshot(
                session_id=candidate_session.session_id,
                harness_version=candidate_session.harness_version,
                history_epoch=candidate_session.history_epoch,
                state=HarnessState(controls=candidate_session.controls),
                usage_tokens={} if self.usage is not None else None,
            ),
        )

    async def _prepare_user_input(
        self,
        request: UserInputRequest,
        session: SessionRecord,
        input_driver: RunDriverBinding | None,
        harness: HarnessCallSnapshot,
    ) -> PreparedUserInput:
        incoming = IncomingRunInput(request, input_driver)
        prospective_run = incoming.new_run(session, history_epoch=harness.history_epoch)
        snapshot = self.extensions.snapshot() if self.extensions is not None else None
        try:
            item = await self._checked_user_input(
                incoming,
                session,
                prospective_run,
                snapshot,
                harness=harness.model_copy(update={"run_id": prospective_run.run_id}),
            )
        except BaseException:
            if snapshot is not None:
                snapshot.close()
            raise
        return PreparedUserInput(session, item, prospective_run, snapshot)

    async def _checked_user_input(
        self,
        incoming: IncomingRunInput,
        session: SessionRecord,
        run: RunRecord,
        catalog: RuntimeCapabilities | None,
        *,
        harness: HarnessCallSnapshot | None = None,
    ) -> QueuedInput:
        item = await self.materialize(incoming, run.history_epoch, catalog)
        await self.evaluate_policies(
            session=session, run=run, inputs=[item], harness=harness, catalog=catalog
        )
        return item.model_copy(
            update={
                "queued_at": utc_now(),
                "prompt_policy_checked": True,
                "prospective_run_id": run.run_id,
                "extension_catalog_id": catalog.catalog_id if catalog is not None else None,
                "extension_generations": tuple(catalog.generation_metadata())
                if catalog is not None
                else (),
            }
        )

    async def materialize(
        self, incoming: IncomingRunInput, history_epoch: int, catalog: RuntimeCapabilities | None
    ) -> QueuedInput:
        skills = (
            await run_blocking(
                self.skill_activations.resolve_user_activations,
                catalog.skills if catalog is not None else self.skill_activations.base_skills,
                incoming.skills,
            )
            if incoming.skills
            else ()
        )
        return incoming.materialize(history_epoch, skills=skills)

    async def _accept_existing(
        self, snapshot: RuntimeSessionSnapshot, active: RunRecord | None, request: UserInputRequest
    ) -> tuple[SessionRecord, QueuedInput]:
        if request.edit is None:
            return await self.enqueue(
                snapshot, active, IncomingRunInput(request), request.session_settings
            )
        before = snapshot.session
        boundary_id, harness = await run_blocking(
            read_edit_boundary, self.store, before, request.edit, self.usage
        )
        prepared = await self._prepare_user_input(request, before, None, harness)
        try:
            committed = await run_blocking(
                accept_message_edit,
                self.store,
                before,
                boundary_id,
                InputRecord(
                    session_id=before.session_id,
                    item=prepared.item,
                    replaces_message_id=request.edit.message_id,
                ),
                request.session_settings,
            )
        finally:
            prepared.close()
        if self.reconcile_agent is not None:
            await self.reconcile_agent(committed.session)
        await self.event_recorder.publish_committed(committed.events)
        return committed.session, prepared.item

    async def enqueue(
        self,
        snapshot: RuntimeSessionSnapshot,
        active_run: RunRecord | None,
        incoming: IncomingRunInput,
        session_settings: InputSessionSettings | None = None,
        *,
        on_input_accepted: Callable[[], None] | None = None,
    ) -> tuple[SessionRecord, QueuedInput]:
        request = incoming.request
        if (
            isinstance(request, HostSessionInput)
            and request.history_epoch is not None
            and request.history_epoch != snapshot.session.history_epoch
        ):
            raise SessionCommitConflict("Host input belongs to withdrawn Session history")
        catalog = None
        try:
            if isinstance(request, UserInputRequest):
                await self.refresh_configuration()
                if self.extensions is not None:
                    await self.extensions.synchronize()
                    catalog = self.extensions.snapshot()
                current = await run_blocking(
                    self.store.load_session_record, snapshot.session.session_id
                )
                if current.session_kind == "subagent":
                    if active_run is None or active_run.kind != "agent":
                        raise PersistenceError(
                            "Child prompt binding requires an active delegated Run"
                        )
                    await run_blocking(
                        AgentRecoveryService(self.store).load_run_delegation, current, active_run
                    )
                prospective = incoming.new_run(snapshot.session)
                if catalog is not None:
                    prospective.metadata["session_control_snapshot"] = (
                        current.controls.run_snapshot(0)
                    )
                queued_input = await self._checked_user_input(
                    incoming,
                    current,
                    prospective,
                    catalog,
                )
            else:
                queued_input = incoming.materialize(
                    snapshot.session.history_epoch, enqueue=True, active=active_run
                )
            session, queued_input, _, events = await run_blocking(
                self.queued_inputs.enqueue,
                session_id=snapshot.session.session_id,
                item=queued_input,
                session_settings=session_settings,
            )
        finally:
            if catalog is not None:
                catalog.close()
        if on_input_accepted is not None:
            on_input_accepted()
        await self.event_recorder.publish_committed(events)
        return session, queued_input

    async def accept_new(
        self, request: UserInputRequest, driver: RunDriverBinding | None = None
    ) -> PreparedUserInput | tuple[HostInputReceipt, SessionRecord]:
        initial = await self._prepare_initial(request, driver)
        try:
            accepted = await run_blocking(self.accept_initial, initial.session, initial.item)
        except BaseException:
            initial.close()
            raise
        if not isinstance(accepted, SessionRecord):
            initial.close()
            return accepted
        initial.session = accepted
        return initial

    async def validate_resources(
        self,
        session: SessionRecord,
        resource_refs: tuple[str, ...],
        attachments: tuple[InputFile, ...] = (),
    ) -> None:
        if not resource_refs and not attachments:
            return
        resources = self.resources
        if resources is None:
            raise ResourceAttachmentError(
                "resource attachments require a configured ResourceService"
            )

        def validate() -> None:
            resources.validate_file_inputs(attachments, session.workspace)
            resources.validate_model_references(session.session_id, resource_refs)

        await run_blocking(validate)

    def receipt(
        self, session_id: str, request: UserInputRequest, *, driver_id: str | None = None
    ) -> HostInputReceipt | None:
        with self.store.session_read(session_id):
            return self._receipt(session_id, request, driver_id=driver_id)

    def _accepted_submission(
        self, session_id: str, request: UserInputRequest
    ) -> tuple[HostInputReceipt, SessionRecord] | None:
        with self.store.session_read(session_id):
            receipt = self._receipt(session_id, request)
            return (receipt, self.store.load_session_record(session_id)) if receipt else None

    def _receipt(
        self, session_id: str, request: UserInputRequest, *, driver_id: str | None = None
    ) -> HostInputReceipt | None:
        try:
            record = self.store.load_input(session_id, request.input_id)
        except FileNotFoundError:
            if request.session_id is not None:
                raise
            return None
        if record is None:
            return None
        item = record.item
        if (
            item.source != "user"
            or (
                driver_id is not None
                and (item.driver is None or item.driver.driver_id != driver_id)
            )
            or item.prompt != request.prompt
            or record.replaces_message_id != (request.edit.message_id if request.edit else None)
            or item.title != request.title
            or item.mode != request.queue_mode
            or item.attachments != request.attachments
            or item.source_history != request.source_history
            or item.resource_refs != request.input_resource_refs
            or [(skill.name, skill.text_offset, skill.order) for skill in item.skill_activations]
            != [(skill.name, skill.text_offset, skill.order) for skill in request.skill_invocations]
        ):
            raise ValueError("Input id is already bound to another input")
        return self._item_receipt(session_id, item)

    def receipt_or_session(
        self, session_id: str, request: UserInputRequest, *, driver_id: str | None = None
    ) -> HostInputReceipt | SessionRecord:
        """Read an existing submission or the Session needed for its first execution."""
        with self.store.session_read(session_id):
            receipt = self._receipt(session_id, request, driver_id=driver_id)
            return receipt if receipt is not None else self.store.load_session_record(session_id)

    def _item_receipt(self, session_id: str, item: QueuedInput) -> HostInputReceipt:
        # Consumption removes the queue item and writes its message atomically.
        # A still-pending input cannot have a canonical message to search for.
        state = self.store.load_input_state(session_id, item.queue_id)
        if state is None:
            raise PersistenceError("accepted input has no lifecycle state")
        if state.status in {"cancelled", "withdrawn"}:
            raise ValueError("Input was already accepted and has been withdrawn or cancelled")
        message = (
            self.store.load_message(session_id, state.message_ids[-1])
            if state.message_ids
            else None
        )
        return HostInputReceipt(
            session_id=session_id,
            queue_id=item.queue_id,
            accepted_at=item.queued_at,
            message_id=message.message_id if message is not None else None,
            run_id=message.run_id if message is not None else None,
        )

    def accept_initial(
        self, session: SessionRecord, item: QueuedInput
    ) -> SessionRecord | tuple[HostInputReceipt, SessionRecord]:
        """Publish the first input, or read its original receipt and head together."""
        proposed = InputRecord(session_id=session.session_id, item=item)
        with self.store.session_mutation(session.session_id):
            try:
                existing = self.store.load_session_record(session.session_id)
            except FileNotFoundError:
                pass
            else:
                record = self.store.load_input(session.session_id, item.queue_id)
                if record is not None:
                    if not same_input_submission(record, proposed):
                        raise ValueError("Input id is already bound to another input")
                    return self._item_receipt(session.session_id, record.item), existing
            references = (
                self.resources.prepared_references(session.session_id)
                if self.resources is not None
                else None
            )
            available = {record.resource_id for record in references or ()}
            if not set(item.resource_refs) <= available:
                raise ResourceAttachmentError("Input contains a foreign prepared resource")
            session = session.model_copy(
                update={
                    "pending_input_ids": (item.queue_id,),
                    "updated_at": utc_now(),
                }
            )
            self.store.create_session_snapshot(
                session,
                [],
                inputs=(proposed,),
                resource_references=references,
                prepared_resources=references is not None,
            )
        self.store.refresh_catalog(session.session_id)
        return session

    async def evaluate_policies(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        inputs: list[QueuedInput],
        catalog: RuntimeCapabilities | None,
        harness: HarnessCallSnapshot | None = None,
    ) -> None:
        controls = harness.state.controls if harness is not None else session.controls
        active: set[str] = set()
        if catalog is not None or harness is not None:
            require_available_controls(
                controls, catalog.session_controllers if catalog is not None else None
            )
            active = {item.controller_id for item in controls.activations}
        policies = catalog.prompt_policies if catalog is not None else self.prompt_policies
        registry = policies.scoped(lambda: active)
        if not registry.registered():
            return
        if harness is None:
            harness = await run_blocking(
                capture_harness,
                self.store,
                session.session_id,
                run=run,
                usage_service=self.usage,
            )
        execution_contracts = catalog.execution_contracts() if catalog is not None else {}
        for item in inputs:
            if item.source != "user":
                continue
            attachments: tuple[PromptAttachmentMetadata, ...] = ()
            if item.resource_refs:
                assert self.resources is not None
                attachments = await run_blocking(
                    _uploaded_metadata, self.resources, session.session_id, item.resource_refs
                )
            attachments += tuple(
                PromptAttachmentMetadata(
                    kind="reference",
                    resource_id=None,
                    media_type=None,
                    size=None,
                    name=file.name,
                    path=file.path,
                )
                for file in item.attachments
                if isinstance(file, InputFileReference)
            )
            # Preserve the single-skill policy contract while accepting the input atomically.
            for activation in item.skill_activations or (None,):
                evaluation = await registry.evaluate(
                    PromptPolicyCall(
                        harness=harness,
                        session_id=session.session_id,
                        prospective_run_id=run.run_id,
                        prompt=item.prompt,
                        input_source=item.source,
                        workspace=session.workspace or self.default_workspace,
                        attachments=attachments,
                        skill_activation=(
                            PromptSkillActivationMetadata(
                                name=activation.name,
                                source=activation.source,
                                location=activation.location,
                            )
                            if activation is not None
                            else None
                        ),
                        catalog_id=catalog.catalog_id if catalog is not None else None,
                        execution_contracts=execution_contracts,
                    )
                )
                if evaluation.allowed:
                    continue
                prompt_redactor = SensitiveValueRedactor((item.prompt,))
                if evaluation.rejected_by is not None:
                    rejected = evaluation.rejected_by
                    raise PromptRejectedError(
                        prompt_redactor.redact(evaluation.reason or "prompt rejected"),
                        plugin_id=rejected.plugin_id,
                        policy_id=rejected.policy_id,
                    )
                failure = evaluation.failures[0]
                raise PromptRejectedError(
                    evaluation.reason or "prompt policy evaluation failed",
                    plugin_id=failure.plugin_id,
                    policy_id=failure.policy_id,
                    policy_failed=True,
                    failure_error_type=failure.error_type,
                    failure_message=prompt_redactor.redact(failure.message),
                )


def _pending_submission(
    session: SessionRecord, item: QueuedInput
) -> tuple[HostInputReceipt, SessionRecord]:
    return HostInputReceipt(
        session_id=session.session_id, queue_id=item.queue_id, accepted_at=item.queued_at
    ), session


def _uploaded_metadata(
    resources: ResourceService, session_id: str, resource_refs: tuple[str, ...]
) -> tuple[PromptAttachmentMetadata, ...]:
    uploads = (
        resources.show_resource(session_id, identity, verify=False) for identity in resource_refs
    )
    return tuple(
        PromptAttachmentMetadata(
            resource_id=item.resource_id,
            media_type=item.media_type,
            size=item.size,
            name=item.source.name,
        )
        for item in uploads
    )

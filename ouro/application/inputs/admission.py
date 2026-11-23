from __future__ import annotations

from collections.abc import Awaitable, Callable

from ouro.application.contracts.execution import HostInputReceipt
from ouro.application.harness import capture_harness
from ouro.application.inputs.queue import same_input_submission
from ouro.application.inputs.requests import PreparedUserInput, UserInputRequest
from ouro.application.resources import ResourceService
from ouro.application.runs.lifecycle import maybe_active_run
from ouro.application.usage import UsageService
from ouro.core.base import utc_now
from ouro.core.callbacks import run_blocking
from ouro.core.errors import (
    PersistenceError,
    PromptRejectedError,
    ResourceAttachmentError,
    SessionBusyError,
)
from ouro.core.harness import HarnessState, HarnessVersion
from ouro.core.harness_calls import HarnessCallSnapshot
from ouro.core.input_content import InputFile, InputFileReference
from ouro.core.inputs import InputRecord, QueuedInput
from ouro.core.prompt_policies import (
    PromptAttachmentMetadata,
    PromptPolicyCall,
    PromptPolicyRegistry,
    PromptSkillActivationMetadata,
)
from ouro.core.redaction import SensitiveValueRedactor
from ouro.core.run_drivers import RunDriverBinding
from ouro.core.session_ports import RuntimeSessionSnapshot, SessionStore
from ouro.core.state import RunRecord, SessionRecord


class InputAdmissionService:
    """Own input receipts, initial publication, and pre-acceptance policy evaluation."""

    def __init__(
        self,
        store: SessionStore,
        *,
        refresh_skills: Callable[[], Awaitable[None]],
        synchronize: Callable[[], Awaitable[None]],
        prepare_initial: Callable[
            [UserInputRequest, RunDriverBinding | None],
            Awaitable[PreparedUserInput],
        ],
        load_target: Callable[[str], Awaitable[RuntimeSessionSnapshot]],
        accept_existing: Callable[
            [RuntimeSessionSnapshot, RunRecord | None, UserInputRequest],
            Awaitable[None],
        ],
        resources: ResourceService | None = None,
        usage_service: UsageService | None = None,
    ) -> None:
        self.store = store
        self.refresh_skills = refresh_skills
        self.synchronize = synchronize
        self.prepare_initial = prepare_initial
        self.load_target = load_target
        self.accept_existing = accept_existing
        self.resources = resources
        self.usage = usage_service

    async def accept(self, request: UserInputRequest) -> tuple[HostInputReceipt, SessionRecord]:
        """Return the receipt and its Session from the same durable boundary."""
        session_id = request.session_id or request.new_session_id
        assert session_id is not None
        existing = await run_blocking(self._accepted_submission, session_id, request)
        if existing is not None:
            return existing
        if request.skill_invocations:
            await self.refresh_skills()
        await self.synchronize()
        if request.session_id is None:
            initial = await self.accept_new(request)
            if isinstance(initial, HostInputReceipt):
                return initial, await run_blocking(self.store.load_session_record, session_id)
            initial.close()
            return HostInputReceipt(
                session_id=session_id,
                queue_id=initial.item.queue_id,
                accepted_at=initial.item.queued_at,
            ), initial.session
        snapshot = await self.load_target(session_id)
        session = snapshot.session
        if (
            request.source_history
            and request.source_history.session_id != session.context_source_session_id
        ):
            raise ValueError("Input context belongs to another source session")
        if snapshot.pending_continuation is not None:
            raise SessionBusyError("Session has an unresolved terminal Run continuation")
        active = maybe_active_run(snapshot.runs, history_epoch=session.history_epoch)
        self.validate_resources(session, request.input_resource_refs, request.attachments)
        if active is not None:
            request.session_settings.apply(session, executing=True)
        try:
            await self.accept_existing(snapshot, active, request)
        except PersistenceError, OSError, SessionBusyError:
            # Resolve a lost response or competing retry from the same durable input identity.
            accepted = await run_blocking(self._accepted_submission, session_id, request)
            if accepted is None:
                raise
            return accepted
        accepted = await run_blocking(self._accepted_submission, session_id, request)
        assert accepted is not None
        return accepted

    async def accept_new(
        self, request: UserInputRequest, driver: RunDriverBinding | None = None
    ) -> PreparedUserInput | HostInputReceipt:
        initial = await self.prepare_initial(request, driver)
        try:
            accepted = await run_blocking(self.accept_initial, initial.session, initial.item)
        except BaseException:
            initial.close()
            raise
        if isinstance(accepted, HostInputReceipt):
            initial.close()
            return accepted
        initial.session = accepted
        return initial

    def validate_resources(
        self,
        session: SessionRecord,
        resource_refs: tuple[str, ...],
        attachments: tuple[InputFile, ...] = (),
    ) -> None:
        if not resource_refs and not attachments:
            return
        if self.resources is None:
            raise ResourceAttachmentError(
                "resource attachments require a configured ResourceService"
            )
        self.resources.validate_file_inputs(attachments, session.workspace)
        self.resources.validate_model_references(session.session_id, resource_refs)

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
    ) -> SessionRecord | HostInputReceipt:
        """Publish the first durable input and adopt its existing files in one boundary."""
        proposed = InputRecord(session_id=session.session_id, item=item)
        with self.store.session_mutation(session.session_id):
            try:
                self.store.load_session_record(session.session_id)
            except FileNotFoundError:
                record = None
            else:
                record = self.store.load_input(session.session_id, item.queue_id)
            if record is not None:
                if not same_input_submission(record, proposed):
                    raise ValueError("Input id is already bound to another input")
                return self._item_receipt(session.session_id, record.item)
            references = (
                self.resources.prepared_references(session.session_id)
                if self.resources is not None
                else None
            )
            available = {record.resource_id for record in references or ()}
            if not set(item.resource_refs) <= available:
                raise ResourceAttachmentError("Input contains a foreign prepared resource")
            accepted = session.model_copy(
                update={
                    "pending_input_ids": (item.queue_id,),
                    "updated_at": utc_now(),
                }
            )
            harness = HarnessVersion(
                session_id=session.session_id,
                version_id=session.harness_version,
                state=HarnessState(controls=session.controls),
            )
            session = accepted
            self.store.create_session_snapshot(
                session,
                [],
                harness_versions=[harness],
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
        registry: PromptPolicyRegistry,
        catalog_id: str | None,
        execution_contracts: dict[str, str],
        default_workspace: str | None,
        harness: HarnessCallSnapshot | None = None,
    ) -> None:
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
        for item in inputs:
            if item.source != "user":
                continue
            attachments: tuple[PromptAttachmentMetadata, ...] = ()
            if item.resource_refs:
                assert self.resources is not None
                attachments = tuple(
                    PromptAttachmentMetadata(
                        resource_id=resource.resource_id,
                        media_type=resource.media_type,
                        size=resource.size,
                        name=resource.source.name,
                    )
                    for resource in (
                        self.resources.show_resource(
                            session.session_id,
                            resource_id,
                            verify=False,
                        )
                        for resource_id in item.resource_refs
                    )
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
                        workspace=session.workspace or default_workspace,
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
                        catalog_id=catalog_id,
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

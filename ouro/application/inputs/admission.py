from __future__ import annotations

from ouro.application.contracts.execution import HostInputReceipt
from ouro.application.harness import capture_harness
from ouro.application.inputs.queue import same_queued_input
from ouro.application.inputs.requests import UserInputRequest
from ouro.application.resources import ResourceService
from ouro.application.usage import UsageService
from ouro.core.base import utc_now
from ouro.core.callbacks import run_blocking
from ouro.core.errors import PersistenceError, PromptRejectedError, ResourceAttachmentError
from ouro.core.harness import HarnessState, HarnessVersion
from ouro.core.harness_calls import HarnessCallSnapshot
from ouro.core.input_content import InputFileReference
from ouro.core.inputs import InputRecord, QueuedInput
from ouro.core.prompt_policies import (
    PromptAttachmentMetadata,
    PromptPolicyCall,
    PromptPolicyRegistry,
    PromptSkillActivationMetadata,
)
from ouro.core.redaction import SensitiveValueRedactor
from ouro.core.session_ports import SessionStore
from ouro.core.state import RunRecord, SessionRecord


class InputAdmissionService:
    """Own input receipts, initial publication, and pre-acceptance policy evaluation."""

    def __init__(
        self,
        store: SessionStore,
        *,
        resources: ResourceService | None = None,
        usage_service: UsageService | None = None,
    ) -> None:
        self.store = store
        self.resources = resources
        self.usage = usage_service

    def receipt(self, session_id: str, request: UserInputRequest) -> HostInputReceipt | None:
        with self.store.session_read(session_id):
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
                or item.prompt != request.prompt
                or item.title != request.title
                or item.mode != request.queue_mode
                or item.attachments != request.attachments
                or item.resource_refs != request.input_resource_refs
                or [
                    (skill.name, skill.text_offset, skill.order) for skill in item.skill_activations
                ]
                != [
                    (skill.name, skill.text_offset, skill.order)
                    for skill in request.skill_invocations
                ]
            ):
                raise ValueError("Input id is already bound to another input")
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

    def accept_initial(self, session: SessionRecord, item: QueuedInput) -> SessionRecord:
        """Publish the first durable input and adopt its existing files in one boundary."""
        with self.store.session_mutation(session.session_id):
            try:
                self.store.load_session_record(session.session_id)
            except FileNotFoundError:
                record = None
            else:
                record = self.store.load_input(session.session_id, item.queue_id)
            if record is not None:
                if not same_queued_input(record.item, item):
                    raise ValueError("Input id is already bound to another input")
                state = self.store.load_input_state(session.session_id, item.queue_id)
                if state is None:
                    raise PersistenceError("accepted input has no lifecycle state")
                if state.status in {"cancelled", "withdrawn"}:
                    raise ValueError(
                        "Input was already accepted and has been withdrawn or cancelled"
                    )
                return self.store.load_session_record(session.session_id)
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
                inputs=(InputRecord(session_id=session.session_id, item=item),),
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
                                content_digest=activation.content_digest,
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

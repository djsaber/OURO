"""Adapt SDK policy callbacks to core policy interfaces."""

from __future__ import annotations

from types import MappingProxyType

from ouro.core.continuations import ContinuationDecision, ContinuationPolicyRegistration
from ouro.core.prompt_policies import (
    PromptAttachmentMetadata,
    PromptPolicyCall,
    PromptPolicyDecision,
    PromptPolicyRegistration,
)
from ouro.core.session_controls import SessionControlTransitionRequest
from ouro.core.tool_policies import ToolPolicyCall, ToolPolicyDecision, ToolPolicyRegistration
from ouro.core.turn_policies import (
    TurnCompletionCandidate,
    TurnPolicyDecision,
    TurnPolicyRegistration,
)
from ouro.plugins.harness_calls import invoke_plugin_callback
from ouro.sdk.plugins import v5 as sdk


def _adapt_continuation_policy(
    contribution: sdk.ContinuationPolicyContribution,
    *,
    owner_id: str,
) -> ContinuationPolicyRegistration:
    async def handler(outcome) -> ContinuationDecision:
        sdk_outcome = sdk.RunOutcome(
            session_id=outcome.session_id,
            run_id=outcome.run_id,
            status=outcome.status,
            origin=outcome.origin,
            final_text=outcome.final_text,
            error=outcome.error,
            usage=MappingProxyType(dict(outcome.usage)),
            tool_call_count=outcome.tool_call_count,
            automatic_continuation=outcome.automatic_continuation,
        )
        raw, change = await invoke_plugin_callback(
            contribution.handler,
            sdk_outcome,
            snapshot=outcome.harness,
            owner_id=owner_id,
            writable=True,
        )
        if not isinstance(raw, sdk.ContinuationDecision):
            raise TypeError("continuation policy must return ContinuationDecision")
        return ContinuationDecision(
            harness_change=change,
            action=raw.action,
            prompt=raw.prompt,
            mode=raw.mode,
            title=raw.title,
            role=raw.role,
            scaffold=raw.scaffold,
            semantic_tag=raw.semantic_tag,
            coalesce_key=raw.coalesce_key,
            reason=raw.reason,
            session_control_transitions=[
                SessionControlTransitionRequest(
                    action=item.action,
                    controller_id=item.controller_id,
                    activation_id=item.activation_id,
                    replace_activation_ids=list(item.replace_activation_ids),
                    expected_session_revision=item.expected_session_revision,
                    expected_activation_revision=item.expected_activation_revision,
                    payload=item.payload,
                    reason=item.reason,
                )
                for item in raw.session_control_transitions
            ],
        )

    return ContinuationPolicyRegistration(
        plugin_id="",
        policy_id=contribution.id,
        handler=handler,
        priority=contribution.priority,
        timeout_seconds=contribution.timeout_seconds,
        active_when_controller_id=contribution.active_when,
    )


def _adapt_tool_policy(
    contribution: sdk.ToolPolicyContribution,
    *,
    owner_id: str,
) -> ToolPolicyRegistration:
    async def handler(call: ToolPolicyCall) -> ToolPolicyDecision:
        raw, change = await invoke_plugin_callback(
            contribution.handler,
            sdk.ToolPolicyCall(
                session_id=call.session_id,
                run_id=call.run_id,
                tool_call_id=call.tool_call_id,
                tool_name=call.tool_name,
                canonical_tool_id=call.canonical_tool_id,
                owner_id=call.owner_id,
                arguments=MappingProxyType(dict(call.arguments)),
                source=call.source,
                effect=call.effect,
                batch_behavior=call.batch_behavior,
                permission_control=call.permission_control,
                workspace=call.workspace,
            ),
            snapshot=call.harness,
            owner_id=owner_id,
            writable=False,
        )
        if not isinstance(raw, sdk.ToolPolicyDecision):
            raise TypeError("tool policy must return ToolPolicyDecision")
        return ToolPolicyDecision(action=raw.action, reason=raw.reason)

    return ToolPolicyRegistration(
        plugin_id="",
        policy_id=contribution.id,
        handler=handler,
        priority=contribution.priority,
        timeout_seconds=contribution.timeout_seconds,
        active_when_controller_id=contribution.active_when,
    )


def _adapt_prompt_policy(
    contribution: sdk.PromptPolicyContribution,
    *,
    owner_id: str,
) -> PromptPolicyRegistration:
    async def handler(call: PromptPolicyCall) -> PromptPolicyDecision:
        raw, change = await invoke_plugin_callback(
            contribution.handler,
            sdk.PromptPolicyCall(
                session_id=call.session_id,
                prospective_run_id=call.prospective_run_id,
                prompt=call.prompt,
                input_source=call.input_source,
                workspace=call.workspace,
                attachments=tuple(
                    sdk.PromptAttachmentMetadata(
                        resource_id=item.resource_id,
                        media_type=item.media_type,
                        size=item.size,
                        kind=item.kind,
                        name=item.name,
                        path=item.path,
                    )
                    for item in call.attachments
                    if isinstance(item, PromptAttachmentMetadata)
                ),
                skill_activation=(
                    sdk.PromptSkillActivationMetadata(
                        name=call.skill_activation.name,
                        source=call.skill_activation.source,
                        location=call.skill_activation.location,
                        content_digest=call.skill_activation.content_digest,
                    )
                    if call.skill_activation is not None
                    else None
                ),
                catalog_id=call.catalog_id,
                execution_contracts=MappingProxyType(dict(call.execution_contracts)),
            ),
            snapshot=call.harness,
            owner_id=owner_id,
            writable=False,
        )
        if not isinstance(raw, sdk.PromptPolicyDecision):
            raise TypeError("prompt policy must return PromptPolicyDecision")
        return PromptPolicyDecision(action=raw.action, reason=raw.reason)

    return PromptPolicyRegistration(
        plugin_id="",
        policy_id=contribution.id,
        handler=handler,
        priority=contribution.priority,
        timeout_seconds=contribution.timeout_seconds,
        active_when_controller_id=contribution.active_when,
    )


def _adapt_turn_policy(
    contribution: sdk.TurnPolicyContribution,
    *,
    owner_id: str,
) -> TurnPolicyRegistration:
    async def handler(candidate: TurnCompletionCandidate) -> TurnPolicyDecision:
        raw, change = await invoke_plugin_callback(
            contribution.handler,
            sdk.TurnCompletionCall(
                session_id=candidate.session_id,
                run_id=candidate.run_id,
                turn_id=candidate.turn_id,
                assistant_text=candidate.assistant_text,
                assistant_reasoning=candidate.assistant_reasoning,
            ),
            snapshot=candidate.harness,
            owner_id=owner_id,
            writable=False,
        )
        if not isinstance(raw, sdk.TurnPolicyDecision):
            raise TypeError("turn policy must return TurnPolicyDecision")
        return TurnPolicyDecision(action=raw.action, feedback=raw.feedback)

    return TurnPolicyRegistration(
        plugin_id="",
        policy_id=contribution.id,
        handler=handler,
        priority=contribution.priority,
        timeout_seconds=contribution.timeout_seconds,
        active_when_controller_id=contribution.active_when,
    )

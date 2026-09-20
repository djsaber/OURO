from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ouro.application.runtime import RuntimeService
from ouro.application.skill_activations import SkillActivationService, SkillInvocationRequest
from ouro.core.adapter import RuntimeAdapter
from ouro.core.errors import SkillActivationError
from ouro.core.messages import ModelMessage, SkillActivationContent
from ouro.core.prompt_policies import (
    PromptPolicyCall,
    PromptPolicyDecision,
    PromptPolicyRegistration,
    PromptPolicyRegistry,
)
from ouro.core.provider import ModelMetadata, ProviderStreamEvent
from ouro.sessions import LocalSessionStore
from ouro.skills import SkillLoader
from ouro.tools import ToolRegistry


class CapturingProvider:
    provider_id = "capture"

    def __init__(self, *, block_first: bool = False) -> None:
        self.block_first = block_first
        self.calls: list[list[ModelMessage]] = []
        self.system_prompts: list[str] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(self, *, model, system_prompt="", messages, tools=None, request_context=None):
        del model, tools
        self.calls.append(list(messages))
        self.system_prompts.append(system_prompt)
        if self.block_first and len(self.calls) == 1:
            self.started.set()
            await self.release.wait()
        yield ProviderStreamEvent(type="text_delta", text="done")
        yield ProviderStreamEvent(type="done")


def _write_skill(
    root: Path,
    *,
    description: str = "Review source code.",
    body: str = "Review the requested code carefully.",
    extra_frontmatter: str = "",
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text(
        f"""---
name: reviewer
description: {description}
{extra_frontmatter}---
{body}
""",
        encoding="utf-8",
    )


def _metadata(root: Path):
    return SkillLoader(root.parent).load_metadata()[0]


def _runtime(tmp_path: Path, provider: CapturingProvider, metadata, *, policies=None):
    store = LocalSessionStore(tmp_path / "sessions")
    store.initialize()
    return RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="capture", provider_id="capture"),
        ),
        tools=ToolRegistry(),
        skills=(metadata,),
        prompt_policies=policies,
    )


def test_skill_activation_service_enforces_user_invocation_and_exact_catalog_metadata(
    tmp_path,
) -> None:
    root = tmp_path / "skills" / "reviewer"
    _write_skill(root, extra_frontmatter="disable-model-invocation: true\n")
    metadata = _metadata(root)
    service = SkillActivationService()

    activation = service.resolve_user_activation(
        (metadata,),
        SkillInvocationRequest(name="reviewer", arguments="target.py"),
    )

    assert activation.name == "reviewer"
    assert activation.instructions == "Review the requested code carefully."
    assert activation.content_digest.startswith("sha256:")
    assert metadata.disable_model_invocation is True

    disabled = metadata.model_copy(update={"enabled": False})
    with pytest.raises(SkillActivationError) as disabled_error:
        service.resolve_user_activation((disabled,), SkillInvocationRequest(name="reviewer"))
    assert disabled_error.value.code == "disabled_skill"

    internal = metadata.model_copy(update={"user_invocable": False})
    with pytest.raises(SkillActivationError) as internal_error:
        service.resolve_user_activation((internal,), SkillInvocationRequest(name="reviewer"))
    assert internal_error.value.code == "not_user_invocable"

    with pytest.raises(SkillActivationError) as unknown_error:
        service.resolve_user_activation((), SkillInvocationRequest(name="reviewer"))
    assert unknown_error.value.code == "unknown_skill"

    _write_skill(root, description="Changed after discovery.")
    with pytest.raises(SkillActivationError) as changed_error:
        service.resolve_user_activation((metadata,), SkillInvocationRequest(name="reviewer"))
    assert changed_error.value.code == "unloadable_skill"
    assert "metadata changed" in str(changed_error.value)


@pytest.mark.asyncio
async def test_runtime_persists_typed_user_activation_and_exposes_it_to_prompt_policy(
    tmp_path,
) -> None:
    root = tmp_path / "skills" / "reviewer"
    _write_skill(root, extra_frontmatter="disable-model-invocation: true\n")
    metadata = _metadata(root)
    provider = CapturingProvider()
    policy_calls: list[PromptPolicyCall] = []
    policies = PromptPolicyRegistry()

    def allow(call: PromptPolicyCall) -> PromptPolicyDecision:
        policy_calls.append(call)
        return PromptPolicyDecision()

    policies.register(PromptPolicyRegistration("test", "capture", allow))
    runtime = _runtime(tmp_path, provider, metadata, policies=policies)

    result = await runtime.run(
        "",
        skill_invocation=SkillInvocationRequest(name="reviewer", arguments="Review this change"),
    )

    assert result.run.status == "completed"
    assert "<name>reviewer</name>" not in provider.system_prompts[0]
    provider_user = provider.calls[0][-1]
    assert provider_user.role == "user"
    assert "Review the requested code carefully." in provider_user.text_content
    assert provider_user.text_content.endswith("Review this change")
    snapshot = runtime.store.load_runtime_session(result.session.session_id)
    user_message = snapshot.messages[0]
    assert user_message.role == "user"
    assert user_message.text_content == "Review this change"
    assert isinstance(user_message.content[0], SkillActivationContent)
    assert user_message.skill_activations[0].name == "reviewer"
    assert policy_calls[0].skill_activation is not None
    assert policy_calls[0].skill_activation.name == "reviewer"
    assert policy_calls[0].skill_activation.content_digest == (
        user_message.skill_activations[0].content_digest
    )
    serialized_events = "\n".join(event.model_dump_json() for event in result.events)
    assert user_message.skill_activations[0].content_digest in serialized_events
    assert "Review the requested code carefully." not in serialized_events


@pytest.mark.asyncio
async def test_runtime_allows_argumentless_skill_and_uses_skill_title(tmp_path) -> None:
    root = tmp_path / "skills" / "reviewer"
    _write_skill(root)
    metadata = _metadata(root)
    provider = CapturingProvider()
    runtime = _runtime(tmp_path, provider, metadata)

    result = await runtime.run("", skill_invocation=SkillInvocationRequest(name="reviewer"))

    assert result.session.title == "Skill: reviewer"
    assert "Review the requested code carefully." in provider.calls[0][-1].text_content
    snapshot = runtime.store.load_runtime_session(result.session.session_id)
    assert snapshot.messages[0].text_content == ""


@pytest.mark.asyncio
async def test_queued_skill_activation_uses_the_instructions_frozen_at_acceptance(tmp_path) -> None:
    root = tmp_path / "skills" / "reviewer"
    _write_skill(root, body="Original queued instructions.")
    metadata = _metadata(root)
    provider = CapturingProvider(block_first=True)
    runtime = _runtime(tmp_path, provider, metadata)
    session = runtime.create_session(title="Queue skill")

    active = asyncio.create_task(runtime.run("First task", session_id=session.session_id))
    await provider.started.wait()
    queued = await runtime.run(
        "",
        session_id=session.session_id,
        skill_invocation=SkillInvocationRequest(name="reviewer"),
    )
    pending = runtime.pending_user_inputs(session.session_id)
    assert len(pending) == 1
    assert pending[0].skill_name == "reviewer"
    _write_skill(root, body="Changed after queue acceptance.")
    provider.release.set()
    await active

    assert any(event.event_type == "follow_up_input_queued" for event in queued.events)
    assert len(provider.calls) == 2
    second_input = provider.calls[1][-1].text_content
    assert "Original queued instructions." in second_input
    assert "Changed after queue acceptance." not in second_input
    snapshot = runtime.store.load_runtime_session(session.session_id)
    activation_messages = [message for message in snapshot.messages if message.skill_activations]
    assert len(activation_messages) == 1
    assert activation_messages[0].skill_activations[0].instructions == (
        "Original queued instructions."
    )

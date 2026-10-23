from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ouro.application.inputs.skills import SkillActivationService, SkillInvocationRequest
from ouro.application.runtime.configuration import RuntimeConfigurationResolver
from ouro.application.runtime.service import RuntimeService
from ouro.core.adapter import RuntimeAdapter
from ouro.core.errors import PromptRejectedError, SkillActivationError
from ouro.core.messages import ModelMessage, SkillActivationContent
from ouro.core.prompt_policies import (
    PromptPolicyCall,
    PromptPolicyDecision,
    PromptPolicyRegistration,
    PromptPolicyRegistry,
)
from ouro.core.provider import ModelMetadata, ProviderStreamEvent
from ouro.sessions import LocalSessionStore
from ouro.skills import SkillLoader, SkillManager
from ouro.tools import Tool, ToolDefinition, ToolRegistry, ToolResult


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
    name: str = "reviewer",
    description: str = "Review source code.",
    body: str = "Review the requested code carefully.",
    extra_frontmatter: str = "",
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text(
        f"""---
name: {name}
description: {description}
{extra_frontmatter}---
{body}
""",
        encoding="utf-8",
    )


def _metadata(root: Path):
    return SkillLoader(root.parent).load_metadata()[0]


def _runtime(
    tmp_path: Path,
    provider: CapturingProvider,
    metadata,
    *,
    policies=None,
    skill_catalog_resolver=None,
):
    store = LocalSessionStore(tmp_path / "sessions")
    store.initialize()
    return RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="capture", provider_id="capture"),
        ),
        tools=ToolRegistry(),
        skills=metadata if isinstance(metadata, tuple) else (metadata,),
        skill_catalog_resolver=skill_catalog_resolver,
        prompt_policies=policies,
    )


def test_skill_activation_service_respects_selection_and_reads_current_content(
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
    assert activation.instructions == (root / "SKILL.md").read_text(encoding="utf-8")
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
    changed = service.resolve_user_activation((metadata,), SkillInvocationRequest(name="reviewer"))
    assert "Changed after discovery." in changed.instructions
    (root / "SKILL.md").unlink()
    with pytest.raises(SkillActivationError) as missing_error:
        service.resolve_user_activation((metadata,), SkillInvocationRequest(name="reviewer"))
    assert missing_error.value.code == "unloadable_skill"


async def test_runtime_discovers_installed_updated_and_removed_skills_at_use_boundaries(tmp_path):
    skill_root = tmp_path / "skills"
    provider = CapturingProvider()
    runtime = _runtime(
        tmp_path, provider, (), skill_catalog_resolver=RuntimeConfigurationResolver(tmp_path).skills
    )
    try:
        assert await runtime.available_skills() == ()
        root = skill_root / "reviewer"
        _write_skill(root)
        assert [item.name for item in await runtime.available_skills()] == ["reviewer"]
        _write_skill(root, description="Current description.", body="Current instructions.")
        result = await runtime.run("", skill_invocation=SkillInvocationRequest(name="reviewer"))
        assert "Current description." in provider.calls[-1][-1].text_content
        assert "Current instructions." in provider.calls[-1][-1].text_content

        (root / "SKILL.md").unlink()
        assert await runtime.available_skills() == ()
        with pytest.raises(SkillActivationError) as missing:
            await runtime.run("", skill_invocation=SkillInvocationRequest(name="reviewer"))
        assert missing.value.code == "unknown_skill"
        await runtime.run("next", session_id=result.session.session_id)
        assert "<name>reviewer</name>" not in provider.system_prompts[-1]
    finally:
        await runtime.close()


async def test_natural_language_turn_sees_skills_installed_during_the_session(tmp_path):
    provider = CapturingProvider()
    home = tmp_path / "home"
    runtime = _runtime(
        home, provider, (), skill_catalog_resolver=RuntimeConfigurationResolver(home).skills
    )
    try:
        first = await runtime.run("Start working")
        assert "<name>reviewer</name>" not in provider.system_prompts[-1]
        source = tmp_path / "reviewer"
        _write_skill(source)
        SkillManager(home).install(str(source))

        await runtime.run("Review this project", session_id=first.session.session_id)

        prompt = provider.system_prompts[-1]
        assert "<name>reviewer</name>" in prompt
        assert "Review source code." in prompt
        assert str(home / "skills" / "reviewer" / "SKILL.md") in prompt
        _write_skill(home / "skills" / "reviewer", description="Review the current project.")
        await runtime.run("Continue reviewing", session_id=first.session.session_id)
        assert "Review the current project." in provider.system_prompts[-1]
        assert "Review source code." not in provider.system_prompts[-1]
    finally:
        await runtime.close()


async def test_model_request_after_install_tool_sees_the_new_skill(tmp_path):
    class InstallingProvider(CapturingProvider):
        async def stream(
            self, *, model, system_prompt="", messages, tools=None, request_context=None
        ):
            self.calls.append(list(messages))
            self.system_prompts.append(system_prompt)
            if len(self.calls) == 1:
                yield ProviderStreamEvent.from_tool_call(
                    {"id": "install_1", "name": "InstallSkill", "arguments": {}}
                )
            else:
                yield ProviderStreamEvent(type="text_delta", text="Installed skill is available.")
            yield ProviderStreamEvent(type="done")

    home = tmp_path / "home"
    source = tmp_path / "reviewer"
    _write_skill(source)
    provider = InstallingProvider()
    runtime = _runtime(
        home, provider, (), skill_catalog_resolver=RuntimeConfigurationResolver(home).skills
    )

    def install(invocation, context):
        SkillManager(home).install(str(source))
        return ToolResult(
            tool_call_id=invocation.tool_call_id,
            tool_name=invocation.tool_name,
            content="Installed reviewer.",
        )

    runtime.tools.register(
        Tool(
            definition=ToolDefinition(
                name="InstallSkill", description="Install a skill.", permission_mode="auto"
            ),
            handler=install,
        )
    )
    try:
        result = await runtime.run("Install the reviewer skill and use it")
        assert result.run.status == "completed"
        assert len(provider.system_prompts) == 2
        assert "<name>reviewer</name>" not in provider.system_prompts[0]
        assert "<name>reviewer</name>" in provider.system_prompts[1]
    finally:
        await runtime.close()


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
    assert user_message.skill_activations[0].location == str(root / "SKILL.md")
    assert user_message.skill_activations[0].base_directory == str(root)
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
    original_document = (root / "SKILL.md").read_text(encoding="utf-8")
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
    assert [item.name for item in pending[0].skills] == ["reviewer"]
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
    assert activation_messages[0].skill_activations[0].instructions == original_document


@pytest.mark.parametrize("reject_second", (False, True))
async def test_multiple_skills_are_accepted_as_one_input_after_all_policies_allow(
    tmp_path, reject_second
):
    for name in ("reviewer", "writer"):
        _write_skill(tmp_path / "skills" / name, name=name, body=f"Instructions for {name}.")
    metadata = tuple(SkillLoader(tmp_path / "skills").load_metadata())
    provider = CapturingProvider()
    policies = PromptPolicyRegistry()
    seen = []

    def review(call):
        seen.append(call.skill_activation.name)
        if reject_second and call.skill_activation.name == "writer":
            return PromptPolicyDecision(action="deny", reason="Writer unavailable")
        return PromptPolicyDecision()

    policies.register(PromptPolicyRegistration("test", "review", review))
    runtime = _runtime(tmp_path, provider, metadata, policies=policies)
    invocations = tuple(SkillInvocationRequest(name=item.name) for item in metadata)
    if reject_second:
        with pytest.raises(PromptRejectedError, match="Writer unavailable"):
            await runtime.run("", skill_invocations=invocations)
        assert not provider.calls
        assert not runtime.store.list_sessions()
    else:
        result = await runtime.run("", skill_invocations=invocations)
        messages = runtime.store.load_runtime_session(result.session.session_id).messages
        inputs = [item for item in messages if item.role == "user"]
        assert len(inputs) == 1 and not inputs[0].scaffold
        assert [item.name for item in inputs[0].skill_activations] == ["reviewer", "writer"]
        for name in ("reviewer", "writer"):
            assert provider.calls[0][-1].text_content.count(f"Instructions for {name}.") == 1
    assert seen == ["reviewer", "writer"]


async def test_multiple_queued_skills_remain_one_input_with_frozen_instructions(tmp_path):
    originals = []
    for name in ("reviewer", "writer"):
        _write_skill(tmp_path / "skills" / name, name=name, body=f"Original {name}.")
        originals.append((tmp_path / "skills" / name / "SKILL.md").read_text(encoding="utf-8"))
    provider = CapturingProvider(block_first=True)
    runtime = _runtime(tmp_path, provider, tuple(SkillLoader(tmp_path / "skills").load_metadata()))
    session = runtime.create_session(title="Multi skill queue")
    active = asyncio.create_task(runtime.run("First task", session_id=session.session_id))
    try:
        await asyncio.wait_for(provider.started.wait(), timeout=5)
        await runtime.run(
            "",
            session_id=session.session_id,
            skill_invocations=(
                SkillInvocationRequest(name="reviewer"),
                SkillInvocationRequest(name="writer"),
            ),
        )
        pending = runtime.pending_user_inputs(session.session_id)
        assert len(pending) == 1
        assert [item.name for item in pending[0].skills] == ["reviewer", "writer"]
        for name in ("reviewer", "writer"):
            _write_skill(tmp_path / "skills" / name, name=name, body=f"Changed {name}.")
    finally:
        provider.release.set()
        await active
    snapshot = runtime.store.load_runtime_session(session.session_id)
    activated = [item for item in snapshot.messages if item.skill_activations]
    assert len(activated) == 1 and not activated[0].scaffold
    assert [item.instructions for item in activated[0].skill_activations] == originals
    assert len(provider.calls) == 2

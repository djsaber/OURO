from __future__ import annotations

import pytest

from ouro.application.resources import ResourceService
from ouro.application.runtime import RuntimeService
from ouro.cli.interactive import run_interactive
from ouro.core.adapter import RuntimeAdapter
from ouro.core.messages import ImageContent, ModelMessage, SkillActivationContent
from ouro.core.provider import ModelMetadata, ProviderCheckResult, ProviderStreamEvent
from ouro.resources import LocalResourceStore
from ouro.sessions import LocalSessionStore
from ouro.skills import discover_skill_catalog
from ouro.tools import ToolRegistry


@pytest.mark.parametrize("replacement", ["", "edited prompt"])
async def test_interactive_undo_restores_resources_and_uses_normal_submission(
    tmp_path, replacement
):
    runtime, provider = _runtime(tmp_path, supports_vision=True)
    image = tmp_path / "reference.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nreference")
    inputs = iter(
        [f'/attach "{image}"', "original prompt", "/undo", "/attachments", replacement, "/quit"]
    )
    outputs = []
    try:
        assert (
            await run_interactive(
                runtime=runtime, input_func=lambda _: next(inputs), output_func=outputs.append
            )
            == 0
        )
        session = runtime.sessions.catalog().entries[0]
        snapshot = runtime.store.load_runtime_session(session.session_id)
        user = next(item for item in snapshot.messages if item.role == "user")
        assert user.text_content == (replacement or "original prompt")
        assert len(user.resource_refs) == 1
        assert snapshot.session.history_epoch == 1
        assert len(provider.requests) == 2
        assert any("Restored input:" in line for line in outputs)
        assert any("[1] reference.png" in line for line in outputs)
        assert any(
            isinstance(block, ImageContent)
            for message in provider.requests[-1]
            for block in message.content
        )
    finally:
        await runtime.close()


class _CapturingProvider:
    provider_id = "capture"

    def __init__(self) -> None:
        self.requests: list[list[ModelMessage]] = []

    async def stream(self, *, model, system_prompt="", messages, tools=None):
        del model, system_prompt, tools
        self.requests.append(list(messages))
        yield ProviderStreamEvent(type="text_delta", text="done")
        yield ProviderStreamEvent(type="done")

    async def check_connection(self, model: ModelMetadata) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            model_id=model.model_id,
            ok=True,
            message="ok",
        )


def _runtime(tmp_path, *, supports_vision: bool) -> tuple[RuntimeService, _CapturingProvider]:
    sessions = LocalSessionStore(tmp_path)
    sessions.initialize()
    resource_store = LocalResourceStore(tmp_path)
    resource_store.initialize()
    resources = ResourceService(store=resource_store, sessions=sessions)
    provider = _CapturingProvider()
    adapter = RuntimeAdapter(
        provider,
        ModelMetadata(
            model_id="capture-model",
            provider_id=provider.provider_id,
            supports_vision=supports_vision,
        ),
    )
    return (
        RuntimeService(
            store=sessions,
            adapter=adapter,
            tools=ToolRegistry(),
            resources=resources,
            skills=tuple(discover_skill_catalog(home=tmp_path).skills),
        ),
        provider,
    )


async def test_interactive_failed_undo_keeps_restored_skill_and_attachments(tmp_path):
    skill = tmp_path / "skills" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: demo\ndescription: Demo.\n---\nApply the demo.\n", encoding="utf-8"
    )
    runtime, _ = _runtime(tmp_path, supports_vision=True)
    notes = tmp_path / "notes.txt"
    notes.write_text("reference", encoding="utf-8")
    inputs = iter(
        [f'/attach "{notes}"', "/skill demo original", "/undo", "/undo", "edited", "/quit"]
    )
    outputs = []
    try:
        assert (
            await run_interactive(
                runtime=runtime, input_func=lambda _: next(inputs), output_func=outputs.append
            )
            == 0
        )
        session = runtime.sessions.catalog().entries[0]
        user = next(
            message
            for message in runtime.store.load_messages_tail(session.session_id)
            if message.role == "user"
        )
        assert user.text_content == "edited"
        assert len(user.resource_refs) == 1
        assert any(
            isinstance(block, SkillActivationContent) and block.name == "demo"
            for block in user.content
        )
        assert any(line.startswith("Error:") for line in outputs)
    finally:
        await runtime.close()


async def test_interactive_attachment_uses_canonical_resource_projection(tmp_path) -> None:
    runtime, provider = _runtime(tmp_path, supports_vision=True)
    image = tmp_path / "reference image.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nreference")
    inputs = iter(
        [
            f'/attach "{image}"',
            f'/attach "{image}"',
            "/attachments",
            "Reverse-engineer an image generation prompt.",
            "/quit",
        ]
    )
    outputs: list[str] = []

    try:
        code = await run_interactive(
            runtime=runtime,
            input_func=lambda _prompt: next(inputs),
            output_func=outputs.append,
        )

        assert code == 0
        assert any("Attached for next message: [1] reference image.png" in line for line in outputs)
        assert any(
            "Already attached for next message: [1] reference image.png" in line for line in outputs
        )
        assert any(line.startswith("Attachments for the next message:") for line in outputs)

        session = runtime.sessions.catalog().entries[0]
        snapshot = runtime.store.load_runtime_session(session.session_id)
        user_message = next(message for message in snapshot.messages if message.role == "user")
        assert len(user_message.resource_refs) == 1
        assert all(not isinstance(block, ImageContent) for block in user_message.content)

        request_message = provider.requests[0][0]
        assert any(isinstance(block, ImageContent) for block in request_message.content)
    finally:
        await runtime.close()


async def test_failed_first_attachment_keeps_created_session_reachable(tmp_path) -> None:
    runtime, provider = _runtime(tmp_path, supports_vision=True)
    missing = tmp_path / "missing.png"
    inputs = iter([f"/attach {missing}", "Continue after the error.", "/quit"])
    outputs: list[str] = []

    try:
        code = await run_interactive(
            runtime=runtime,
            input_func=lambda _prompt: next(inputs),
            output_func=outputs.append,
        )

        assert code == 0
        assert any("Error:" in line and missing.name in line for line in outputs)
        entries = runtime.sessions.catalog().entries
        assert len(entries) == 1
        snapshot = runtime.store.load_runtime_session(entries[0].session_id)
        user_text = [
            message.text_content for message in snapshot.messages if message.role == "user"
        ]
        assert user_text == ["Continue after the error."]
        assert len(provider.requests) == 1
    finally:
        await runtime.close()


async def test_interactive_keeps_attachment_when_endpoint_rejects_vision(tmp_path) -> None:
    runtime, provider = _runtime(tmp_path, supports_vision=False)
    image = tmp_path / "reference.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nreference")
    inputs = iter(
        [
            f"/attach {image}",
            "Describe this image.",
            "/attachments",
            "/quit",
        ]
    )
    outputs: list[str] = []

    try:
        code = await run_interactive(
            runtime=runtime,
            input_func=lambda _prompt: next(inputs),
            output_func=outputs.append,
        )

        assert code == 0
        assert provider.requests == []
        assert any("does not support image attachment" in line for line in outputs)
        assert any(line.startswith("Attachments for the next message:") for line in outputs)
        session = runtime.sessions.catalog().entries[0]
        assert runtime.store.load_runtime_session(session.session_id).messages == []
    finally:
        await runtime.close()


async def test_interactive_detach_only_changes_next_message_draft(tmp_path) -> None:
    runtime, provider = _runtime(tmp_path, supports_vision=True)
    image = tmp_path / "reference.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nreference")
    inputs = iter(
        [
            f"/attach {image}",
            "/detach 1",
            "Continue without the image.",
            "/quit",
        ]
    )
    outputs: list[str] = []

    try:
        code = await run_interactive(
            runtime=runtime,
            input_func=lambda _prompt: next(inputs),
            output_func=outputs.append,
        )

        assert code == 0
        assert "Detached from next message: reference.png" in outputs
        session = runtime.sessions.catalog().entries[0]
        snapshot = runtime.store.load_runtime_session(session.session_id)
        user_message = next(message for message in snapshot.messages if message.role == "user")
        assert user_message.resource_refs == []
        assert runtime.resources is not None
        assert len(runtime.resources.session_resource_ids(session.session_id)) == 1
        assert all(
            not isinstance(block, ImageContent)
            for request in provider.requests
            for message in request
            for block in message.content
        )
    finally:
        await runtime.close()


async def test_interactive_new_session_clears_previous_attachment_draft(tmp_path) -> None:
    runtime, provider = _runtime(tmp_path, supports_vision=True)
    image = tmp_path / "reference.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nreference")
    inputs = iter(
        [
            f"/attach {image}",
            "/new",
            "/attachments",
            "Start clean.",
            "/quit",
        ]
    )
    outputs: list[str] = []

    try:
        code = await run_interactive(
            runtime=runtime,
            input_func=lambda _prompt: next(inputs),
            output_func=outputs.append,
        )

        assert code == 0
        assert "No attachments for the next message." in outputs
        assert len(runtime.sessions.catalog().entries) == 2
        assert all(
            not isinstance(block, ImageContent)
            for request in provider.requests
            for message in request
            for block in message.content
        )
    finally:
        await runtime.close()

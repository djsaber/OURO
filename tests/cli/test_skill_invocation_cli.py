from __future__ import annotations

from pathlib import Path

import pytest

from ouro.application.runtime import RuntimeService
from ouro.cli.interactive import run_interactive
from ouro.core.adapter import RuntimeAdapter
from ouro.core.messages import ModelMessage
from ouro.core.provider import ModelMetadata, ProviderStreamEvent
from ouro.sessions import LocalSessionStore
from ouro.skills import SkillLoader
from ouro.tools import ToolRegistry


class CapturingProvider:
    provider_id = "capture"

    def __init__(self) -> None:
        self.calls: list[list[ModelMessage]] = []

    async def stream(self, *, model, system_prompt="", messages, tools=None):
        del model, system_prompt, tools
        self.calls.append(list(messages))
        yield ProviderStreamEvent(type="text_delta", text="reviewed")
        yield ProviderStreamEvent(type="done")


def _runtime(tmp_path: Path) -> tuple[RuntimeService, CapturingProvider]:
    skill_root = tmp_path / "skills" / "reviewer"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        """---
name: reviewer
description: Review source changes.
argument-hint: <target>
---
Apply the workspace review checklist.
""",
        encoding="utf-8",
    )
    metadata = SkillLoader(skill_root.parent).load_metadata()[0]
    store = LocalSessionStore(tmp_path / "sessions")
    store.initialize()
    provider = CapturingProvider()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="capture", provider_id="capture"),
        ),
        tools=ToolRegistry(),
        skills=(metadata,),
    )
    return runtime, provider


@pytest.mark.asyncio
async def test_interactive_lists_and_invokes_skill_through_the_normal_user_run_path(
    tmp_path,
) -> None:
    runtime, provider = _runtime(tmp_path)
    inputs = iter(["/skills", "/skill   reviewer   inspect app.py", "/history", "/quit"])
    outputs: list[str] = []

    code = await run_interactive(
        runtime=runtime,
        input_func=lambda _prompt: next(inputs),
        output_func=outputs.append,
    )

    assert code == 0
    rendered = "\n".join(outputs)
    assert "Available skills:" in rendered
    assert "/skill reviewer <target>" in rendered
    assert "[user] /skill reviewer inspect app.py" in rendered
    assert len(provider.calls) == 1
    user_input = provider.calls[0][-1].text_content
    assert "Apply the workspace review checklist." in user_input
    assert user_input.endswith("inspect app.py")


@pytest.mark.asyncio
async def test_interactive_rejects_skill_steering_before_starting_a_run(tmp_path) -> None:
    runtime, provider = _runtime(tmp_path)
    inputs = iter(["/steer /skill reviewer inspect app.py", "/quit"])
    outputs: list[str] = []

    code = await run_interactive(
        runtime=runtime,
        input_func=lambda _prompt: next(inputs),
        output_func=outputs.append,
    )

    assert code == 0
    assert any("Skill activation cannot steer" in output for output in outputs)
    assert provider.calls == []

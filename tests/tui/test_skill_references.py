from __future__ import annotations

import asyncio

import pytest

from ouro.application.skill_activations import SkillInvocationRequest
from ouro.core.input_content import InputContent, InputFileReference, InputSkillReference
from ouro.skills import SkillLoader
from ouro.tui.app import OuroTuiApp
from ouro.tui.input_drafts import TuiInputDraftStore
from ouro.tui.widgets import CommandPopup, Composer
from tests.tui.helpers import run_ready_app
from tests.tui.test_history_commands import application_at


def application_with_skills(home):
    for name in ("reviewer", "writer"):
        path = home / "skills" / name / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"---\nname: {name}\ndescription: Use {name}.\n---\nInstructions for {name}.\n",
            encoding="utf-8",
        )
    application = application_at(home, skills=tuple(SkillLoader(home / "skills").load_metadata()))
    application.runtime.default_workspace = str(home)
    return application


async def test_skill_multiselect_is_inline_atomic_and_preserves_the_draft(tmp_path):
    app = OuroTuiApp(application=application_with_skills(tmp_path))
    async with run_ready_app(app, size=(80, 28)) as pilot:
        composer = app.query_one(Composer)
        composer.load_text("before after")
        composer.move_cursor((0, 0))
        screen = app.screen
        await pilot.press("@")
        popup = app.query_one(CommandPopup)
        assert [item.name for item in popup._commands] == ["@file", "@skill"]
        await pilot.press(*"skill", "enter")
        assert app.focused is composer and app.screen is screen
        assert popup.region.bottom == composer.region.y
        assert composer.draft_content == InputContent(text="before after")
        await pilot.press("space", "down", "space", "enter")
        content = composer.input_content
        assert content.text == "before after"
        assert [item.name for item in content.skills] == ["reviewer", "writer"]
        assert [item.text_offset for item in content.skills] == [0, 0]
        assert composer.text == "@reviewer @writer before after"
        assert not popup.display and app._controller.session_id is None
        await pilot.press("backspace")
        assert [item.name for item in composer.input_content.skills] == ["reviewer"]
        composer.undo()
        assert composer.input_content == content
        composer.redo()
        assert len(composer.input_content.skills) == 1
        composer.undo()
        composer.move_cursor((0, 0))
        await pilot.press("@", *"skill", "enter")
        assert not app._skill_picker.options
        await pilot.press("escape")
        assert composer.input_content == content


async def test_mixed_references_survive_restart_and_form_one_complete_attachment_scaffold(
    tmp_path, monkeypatch
):
    app = OuroTuiApp(application=application_with_skills(tmp_path))
    async with run_ready_app(app) as pilot:
        composer = app.query_one(Composer)
        await pilot.press("@", *"skill", "space", "space", "down", "space", "enter")
        assert len(composer.input_content.skills) == 2
        files = []
        for directory in ("one", "two"):
            path = tmp_path / directory / "notes.txt"
            path.parent.mkdir()
            path.write_text(f"Contents from {directory}.", encoding="utf-8")
            files.append(
                InputFileReference(name=path.name, path=str(path), workspace=str(tmp_path))
            )
        composer.insert_attachments(tuple(files))
        composer.insert_plain_paste("inspect")
        original = composer.input_content
        display = composer.text
        assert display == "@reviewer @writer @notes.txt @notes.txt inspect"
        await pilot.pause()
    assert TuiInputDraftStore(tmp_path).load().content_for(None) == original

    application = application_with_skills(tmp_path)
    restarted = OuroTuiApp(application=application)
    errors = []
    monkeypatch.setattr(restarted, "notify", lambda message, **kwargs: errors.append(message))
    async with run_ready_app(restarted) as pilot:
        composer = restarted.query_one(Composer)
        assert composer.input_content == original and composer.text == display
        await pilot.press("enter")
        if workers := [w for w in restarted.workers if w.group == "run"]:
            await asyncio.wait_for(restarted.workers.wait_for_complete(workers), timeout=10)
        await pilot.pause()
        assert not errors
        snapshot = application.runtime.store.load_runtime_session(restarted._controller.session_id)
        user_messages = [item for item in snapshot.messages if item.role == "user"]
        scaffolds = [item for item in user_messages if item.scaffold]
        assert len(user_messages) == 2 and len(scaffolds) == 1
        assert scaffolds[0].semantic_tag == "attachments"
        assert len(scaffolds[0].input_attachments) == 2
        for directory in ("one", "two"):
            assert f"Contents from {directory}." in scaffolds[0].text_content
        body = next(item for item in user_messages if not item.scaffold)
        assert body.text_content == "inspect"
        assert [item.name for item in body.skill_activations] == ["reviewer", "writer"]
        assert len({item.path for item in body.input_attachments}) == 2
        await restarted._edit_session_history("/undo").wait()
        await pilot.pause()
        assert composer.input_content == original and composer.text == display


@pytest.mark.parametrize("key", ("escape", "ctrl+a"))
async def test_cancel_skill_menu_preserves_existing_elements(tmp_path, key):
    app = OuroTuiApp(application=application_with_skills(tmp_path))
    async with run_ready_app(app) as pilot:
        composer = app.query_one(Composer)
        composer.load_input(
            InputContent(text="draft", skills=(InputSkillReference(name="reviewer"),))
        )
        original = composer.input_content
        composer.move_cursor((0, 0))
        await pilot.press("@", *"skill", "enter", "space")
        if key == "escape":
            await pilot.press(key)
        else:
            # Removing the command closes the menu and restores the captured draft.
            composer.load_text("")
        await pilot.pause()
        assert composer.input_content == original
        assert not app.query_one(CommandPopup).display


async def test_rejected_skill_submission_restores_all_elements(tmp_path):
    app = OuroTuiApp(application=application_at(tmp_path))
    async with run_ready_app(app) as pilot:
        composer = app.query_one(Composer)
        composer.load_input(
            InputContent(text="task", skills=(InputSkillReference(name="missing"),))
        )
        original = composer.input_content
        await pilot.press("enter")
        if workers := [w for w in app.workers if w.group == "run"]:
            await asyncio.wait_for(app.workers.wait_for_complete(workers), timeout=10)
        await pilot.pause()
        assert composer.input_content == original
        assert app._controller.session_id is None


async def test_unqueue_restores_multiple_skills_and_files_after_an_existing_draft(tmp_path):
    application = application_with_skills(tmp_path)
    runtime = application.runtime
    session = runtime.create_session(title="queued references")
    app = OuroTuiApp(application=application, resume_session_id=session.session_id)
    async with run_ready_app(app) as pilot:
        composer = app.query_one(Composer)
        composer.insert_skills(
            (InputSkillReference(name="reviewer"), InputSkillReference(name="writer"))
        )
        composer.insert_attachments(
            (InputFileReference(name="a.txt", path=str(tmp_path / "a.txt")),)
        )
        composer.insert_plain_paste("task")
        original = composer.input_content
        activations = tuple(
            runtime.skill_activations.resolve_user_activation(
                runtime.available_skills(),
                SkillInvocationRequest(
                    name=item.name, text_offset=item.text_offset, order=item.order
                ),
            )
            for item in original.skills
        )
        _, queued, _ = runtime.queued_inputs.enqueue(
            snapshot=runtime.store.load_runtime_session(session.session_id),
            prompt=original.text,
            skill_activations=activations,
            attachments=original.attachments,
            title=None,
            role="user",
            scaffold=False,
        )
        composer.load_text("draft")
        await pilot.pause()
        await app._unqueue_message(queued.queue_id)
        assert composer.input_content == InputContent(text="draft").followed_by(original)
        assert composer.text == "draft\n@reviewer @writer @a.txt task"
        assert not runtime.pending_user_inputs(session.session_id)

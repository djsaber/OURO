from __future__ import annotations

import pytest
from textual import events
from textual.widgets import Static

from ouro.core.input_content import InputContent, InputFileReference, InputSkillReference
from ouro.skills import SkillLoader
from ouro.tui.app import OuroTuiApp
from ouro.tui.attachment_picker import AttachmentEntry, AttachmentPicker
from ouro.tui.widgets import CommandPopup, Composer
from tests.tui.helpers import command_popup_text, resize_terminal, run_ready_app
from tests.tui.test_history_commands import application_at


async def test_large_file_menu_drains_arrow_bursts_then_filters_and_selects(tmp_path, monkeypatch):
    entries = tuple(
        AttachmentEntry(tmp_path / f"file-{index:05}.txt", False) for index in range(10000)
    )
    monkeypatch.setattr(AttachmentPicker, "read_directory", staticmethod(lambda _: entries))
    app = OuroTuiApp(application=application_at(tmp_path))
    async with run_ready_app(app, size=(60, 26)) as pilot:
        app._active_workspace = str(tmp_path)
        composer = app.query_one(Composer)
        await pilot.press("@", *"file", "enter")
        await app.workers.wait_for_complete(
            [worker for worker in app.workers if worker.group == "attachment-directory"]
        )
        await pilot.pause()
        popup = app.query_one(CommandPopup)
        # Feed real key messages without Pilot's per-key idle waits.
        for key in ["down"] * 120 + ["up"] * 40:
            app.post_message(events.Key(key, None))
        await pilot.pause()
        assert popup.selected_argument.value == str(entries[79].path)
        assert popup._content.region.y + 80 >= popup._heading_widget.region.bottom
        assert popup._content.region.y + 80 < popup.content_region.bottom
        assert app.focused is composer and composer.text == "@file "
        await pilot.press("space")
        assert app._attachment_picker.selected == [entries[79].path]
        composer.set_attachment_query("file-00079")
        await pilot.pause()
        assert len(popup._arguments) == 1 and popup.selected_argument.checked
        assert popup.scroll_y == 0
        await pilot.press("escape")
        assert not popup.display and composer.input_content.is_empty


@pytest.mark.parametrize("kind", ("file", "skill"))
async def test_reference_heading_and_count_stay_visible_while_scrolling_and_filtering(
    tmp_path, kind
):
    for index in range(20):
        name = f"item-{index:02}"
        (tmp_path / f"{name}.txt").write_text(name, encoding="utf-8")
        skill = tmp_path / "skills" / name / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(f"---\nname: {name}\ndescription: Test skill.\n---\nTest instructions.")
    application = application_at(
        tmp_path, skills=tuple(SkillLoader(tmp_path / "skills").load_metadata())
    )
    app = OuroTuiApp(application=application)
    async with run_ready_app(app, size=(44, 22)) as pilot:
        app._active_workspace = str(tmp_path)
        composer = app.query_one(Composer)
        await pilot.press("@", *kind, "enter")
        if workers := [worker for worker in app.workers if worker.group == "attachment-directory"]:
            await app.workers.wait_for_complete(workers)
        composer.set_attachment_query("item")
        await pilot.pause()
        popup = app.query_one(CommandPopup)
        heading = popup.query_one("#command-popup-heading", Static)
        assert heading.display and heading.content.plain.endswith("[0]")
        assert heading.region.y == popup.content_region.y
        initial_y = heading.region.y
        await pilot.press("space", *(["down"] * 19))
        await pilot.pause()
        assert popup.scroll_y > 0
        assert heading.region.y == initial_y
        assert heading.content.plain.endswith("[1]")
        assert heading.content.cell_len == heading.content_region.width
        assert "Reference selected" not in command_popup_text(popup).plain
        assert popup.region.bottom == composer.region.y
        assert popup.region.right <= 44
        assert popup._content.region.y + 19 >= heading.region.bottom
        assert popup._content.region.y + 19 < popup.content_region.bottom
        await pilot.press("down", "space")
        assert heading.content.plain.endswith("[0]")
        assert popup._content.region.y == heading.region.bottom
        await pilot.press("space")
        await resize_terminal(pilot, 60, 26)
        await pilot.pause()
        assert heading.content.plain.endswith("[1]")
        assert heading.content.cell_len == heading.content_region.width
        composer.set_attachment_query("unmatched")
        await pilot.pause()
        assert popup.display and heading.display and heading.content.plain.endswith("[1]")
        assert popup.selected_argument is None
        assert composer.draft_content.is_empty
        await pilot.press("enter")
        if workers := [worker for worker in app.workers if worker.group == "attachment-reference"]:
            await app.workers.wait_for_complete(workers)
        content = composer.input_content
        assert len(content.elements) == 1 and content.text == ""
        assert composer.text == ("@item-00.txt " if kind == "file" else "@item-00 ")
        assert not popup.display


@pytest.mark.parametrize("kind", ("file", "skill"))
async def test_reference_commands_only_open_at_the_start_of_the_input(tmp_path, kind):
    app = OuroTuiApp(application=application_at(tmp_path))
    async with run_ready_app(app) as pilot:
        composer = app.query_one(Composer)
        popup = app.query_one(CommandPopup)
        for text, cursor in (
            ("name", (0, 4)),
            (" ", (0, 1)),
            ("check ", (0, 6)),
            ("check\n", (1, 0)),
        ):
            composer.load_text(text)
            composer.move_cursor(cursor)
            await pilot.press("@", *kind, "space")
            assert composer.input_content == InputContent(text=text + f"@{kind} ")
            assert not composer.attachment_menu_open and not popup.display
        for element in (
            InputFileReference(name="notes.txt", path=str(tmp_path / "notes.txt")),
            InputSkillReference(name="reviewer"),
        ):
            original = InputContent.from_elements("", (element,))
            composer.load_input(original)
            composer.move_cursor((0, len(composer.text)))
            await pilot.press("@", *kind, "space")
            assert composer.input_content.text == f"@{kind} "
            assert composer.input_content.elements == original.elements
            assert not composer.attachment_menu_open and not popup.display
            composer.load_input(original)
            composer.move_cursor((0, 0))
            await pilot.press("@", *kind, "enter")
            await pilot.pause()
            assert composer.attachment_menu_open and popup.display
            assert composer.draft_content == original
            await pilot.press("escape")
            assert composer.input_content == original

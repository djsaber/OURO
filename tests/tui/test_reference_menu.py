from __future__ import annotations

import pytest
from rich.style import Style
from textual.widgets import Static

from ouro.core.input_content import InputContent, InputFileReference, InputSkillReference
from ouro.skills import SkillLoader
from ouro.tui.app import OuroTuiApp
from ouro.tui.widgets import CommandPopup, Composer
from tests.tui.helpers import run_ready_app
from tests.tui.test_history_commands import application_at


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
        assert heading.content.get_style_at_offset(
            app.console, len(heading.content.plain) - 1
        ) == Style.parse(app.tui_theme.text_lowlight)
        assert "Reference selected" not in popup._content.content.plain
        assert popup.region.bottom == composer.region.y
        assert popup.region.right <= 44
        assert popup._content.region.y + 19 >= heading.region.bottom
        assert popup._content.region.y + 19 < popup.content_region.bottom
        await pilot.press("down", "space")
        assert heading.content.plain.endswith("[0]")
        assert popup._content.region.y == heading.region.bottom
        await pilot.press("space")
        await pilot.resize_terminal(60, 26)
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

from pathlib import Path

import pytest
from textual.app import App
from textual.widgets import OptionList

from ouro.tui.archive_paths import ArchivePathScreen


@pytest.mark.parametrize(
    ("field", "invalid", "valid", "message"),
    [
        ("location", "d", "valid", "Choose an existing folder"),
        ("location", "file.txt", "valid", "Choose an existing folder"),
        ("location", "", "valid", "Choose an existing folder"),
        ("filename", "../bad", "backup", "Enter a file name without a folder"),
    ],
)
async def test_invalid_path_warns_and_preserves_editor_until_corrected(
    tmp_path, monkeypatch, field, invalid, valid, message
):
    (tmp_path / "valid").mkdir()
    (tmp_path / "file.txt").write_text("file", encoding="utf-8")
    app = App()
    notices = []
    monkeypatch.setattr(app, "notify", lambda message, **kwargs: notices.append(str(message)))
    async with app.run_test(size=(100, 30)) as pilot:
        screen = ArchivePathScreen(directory=tmp_path, filename="original.zip")
        app.push_screen(screen)
        await app.workers.wait_for_complete()
        await pilot.pause()
        options = screen.query_one(OptionList)
        options.highlighted = options.get_option_index(field)
        await pilot.press("enter", "ctrl+u", *invalid, "enter")
        assert app.screen is screen
        assert notices == [message]
        assert screen._editing_value == field
        assert screen._edit_text == invalid
        assert screen._directory == tmp_path
        assert screen._filename == "original.zip"
        await pilot.press("ctrl+u", *valid, "enter")
        assert screen._editing_value is None
        assert screen._directory == (tmp_path / valid if field == "location" else tmp_path)
        assert screen._filename == ("backup.zip" if field == "filename" else "original.zip")


async def test_folder_access_error_warns_and_allows_cancel(tmp_path, monkeypatch):
    app = App()
    notices = []
    monkeypatch.setattr(app, "notify", lambda message, **kwargs: notices.append(str(message)))
    async with app.run_test(size=(100, 30)) as pilot:
        screen = ArchivePathScreen(directory=tmp_path, filename=None)
        app.push_screen(screen)
        await app.workers.wait_for_complete()
        await pilot.pause()
        original = Path.is_dir

        def denied(path):
            if path == tmp_path / "denied":
                raise PermissionError("Folder access denied")
            return original(path)

        monkeypatch.setattr(Path, "is_dir", denied)
        await pilot.press("enter", "ctrl+u", *"denied", "enter")
        assert notices == ["Folder access denied"]
        assert screen._editing_value == "location"
        assert screen._directory == tmp_path
        await pilot.press("escape")
        assert app.screen is screen
        assert screen._editing_value is None
        await pilot.press("escape")
        assert app.screen is not screen

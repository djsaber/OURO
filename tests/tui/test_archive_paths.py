from pathlib import Path

import pytest
from textual.app import App
from textual.widgets import OptionList

from ouro.tui.archive_paths import ArchivePathScreen


async def test_archive_listing_reuses_directory_entry_types(tmp_path, monkeypatch):
    (tmp_path / "folder").mkdir()
    (tmp_path / "archive.zip").touch()
    (tmp_path / "ignored.txt").touch()
    screen = ArchivePathScreen(directory=tmp_path, filename=None)
    type_queries = 0
    original_is_dir, original_is_file = Path.is_dir, Path.is_file

    def counted_is_dir(self):
        nonlocal type_queries
        type_queries += 1
        return original_is_dir(self)

    def counted_is_file(self):
        nonlocal type_queries
        type_queries += 1
        return original_is_file(self)

    monkeypatch.setattr(Path, "is_dir", counted_is_dir)
    monkeypatch.setattr(Path, "is_file", counted_is_file)
    choices = await screen._path_choices()
    assert {choice.value for choice in choices} >= {"directory:folder", "file:archive.zip"}
    assert type_queries == 0


async def test_import_choices_list_archives_before_one_contiguous_folder_group(tmp_path) -> None:
    (tmp_path / "zeta").mkdir()
    (tmp_path / "Alpha").mkdir()
    (tmp_path / "beta.zip").write_bytes(b"zip")
    (tmp_path / "ALPHA.ZIP").write_bytes(b"zip")
    (tmp_path / "ignored.txt").write_text("ignored", encoding="utf-8")

    choices = await ArchivePathScreen(directory=tmp_path, filename=None)._path_choices()

    assert [choice.value for choice in choices] == [
        "location",
        "home",
        "parent",
        "file:ALPHA.ZIP",
        "file:beta.zip",
        "directory:Alpha",
        "directory:zeta",
    ]
    assert list(dict.fromkeys(choice.group for choice in choices)) == [
        "LOCATION",
        "ARCHIVES",
        "FOLDERS",
    ]


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

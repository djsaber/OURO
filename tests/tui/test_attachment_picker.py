from __future__ import annotations

from pathlib import Path

import pytest

from ouro.tui.composer.attachment_picker import AttachmentEntry, AttachmentPicker


def test_directory_scan_reuses_entry_metadata_and_sorts_directories_first(tmp_path, monkeypatch):
    (tmp_path / "z-dir").mkdir()
    (tmp_path / "A-dir").mkdir()
    (tmp_path / "b.txt").touch()
    (tmp_path / "a.txt").touch()
    type_queries = 0
    original = Path.is_dir

    def counted_is_dir(self, **kwargs):
        nonlocal type_queries
        type_queries += 1
        return original(self, **kwargs)

    monkeypatch.setattr(Path, "is_dir", counted_is_dir)
    entries = AttachmentPicker.read_directory(tmp_path)
    assert entries == tuple(
        AttachmentEntry(tmp_path / name, directory)
        for name, directory in (
            ("A-dir", True),
            ("z-dir", True),
            ("a.txt", False),
            ("b.txt", False),
        )
    )
    assert type_queries == 0


def test_directory_scan_excludes_symbolic_links(tmp_path):
    target = tmp_path / "directory"
    target.mkdir()
    file = tmp_path / "file.txt"
    file.touch()
    try:
        (tmp_path / "directory-link").symlink_to(target, target_is_directory=True)
        (tmp_path / "file-link").symlink_to(file)
        (tmp_path / "broken-link").symlink_to(tmp_path / "missing")
    except OSError:
        pytest.skip("Creating symlinks requires permission on this platform")
    assert AttachmentPicker.read_directory(tmp_path) == (
        AttachmentEntry(target, True),
        AttachmentEntry(file, False),
    )


def test_directory_scan_returns_empty_or_reports_missing_directory(tmp_path):
    assert AttachmentPicker.read_directory(tmp_path) == ()
    with pytest.raises(FileNotFoundError):
        AttachmentPicker.read_directory(tmp_path / "missing")

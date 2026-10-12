"""Directory and selection state projected onto the shared composer command menu."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ouro.tui.commands import TuiCommandArgument
from ouro.tui.theme import TuiTheme


@dataclass(frozen=True)
class AttachmentEntry:
    path: Path
    is_directory: bool


class AttachmentPicker:
    def __init__(self, directory: Path, *, limit: int = 16) -> None:
        self.directory = directory.absolute()
        self.entries: tuple[AttachmentEntry, ...] = ()
        self.selected: list[Path] = []
        self.limit = limit
        self.loading = False
        self.loaded = False

    @staticmethod
    def read_directory(directory: Path) -> tuple[AttachmentEntry, ...]:
        entries = (
            AttachmentEntry(path, path.is_dir())
            for path in directory.iterdir()
            if not path.is_symlink() and (path.is_dir() or path.is_file())
        )
        return tuple(
            sorted(
                entries,
                key=lambda item: (not item.is_directory, item.path.name.casefold(), item.path.name),
            )
        )

    def directory_for(self, value: str) -> Path | None:
        if self.directory.parent != self.directory and value == str(self.directory.parent):
            return self.directory.parent
        return next(
            (
                entry.path
                for entry in self.entries
                if entry.is_directory and str(entry.path) == value
            ),
            None,
        )

    def toggle(self, value: str) -> None:
        path = next(
            (
                entry.path
                for entry in self.entries
                if not entry.is_directory and str(entry.path) == value
            ),
            None,
        )
        if path is None:
            return
        if path in self.selected:
            self.selected.remove(path)
        elif len(self.selected) >= self.limit:
            raise ValueError(f"You can add at most {self.limit} more attachments")
        else:
            self.selected.append(path)

    def files_for(self, value: str) -> tuple[Path, ...]:
        if self.directory_for(value) is not None:
            return ()
        if self.selected:
            return tuple(self.selected)
        return tuple(
            entry.path
            for entry in self.entries
            if not entry.is_directory and str(entry.path) == value
        )

    def arguments(self, query: str, theme: TuiTheme) -> tuple[TuiCommandArgument, ...]:
        needle = query.casefold().strip()
        arguments = []
        if not needle and self.directory.parent != self.directory:
            arguments.append(TuiCommandArgument(str(self.directory.parent), "", label=".."))
        for entry in self.entries:
            if needle not in entry.path.name.casefold():
                continue
            accent: str | None
            if entry.is_directory:
                label = entry.path.name
                accent = theme.text_secondary
            else:
                selected = entry.path in self.selected
                label = entry.path.name
                accent = theme.accent if selected else None
            arguments.append(
                TuiCommandArgument(
                    str(entry.path),
                    "",
                    label=label,
                    accent=accent,
                    checked=None if entry.is_directory else entry.path in self.selected,
                )
            )
        return tuple(arguments)

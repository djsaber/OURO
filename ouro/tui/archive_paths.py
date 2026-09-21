from __future__ import annotations

import asyncio
from pathlib import Path

from ouro.tui.screens import Choice, EditableChoiceScreen


class ArchivePathScreen(EditableChoiceScreen):
    """Browse archive files or choose an export folder using the shared menu."""

    def __init__(self, *, directory: Path, filename: str | None) -> None:
        self._directory = directory.expanduser().absolute()
        self._filename = filename
        super().__init__(
            title="SAVE ARCHIVE" if filename is not None else "OPEN ARCHIVE",
            placeholder="Filter folders and archives…",
            choices=(),
            on_save=self._save_path,
        )

    def on_mount(self) -> None:
        super().on_mount()
        self.run_worker(self._load_directory())

    async def _load_directory(self) -> None:
        self._replace_choices(await self._path_choices(), selected="location")

    async def _path_choices(self) -> tuple[Choice, ...]:
        def read() -> tuple[Choice, ...]:
            rows = [
                Choice(
                    "location",
                    "Folder",
                    trailing=str(self._directory),
                    edit_value=str(self._directory),
                    group="LOCATION",
                ),
                Choice("home", "Home folder", group="LOCATION"),
                Choice(
                    "parent",
                    "Parent folder",
                    disabled=self._directory.parent == self._directory,
                    group="LOCATION",
                ),
            ]
            if self._filename is not None:
                rows.extend(
                    (
                        Choice(
                            "filename",
                            "File name",
                            trailing=self._filename,
                            edit_value=self._filename,
                            group="ARCHIVE",
                        ),
                        Choice("save", "Use this location", group="ARCHIVE"),
                    )
                )
            archives: list[Choice] = []
            folders: list[Choice] = []
            for path in sorted(self._directory.iterdir(), key=lambda item: item.name.casefold()):
                if path.is_dir():
                    folders.append(Choice(f"directory:{path.name}", path.name, group="FOLDERS"))
                elif self._filename is None and path.is_file() and path.suffix.lower() == ".zip":
                    archives.append(Choice(f"file:{path.name}", path.name, group="ARCHIVES"))
            rows.extend(archives)
            rows.extend(folders)
            return tuple(rows)

        try:
            return await asyncio.to_thread(read)
        except OSError as exc:
            self.notify(str(exc), severity="error", markup=False)
            return (
                Choice("location", "Folder", edit_value=str(self._directory)),
                Choice("home", "Home folder"),
                Choice("parent", "Parent folder"),
            )

    async def _save_path(self, field_id: str, value: str) -> tuple[Choice, ...] | None:
        try:
            if field_id == "location":
                if not value.strip():
                    raise ValueError("Choose an existing folder")
                path = Path(value).expanduser()
                if not path.is_absolute():
                    path = self._directory / path
                if not await asyncio.to_thread(path.is_dir):
                    raise ValueError("Choose an existing folder")
                self._directory = path.absolute()
            elif field_id == "filename":
                if not value.strip() or Path(value).name != value or value in {".", ".."}:
                    raise ValueError("Enter a file name without a folder")
                self._filename = value if value.lower().endswith(".zip") else f"{value}.zip"
        except (OSError, RuntimeError, ValueError) as exc:
            self.notify(str(exc), severity="error", markup=False)
            # A rejected edit stays open so the user can correct it or cancel.
            return None
        return await self._path_choices()

    async def _select_choice(self, value: str) -> None:
        if self._editing_value is not None or value in {"location", "filename"}:
            await super()._select_choice(value)
            return
        if value == "save" and self._filename is not None:
            self.dismiss(str(self._directory / self._filename))
            return
        if value.startswith("file:"):
            self.dismiss(str(self._directory / value.removeprefix("file:")))
            return
        if value == "home":
            self._directory = Path.home()
        elif value == "parent":
            self._directory = self._directory.parent
        elif value.startswith("directory:"):
            self._directory /= value.removeprefix("directory:")
        self._replace_choices(await self._path_choices(), selected="location")

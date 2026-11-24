from __future__ import annotations

from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Protocol, cast
from weakref import ReferenceType, ref

from textual.worker import Worker

from ouro.application.transfer.service import (
    DATA_KINDS,
    DataKind,
    DataTransferItem,
    DataTransferRequest,
    DataTransferResult,
    TransferAction,
)
from ouro.core.errors import OuroError
from ouro.tui.features.transfer.paths import ArchivePathScreen
from ouro.tui.menus.choice import ChoiceScreen
from ouro.tui.menus.models import Choice
from ouro.tui.menus.multiple import MultiChoiceScreen
from ouro.tui.menus.workflows import Notify
from ouro.tui.theme import theme_for

TRANSFER_KIND_LABELS = {
    "providers": "Provider configuration",
    "provider-templates": "Provider templates",
    "mcp": "MCP configuration",
    "agents": "Agent configuration",
    "prompts": "Personal prompt configuration",
    "sessions": "Sessions and attachments",
    "skills": "Native skill files",
}


def _transfer_summary(result: DataTransferResult) -> str:
    exporting = result.action == "export"
    action = "exported to" if exporting else "imported from"
    if result.kind in {"sessions", "skills"}:
        unit = result.kind[:-1] if result.count == 1 else result.kind
        what = f"{result.count} {unit}"
    else:
        what = TRANSFER_KIND_LABELS[result.kind]
    lines = [f"{what} {action} {result.path}"]
    if result.skipped or result.replaced:
        lines.append(f"Sessions skipped: {result.skipped}. Sessions replaced: {result.replaced}.")
    if result.restart_required:
        lines.append("Restart OURO to use the imported data.")
    if result.diagnostics:
        lines.append(
            f"Sessions with external references: {len(result.diagnostics)}. "
            "You can read their history now. Continuing may require the original plugins, "
            "tools, models or files."
        )
    return "\n".join(lines)


@dataclass
class DataTransferProgress:
    request: DataTransferRequest | None = None
    running: bool = False
    result: DataTransferResult | None = None
    error: str | None = None


class DataTransferScreen(ChoiceScreen):
    """Choose transfer settings without composing flags or typing record identities."""

    def __init__(
        self,
        *,
        action: TransferAction,
        kind: DataKind | None,
        directory: Path,
        load_items: Callable[[DataTransferRequest], Awaitable[tuple[DataTransferItem, ...]]],
        identify_archive: Callable[[Path], Awaitable[DataKind]],
        progress: DataTransferProgress,
        on_submit: Callable[[DataTransferRequest], None],
        current_session_id: str | None = None,
    ) -> None:
        self._current_session_id = current_session_id
        self._action = action
        self._kind = kind
        self._directory = directory
        self._path = self._default_path() if action == "export" else None
        self._identities: tuple[str, ...] = (current_session_id,) if current_session_id else ()
        self._replace = False
        self._load_items = load_items
        self._identify_archive = identify_archive
        self._identifying = False
        self._loading_items = False
        self._archive_error: str | None = None
        self._progress = progress
        self._on_submit = on_submit
        super().__init__(
            title="EXPORT CURRENT SESSION" if current_session_id else f"{action.upper()} DATA",
            placeholder=None,
            choices=self._transfer_choices(),
            on_refresh=self._current_choices,
        )

    async def _current_choices(self) -> tuple[Choice, ...]:
        return self._transfer_choices()

    def _default_path(self) -> Path:
        return self._directory / f"ouro-{self._kind}-{datetime.now():%Y%m%d-%H%M%S}.zip"

    def _request(self) -> DataTransferRequest:
        if self._kind is None:
            raise ValueError("Choose a valid OURO archive first")
        return DataTransferRequest(
            action=self._action,
            kind=self._kind,
            path=self._path or self._directory,
            identities=self._identities,
            replace=self._replace,
        )

    async def _select_choice(self, selected: str) -> None:
        try:
            if selected == "path":
                value = await self.open_child(
                    ArchivePathScreen(
                        directory=self._path.parent if self._path else self._directory,
                        filename=self._path.name
                        if self._action == "export" and self._path
                        else None,
                    )
                )
                if value is not None and (self._action == "import" or Path(value) != self._path):
                    self._path = Path(value)
                    if self._action == "import":
                        self._kind = None
                        self._identities = ()
                        self._replace = False
                        self._archive_error = None
                        self._identifying = True
                        _worker: Worker[None] = self.run_worker(
                            partial(self._inspect_archive, self._path),
                            group="archive-inspection",
                            exclusive=True,
                        )
            elif selected == "selection" and self._current_session_id is None:
                await self._choose_selection()
            elif selected == "replace":
                self._replace = not self._replace
            elif selected == "submit" and self._path is not None and self._kind is not None:
                self._on_submit(self._request())
            self.update_choices(self._transfer_choices(), preferred_value=selected)
        except (OuroError, OSError, ValueError) as exc:
            self.notify(str(exc), severity="error", markup=False)

    async def _inspect_archive(self, path: Path) -> None:
        try:
            self._kind = await self._identify_archive(path)
        except (OuroError, OSError, ValueError) as exc:
            self._archive_error = str(exc)
        self._identifying = False
        await self.refresh_choices()

    async def _choose_selection(self) -> None:
        value = await self.open_child(
            ChoiceScreen(
                title="TRANSFER SCOPE",
                placeholder=None,
                choices=(Choice("all", "All"), Choice("selected", "Choose specific items")),
                initial_value="selected" if self._identities else "all",
            )
        )
        if value == "all":
            self._identities = ()
        elif value == "selected":
            self._loading_items = True
            _worker: Worker[None] = self.run_worker(
                partial(self._open_selection, self._request()),
                group="transfer-selection",
                exclusive=True,
            )

    async def _open_selection(self, request: DataTransferRequest) -> None:
        try:
            items = await self._load_items(request)
            if self._kind != request.kind or self._path != request.path:
                return
            if not items:
                self.notify(
                    f"No {request.kind} available to {request.action}.",
                    severity="information",
                    markup=False,
                )
                return

            async def confirm(identities: tuple[str, ...]) -> None:
                if not identities:
                    self.notify("Select at least one item, or choose All", severity="warning")
                    return
                self._identities = identities
                screen.dismiss("selected")

            theme = theme_for(self)
            screen = MultiChoiceScreen(
                title="SELECT SESSIONS" if self._kind == "sessions" else "SELECT SKILLS",
                placeholder="Search by title or name…",
                choices=tuple(
                    Choice(
                        item.identity,
                        item.label,
                        trailing=item.identity if self._kind == "sessions" else "",
                    )
                    for item in items
                ),
                on_confirm=confirm,
                selected_marker=theme.selected_choice_marker,
                unselected_marker=theme.unselected_choice_marker,
                confirm_label="Use",
                selected_values=self._identities,
            )
            await self.open_child(screen)
        except (OuroError, OSError, ValueError) as exc:
            self.notify(str(exc), severity="error", markup=False)
        finally:
            self._loading_items = False
            await self.refresh_choices()

    def _transfer_choices(self) -> tuple[Choice, ...]:
        rows = []
        if self._kind is not None:
            rows.append(
                Choice(
                    "kind",
                    "Current session"
                    if self._current_session_id
                    else ("Detected data" if self._action == "import" else "Data"),
                    trailing=self._current_session_id or TRANSFER_KIND_LABELS[self._kind],
                    disabled=True,
                    group="TRANSFER",
                )
            )
        rows.append(
            Choice(
                "path",
                "Archive file" if self._action == "import" else "Save archive to",
                trailing=str(self._path) if self._path else "Choose file",
                group="TRANSFER",
            ),
        )
        if self._identifying or self._archive_error:
            rows.append(
                Choice(
                    "archive-status",
                    "Reading archive" if self._identifying else "Cannot read archive",
                    description=self._archive_error or "",
                    loading=self._identifying,
                    disabled=True,
                    group="STATUS",
                )
            )
        if self._current_session_id is None and (
            self._kind == "sessions" or (self._action == "export" and self._kind == "skills")
        ):
            rows.append(
                Choice(
                    "selection",
                    "Sessions" if self._kind == "sessions" else "Skills",
                    trailing=f"{len(self._identities)} selected" if self._identities else "All",
                    description="Loading selection…" if self._loading_items else "",
                    loading=self._loading_items,
                    disabled=self._loading_items
                    or (self._action == "import" and self._path is None),
                    group="TRANSFER",
                )
            )
        if self._kind is not None:
            session_import = self._action == "import" and self._kind == "sessions"
            rows.append(
                Choice(
                    "replace",
                    "Existing sessions" if session_import else "Replace existing data",
                    trailing_badge=("Replace" if self._replace else "Skip")
                    if session_import
                    else ("Yes" if self._replace else "No"),
                    description="Applies to complete session trees" if session_import else "",
                    group="TRANSFER",
                )
            )
        rows.append(
            Choice(
                "submit",
                self._action.title(),
                disabled=self._path is None or self._kind is None or self._progress.running,
                group="ACTION",
            )
        )
        rows.extend(self._progress_choices())
        return tuple(rows)

    def _progress_choices(self) -> tuple[Choice, ...]:
        progress = self._progress
        request = progress.request
        if request is None:
            return ()
        action = request.action.title()
        if progress.running:
            return (
                Choice(
                    "progress",
                    f"{action} in progress",
                    description=f"{TRANSFER_KIND_LABELS[request.kind]} · {request.path}",
                    loading=True,
                    disabled=True,
                    group="STATUS",
                ),
            )
        if progress.error is not None:
            return (
                Choice(
                    "progress",
                    f"{action} failed",
                    description=progress.error,
                    description_accent=theme_for(self).danger,
                    disabled=True,
                    group="STATUS",
                ),
            )
        if progress.result is not None:
            return tuple(
                Choice(f"result:{index}", line, disabled=True, group="RESULT")
                for index, line in enumerate(_transfer_summary(progress.result).splitlines())
            )
        return ()


class TransferOperations(Protocol):
    @property
    def draft_workspace(self) -> str | None: ...

    async def data_transfer_items(
        self, request: DataTransferRequest
    ) -> tuple[DataTransferItem, ...]: ...

    async def data_archive_kind(self, path: Path) -> DataKind: ...

    async def transfer_data(self, request: DataTransferRequest) -> DataTransferResult: ...


class DataTransferWorkflow:
    """Own transfer progress while the App owns execution and screen navigation."""

    def __init__(
        self,
        *,
        controller: Callable[[], TransferOperations],
        show: Callable[[ChoiceScreen], object],
        notify: Notify,
        start: Callable[[Callable[[], Coroutine[None, None, None]]], object],
    ) -> None:
        self._controller = controller
        self._show = show
        self._notify = notify
        self._start = start
        self.progress: DataTransferProgress | None = None
        self._screen_ref: ReferenceType[DataTransferScreen] | None = None

    def export_session(self, session_id: str | None) -> None:
        if session_id is None:
            self._notify("Open the conversation you want to export.", severity="warning")
            return
        self.open("export", "sessions", current_session_id=session_id)

    def choose(self, *, menu: ChoiceScreen | None = None) -> None:
        picker: ChoiceScreen

        async def select_action(selected: str) -> None:
            action = cast(TransferAction, selected)
            if action == "import":
                self.open("import", None, menu=picker)
                return

            async def select_kind(kind: str) -> None:
                self.open(action, cast(DataKind, kind), menu=picker)

            picker.push_page(
                title=f"{action.upper()} DATA",
                placeholder=None,
                choices=tuple(Choice(kind, TRANSFER_KIND_LABELS[kind]) for kind in DATA_KINDS),
                on_select=select_kind,
            )

        choices = (Choice("import", "Import data"), Choice("export", "Export data"))
        if menu is not None:
            picker = menu
            picker.push_page(
                title="DATA",
                placeholder=None,
                choices=choices,
                on_select=select_action,
            )
        else:
            picker = ChoiceScreen(
                title="DATA",
                placeholder=None,
                choices=choices,
                on_select=select_action,
            )
            self._show(picker)

    def open(
        self,
        action: TransferAction,
        kind: DataKind | None,
        *,
        menu: ChoiceScreen | None = None,
        current_session_id: str | None = None,
    ) -> None:
        if self.progress is None:
            self.progress = DataTransferProgress()
        screen = DataTransferScreen(
            action=action,
            kind=kind,
            directory=Path(self._controller().draft_workspace or Path.cwd()),
            load_items=self._controller().data_transfer_items,
            identify_archive=self._controller().data_archive_kind,
            progress=self.progress,
            on_submit=self.submit,
            current_session_id=current_session_id,
        )
        self._screen_ref = ref(screen)
        if menu is not None:
            menu.show_child(screen)
        else:
            self._show(screen)

    def submit(self, request: DataTransferRequest) -> None:
        progress = self.progress
        assert progress is not None
        if progress.running:
            return
        progress.request = request
        progress.running = True
        progress.result = None
        progress.error = None
        # The App owns execution so closing a menu cannot cancel a file commit.
        self._start(partial(self._run, request))

    async def _run(self, request: DataTransferRequest) -> None:
        progress = self.progress
        assert progress is not None
        try:
            progress.result = await self._controller().transfer_data(request)
        except Exception as exc:  # noqa: BLE001 - surface operation errors inside the menu.
            progress.error = str(exc)
        finally:
            progress.running = False
        if progress.error is not None:
            self._notify(
                progress.error,
                title=f"{request.action.title()} failed",
                severity="error",
                timeout=10,
                markup=False,
            )
        elif progress.result is not None:
            self._notify(
                _transfer_summary(progress.result),
                title=f"{request.action.title()} completed",
                timeout=6,
                markup=False,
            )
        screen = self._screen_ref() if self._screen_ref else None
        if screen is not None:
            await screen.refresh_choices()

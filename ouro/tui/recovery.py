"""Offline data recovery presentation; the caller owns runtime and home access."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from threading import Lock
from typing import Any, cast

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.messages import Prune
from textual.widgets import OptionList, Static

from ouro.core.errors import OuroError
from ouro.deployment import SessionBackend, TaskBackend
from ouro.tui.menus.choice import ChoiceScreen
from ouro.tui.menus.confirmation import ConfirmationScreen
from ouro.tui.menus.models import Choice
from ouro.tui.theme import theme_for
from ouro_upgrade.engine import pending_operation, upgrade
from ouro_upgrade.errors import UpgradeError
from ouro_upgrade.progress import UpgradePhase, UpgradeProgress
from ouro_upgrade.units import UpgradeFailure, UpgradeUnit

_PHASE_LABELS: dict[UpgradePhase, str] = {
    "configuration": "Checking and upgrading configuration",
    "sessions": "Checking and upgrading sessions",
    "tasks": "Checking and upgrading tasks",
    "usage": "Checking and upgrading usage records",
    "verify_source": "Verifying source data",
    "prepare": "Preparing publication",
    "finalize": "Finalizing upgrade",
    "publish": "Writing upgraded files",
    "verify": "Verifying data",
    "restore": "Restoring original files",
    "complete": "Complete",
}

_RECOVERY_CSS = """
    DataRecoveryScreen, _DiscardScreen {
        background: $ouro-background;
        color: $ouro-text-primary;
    }
    #choice-panel {
        background: transparent;
        border: none;
        min-height: 0;
        max-height: 90%;
    }
    #choice-title, #choice-filter, .grouped-menu-list, #choice-hint {
        border: none;
    }
    #choice-filter { display: none; }
    #choice-list { min-height: 1; padding: 1 2; }
    #recovery-home, #recovery-status {
        height: auto;
        padding: 1 2 0 2;
    }
    #recovery-home { color: $ouro-text-lowlight; max-height: 4; }
    #recovery-status { max-height: 7; overflow-y: auto; }
"""


def _unit_label(unit: UpgradeUnit) -> str:
    labels = {
        "session": "Session",
        "task": "Task",
        "configuration": "Configuration",
        "session_store": "Session store",
        "task_store": "Task store",
        "usage": "Usage ledger",
    }
    return f"{labels[unit.kind]}: {unit.identity}"


class _DiscardScreen(ConfirmationScreen):
    """Show the identified deletion boundaries; successful data stays in place."""

    DEFAULT_CSS = _RECOVERY_CSS

    def __init__(self, home: Path, units: tuple[UpgradeUnit, ...]) -> None:
        super().__init__(
            title="DELETE FAILED DATA",
            message="Delete only the listed data that still fails the upgrade?",
            confirm_label="Delete failed data and retry",
        )
        self._home = home
        self._units = units

    def _context_widgets(self) -> ComposeResult:
        yield Static(Text(str(self._home)), id="recovery-home")
        yield Static(
            Text(
                "Only the following failed units will be removed from active storage, "
                "including their owned history and files. Successful data is kept.\n"
                + "\n".join(_unit_label(unit) for unit in self._units)
                + "\nStop all other OURO processes first.",
                style=theme_for(self).danger,
            ),
            id="recovery-status",
        )


class DataRecoveryScreen(ChoiceScreen):
    """Present progress and explicit failure recovery without constructing Runtime."""

    DEFAULT_CSS = _RECOVERY_CSS
    BINDINGS = [Binding("ctrl+q,ctrl+c", "app.quit", "Quit", show=False, priority=True)]

    def __init__(
        self,
        *,
        home: Path,
        session_backend: SessionBackend | None = None,
        task_backend: TaskBackend | None = None,
    ) -> None:
        self.home = home
        self._session_backend = session_backend
        self._task_backend = task_backend
        self._failures: tuple[UpgradeFailure, ...] = ()
        self._busy = False
        self._operation: asyncio.Task[dict[str, object]] | None = None
        self._progress_lock = Lock()
        self._latest_progress: UpgradeProgress | None = None
        self._displayed_progress: UpgradeProgress | None = None
        self._completion: asyncio.Future[bool] | None = None
        super().__init__(
            title="OURO DATA UPGRADE",
            placeholder="",
            choices=(),
            on_select=self._select,
            on_cancel=self._cancel,
            interaction_hint="↑/↓ move · Enter select · Esc exit",
        )

    def _context_widgets(self) -> ComposeResult:
        yield Static(Text(str(self.home)), id="recovery-home")
        yield Static(id="recovery-status")

    def on_mount(self) -> None:
        super().on_mount()
        self.query_one("#choice-list", OptionList).focus()
        self._progress_timer = self.set_interval(0.1, self._refresh_progress, pause=True)

    async def recover(self, error: str, *, auto_upgrade: bool) -> bool:
        """Wait for recovery, retaining this surface until the caller is ready."""
        self._completion = asyncio.get_running_loop().create_future()
        self._busy = False
        await self.wait_for_refresh()
        if auto_upgrade:
            self.run_worker(self._upgrade(), name="data-upgrade")
        else:
            self._show_failure(error)
        return await self._completion

    @property
    def recovering(self) -> bool:
        return self._completion is not None and not self._completion.done()

    def show_error(self, message: str) -> None:
        self._busy = False
        self._status(message, failed=True)
        self.update_choices((Choice("exit", "Exit"),))

    def show_exiting(self) -> None:
        self._status("Exiting…")
        self.update_choices(())

    def show_starting(self) -> None:
        self._busy = True
        self._status("Data is ready. Starting OURO…")
        self.update_choices((Choice("progress", "Starting…", loading=True, disabled=True),))
        self.query_one("#choice-hint", Static).update("Please wait…")

    async def on_prune(self, event: Prune) -> None:
        if self._completion is not None and not self._completion.done():
            self._completion.cancel()
        await super().on_prune(event)

    @on(events.Unmount)
    async def _wait_for_operation(self) -> None:
        # Thread work cannot be cancelled safely. Even external App shutdown must
        # finish it before another Host may start using this home.
        if self._operation is not None:
            await asyncio.gather(self._operation, return_exceptions=True)

    def _status(self, message: str, *, failed: bool = False) -> None:
        self.query_one("#recovery-status", Static).update(
            Text(message, style=theme_for(self).failure if failed else theme_for(self).text_primary)
        )

    def _show_failure(self, error: str) -> None:
        if not self.recovering:
            return
        pending = pending_operation(self.home)
        if pending == "reset":
            error = "An earlier full reset was interrupted. Manual recovery is required."
        if self._failures:
            error += "\n" + "\n".join(
                f"{_unit_label(failure.unit)} — {failure.error}" for failure in self._failures
            )
        self._status(error, failed=True)
        choices = []
        if pending == "upgrade":
            choices.append(
                Choice(
                    "restore",
                    "Restore unfinished changes and retry",
                    "Restore only the interrupted operation. Earlier successful upgrades are kept.",
                )
            )
        elif pending is None:
            choices.append(Choice("retry", "Retry upgrade", "Check and upgrade remaining data."))
            if any(failure.discardable for failure in self._failures):
                choices.append(
                    Choice(
                        "discard",
                        "Delete failed data and retry",
                        "Review failed units before deletion. Successful data is kept.",
                        accent=theme_for(self).danger,
                        destructive=True,
                    )
                )
        choices.append(Choice("exit", "Exit", "Keep data for manual recovery."))
        self.update_choices(tuple(choices), preferred_value="exit", title="OURO DATA RECOVERY")
        self.query_one("#choice-hint", Static).update("↑/↓ move · Enter select · Esc exit")
        self.query_one("#choice-list", OptionList).focus()

    async def _select(self, selected: str) -> None:
        if self._busy:
            return
        if selected == "exit":
            self._cancel()
        elif selected == "restore":
            await self._upgrade(restore=True)
        elif selected == "retry":
            await self._upgrade()
        elif selected == "discard":
            units = tuple(failure.unit for failure in self._failures if failure.discardable)
            confirmation = await self.open_child(_DiscardScreen(self.home, units))
            if ConfirmationScreen.confirmed(confirmation):
                await self._upgrade(discard=frozenset(units))

    async def _upgrade(
        self,
        *,
        restore: bool = False,
        discard: frozenset[UpgradeUnit] = frozenset(),
    ) -> None:
        def perform() -> dict[str, object]:
            if restore:
                upgrade(self.home, restore=True, progress=self._receive_progress)
            return upgrade(
                self.home,
                session_backend=self._session_backend,
                task_backend=self._task_backend,
                progress=self._receive_progress,
                isolate_failures=True,
                discard=discard,
            )

        await self._run_operation(perform)

    async def _run_operation(self, operation: Callable[[], dict[str, object]]) -> None:
        self._failures = ()
        self._busy = True
        with self._progress_lock:
            self._latest_progress = None
        self._displayed_progress = None
        self._status("Preparing data. Keep this terminal open until the operation finishes.")
        self.update_choices(
            (Choice("progress", "Starting…", loading=True, disabled=True),),
            title="OURO DATA UPGRADE",
        )
        self.query_one("#choice-hint", Static).update("Please wait…")
        self._progress_timer.resume()
        self._operation = asyncio.create_task(asyncio.to_thread(operation))
        try:
            report = await asyncio.shield(self._operation)
        except (UpgradeError, OuroError) as exc:
            self._show_failure(str(exc))
        except Exception as exc:  # noqa: BLE001 - contain worker failures without exposing payloads.
            self._show_failure(f"Data maintenance failed ({type(exc).__name__}).")
        else:
            self._failures = tuple(
                UpgradeFailure(UpgradeUnit(**item["unit"]), item["error"], item["discardable"])
                for item in cast(list[dict[str, Any]], report.get("failures", []))
            )
            if self._failures:
                self._show_failure(
                    "Some data could not be upgraded. Successful upgrades are saved."
                )
            else:
                self._finish(True)
        finally:
            self._progress_timer.pause()
            self._busy = False

    def _receive_progress(self, progress: UpgradeProgress) -> None:
        # Coalesce worker updates so large homes cannot flood the UI message queue.
        with self._progress_lock:
            self._latest_progress = progress

    def _refresh_progress(self) -> None:
        with self._progress_lock:
            progress = self._latest_progress
        if progress is None or progress == self._displayed_progress:
            return
        self._displayed_progress = progress
        label = _PHASE_LABELS[progress.phase]
        if progress.total is not None:
            label += f" ({progress.completed}/{progress.total})"
        self.update_choices((Choice("progress", label, loading=True, disabled=True),))

    def _finish(self, upgraded: bool) -> None:
        if self._completion is not None and not self._completion.done():
            self._completion.set_result(upgraded)

    def _cancel(self) -> bool:
        if not self._busy:
            if self.recovering:
                self._finish(False)
            else:
                self.app.exit(1)
        return True

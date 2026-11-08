"""Own revision tracking and asynchronous persistence of TUI input drafts."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from ouro.core.errors import ConfigError, OuroError
from ouro.tui.composer.drafts import TuiInputDrafts, TuiInputDraftStore

_INPUT_DRAFT_SAVE_DEBOUNCE_SECONDS = 0.3


class InputDraftPersistence:
    """Save the latest Host snapshot while owning only disk-write progress.

    The Host decides which edits are persistent and which snapshot to save.
    Submission flushes and shutdown waits share the same owned task.
    """

    def __init__(
        self,
        store: TuiInputDraftStore,
        snapshot: Callable[[], TuiInputDrafts],
        on_error: Callable[[OuroError], None],
    ) -> None:
        self._store = store
        self._snapshot = snapshot
        self._on_error = on_error
        self._revision = 0
        self._saved_revision = 0
        self._task: asyncio.Task[None] | None = None
        self._save_requested = asyncio.Event()

    def changed(self) -> None:
        self._revision += 1
        task = self._task
        if task is None or task.done():
            self._task = asyncio.create_task(self._persist(), name="tui-input-drafts")

    async def wait(self) -> None:
        """Join an existing save without changing its debounce window."""
        if self._task is not None:
            await self._task

    async def flush(self) -> None:
        if self._saved_revision == self._revision:
            return
        self._save_requested.set()
        if self._task is None:
            self._task = asyncio.create_task(self._persist())
        # Closing the Host cancels workers, but its pending disk save must finish.
        await asyncio.shield(self._task)
        if self._saved_revision < self._revision:
            raise ConfigError("Could not save the input draft; retry before sending")

    async def _persist(self) -> None:
        try:
            while True:
                revision = self._revision
                try:
                    await asyncio.wait_for(
                        self._save_requested.wait(), _INPUT_DRAFT_SAVE_DEBOUNCE_SECONDS
                    )
                except TimeoutError:
                    if revision != self._revision:
                        continue
                self._save_requested.clear()
                revision = self._revision
                drafts = self._snapshot()
                try:
                    await asyncio.to_thread(self._store.save, drafts)
                    self._saved_revision = revision
                except OuroError as exc:
                    self._on_error(exc)
                    if revision != self._revision:
                        continue
                    return
                if revision == self._revision:
                    return
        finally:
            self._task = None

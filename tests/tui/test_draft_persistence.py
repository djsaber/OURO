from __future__ import annotations

import asyncio
import threading

import pytest

from ouro.core.errors import ConfigError, OuroError, PersistenceError
from ouro.tui.composer.draft_persistence import InputDraftPersistence
from ouro.tui.composer.drafts import TuiInputDrafts, TuiInputDraftStore


async def test_flush_includes_edits_made_during_a_pending_disk_write(tmp_path, monkeypatch):
    store = TuiInputDraftStore(tmp_path)
    drafts = TuiInputDrafts().remember(None, "first")
    errors: list[OuroError] = []
    persistence = InputDraftPersistence(store, lambda: drafts, errors.append)
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    saved: list[TuiInputDrafts] = []
    save = store.save

    def delayed(snapshot):
        if not saved:
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(10)
        save(snapshot)
        saved.append(snapshot)

    monkeypatch.setattr(store, "save", delayed)
    persistence.changed()
    flushing = asyncio.create_task(persistence.flush())
    try:
        await asyncio.wait_for(entered.wait(), 10)
        drafts = drafts.remember(None, "latest")
        persistence.changed()
    finally:
        release.set()
        await asyncio.wait_for(flushing, 10)

    assert [snapshot.draft_for(None) for snapshot in saved] == ["first", "latest"]
    assert store.load() == drafts
    assert errors == []


async def test_failed_flush_can_retry_the_same_draft_without_another_edit(tmp_path, monkeypatch):
    store = TuiInputDraftStore(tmp_path)
    drafts = TuiInputDrafts().remember(None, "retry this submission")
    errors: list[OuroError] = []
    persistence = InputDraftPersistence(store, lambda: drafts, errors.append)
    failure = PersistenceError("disk unavailable")

    def fail(snapshot):
        raise failure

    persistence.changed()
    with monkeypatch.context() as patch:
        patch.setattr(store, "save", fail)
        with pytest.raises(ConfigError, match="Could not save the input draft"):
            await persistence.flush()

    assert errors == [failure]
    await persistence.flush()
    assert store.load() == drafts

import asyncio

import pytest
from textual.app import App
from textual.worker import WorkerFailed

from ouro.tui.menus.choice import ChoiceScreen
from ouro.tui.menus.models import Choice
from tests.tui.helpers import wait_for_screen, wait_for_worker_group, wait_for_workers


async def test_worker_group_completion_does_not_wait_for_application_lifetime():
    app = App()
    release = asyncio.Event()
    entered = asyncio.Event()

    async def operation():
        entered.set()
        await release.wait()

    async with app.run_test() as pilot:
        observer = app.run_worker(asyncio.Event().wait(), group="observer")
        worker = app.run_worker(operation(), group="operation")
        try:
            await asyncio.wait_for(entered.wait(), 5)
            # Empty explicit lists and groups must never expand to all workers.
            await wait_for_workers(app, [], timeout=1)
            await wait_for_worker_group(app, pilot, group="absent", timeout=1)
            waiting = asyncio.create_task(wait_for_worker_group(app, pilot, group="operation"))
            await pilot.pause()
            assert not waiting.done()
            release.set()
            await waiting
            assert worker.is_finished
            assert observer.is_running
            # Once the completed worker has left the manager the same wait is empty.
            await wait_for_worker_group(app, pilot, group="operation", timeout=1)
            assert observer.is_running
        finally:
            release.set()


async def test_worker_timeout_reports_pending_operation_without_cancelling_it():
    app = App()
    release = asyncio.Event()
    async with app.run_test():
        worker = app.run_worker(release.wait(), name="held-operation", group="operation")
        try:
            with pytest.raises(AssertionError, match="held-operation.*operation.*page="):
                await wait_for_workers(app, [worker], timeout=0)
            assert not worker.is_cancelled
            release.set()
            await wait_for_workers(app, [worker])
        finally:
            release.set()


async def test_worker_wait_preserves_operation_failure():
    async def fail():
        raise ValueError("operation failed")

    app = App()
    async with app.run_test():
        worker = app.run_worker(fail(), exit_on_error=False)
        with pytest.raises(WorkerFailed, match="operation failed"):
            await wait_for_workers(app, [worker])


async def test_child_selection_waits_for_mount_and_returns_result():
    entered, release = asyncio.Event(), asyncio.Event()

    class DelayedMenu(ChoiceScreen):
        async def on_mount(self):
            entered.set()
            await release.wait()
            super().on_mount()

    parent = ChoiceScreen(title="PARENT", placeholder="", choices=(Choice("child", "Child"),))
    child = DelayedMenu(title="CHILD", placeholder="", choices=(Choice("value", "Value"),))
    app = App()
    async with app.run_test():
        await app.push_screen(parent)
        selection = app.run_worker(parent.open_child(child))
        ready = asyncio.create_task(wait_for_screen(app, DelayedMenu, title="CHILD"))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            assert app.screen is child and not child.is_mounted
            assert not ready.done()
        finally:
            release.set()
        await ready
        await child._select_choice("value")
        await wait_for_workers(app, [selection])
        assert selection.result == "value"

import asyncio
from types import SimpleNamespace

import pytest

from ouro.tui.app import OuroTuiApp
from tests.tui.helpers import run_ready_app
from tests.tui.test_app import _application


async def test_switching_away_closes_tree_that_finishes_subscribing_late(tmp_path, monkeypatch):
    app = OuroTuiApp(application=_application(tmp_path))
    entered, release = asyncio.Event(), asyncio.Event()
    closed = []
    tree = SimpleNamespace(close=lambda: closed.append(True))

    async def delayed_subscribe(root_session_id):
        entered.set()
        await release.wait()
        return tree

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        monkeypatch.setattr(app._controller, "subscribe_agent_tree", delayed_subscribe)
        opening = asyncio.create_task(app._replace_agent_tree("old-root"))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await app._replace_agent_tree(None)
        finally:
            release.set()
        assert await opening == ()
        assert closed == [True]
        assert app._agent_tree_subscription is None


async def test_cancelled_session_installation_releases_its_live_subscription(tmp_path, monkeypatch):
    application = _application(tmp_path)
    session = application.runtime.sessions.create(title="Cancelled attachment").session
    app = OuroTuiApp(application=application)
    entered = asyncio.Event()
    subscriptions = []

    async def delayed_install(loaded, *, subscription):
        subscriptions.append(subscription)
        entered.set()
        await asyncio.Event().wait()

    async with run_ready_app(app, size=(100, 30)):
        monkeypatch.setattr(app, "_install_session_surface", delayed_install)
        opening = asyncio.create_task(app._open_session(session.session_id))
        await asyncio.wait_for(entered.wait(), 3)
        opening.cancel()
        with pytest.raises(asyncio.CancelledError):
            await opening
        assert subscriptions[0].closed
        await asyncio.gather(*application.supervisor._event_stream_tasks)
        assert not application.supervisor._event_streams

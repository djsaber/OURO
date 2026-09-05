import asyncio
from types import SimpleNamespace

from ouro.tui.app import OuroTuiApp
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

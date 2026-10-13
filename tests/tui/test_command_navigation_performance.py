from __future__ import annotations

import pytest

from ouro.tui.app import OuroTuiApp
from ouro.tui.widgets import CommandPopup, Composer
from tests.tui.helpers import run_ready_app
from tests.tui.test_history_commands import application_at


@pytest.mark.parametrize(
    "command", ("/navigate ", "/navigate t", "/approval a", "/workspace ", "/model ")
)
async def test_arrow_navigation_reuses_displayed_command_candidates(tmp_path, monkeypatch, command):
    app = OuroTuiApp(application=application_at(tmp_path))
    async with run_ready_app(app) as pilot:
        composer = app.query_one(Composer)
        composer.load_command(command)
        await pilot.pause()
        popup = app.query_one(CommandPopup)
        assert popup.display and popup.argument_command is not None
        calls = 0
        original = app._command_catalog

        def counted_catalog():
            nonlocal calls
            calls += 1
            return original()

        monkeypatch.setattr(app, "_command_catalog", counted_catalog)
        await pilot.press("down", "down", "up", "up")
        assert composer.text == command
        assert calls == 0

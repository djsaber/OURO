"""TUI application scenarios: plugins."""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import Input, OptionList

from ouro.core.events import EventRecord
from ouro.core.messages import MessageRecord
from ouro.tui.app import OuroTuiApp
from ouro.tui.conversation.history import ConversationHistory
from ouro.tui.features.plugins.install import PluginInstallScreen
from ouro.tui.state import TuiSessionProjection
from tests.tui.app.support import _application
from tests.tui.helpers import run_ready_app
from tests.tui.helpers import wait_for_screen as _wait_for_screen


@pytest.mark.asyncio
async def test_plugin_message_event_renders_non_scaffold_user_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    visible = MessageRecord.text(
        session_id="ses_plugin_input",
        role="user",
        text="Ship through Goal",
        scaffold=False,
        sequence=1,
    )
    scaffold = MessageRecord.text(
        session_id="ses_plugin_input",
        role="user",
        text="internal goal instruction",
        scaffold=True,
        semantic_tag="goal-create",
        sequence=2,
    )
    by_id = {message.message_id: message for message in (visible, scaffold)}

    async def queued_user_inputs(_session_id: str):
        return ()

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        monkeypatch.setattr(app._controller, "queued_user_inputs", queued_user_inputs)
        events = tuple(
            EventRecord(
                session_id="ses_plugin_input",
                event_type="plugin_message_added",
                payload={"message_id": item.message_id},
                sequence=index,
            )
            for index, item in enumerate((scaffold, visible), start=1)
        )

        app._projection = TuiSessionProjection("ses_plugin_input")
        await app._apply_runtime_event_batch(events, input_messages=by_id)

        entries = app.query_one(ConversationHistory).entries
        assert [(entry.kind, entry.text) for entry in entries] == [("user", "Ship through Goal")]


@pytest.mark.asyncio
async def test_plugin_install_failure_uses_error_toast_without_changing_conversation(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 34), notifications=True) as pilot:
        await pilot.pause()
        history = app.query_one(ConversationHistory)
        entries_before = history.entries

        await pilot.press("ctrl+p")
        await pilot.pause()
        app.screen.query_one("#choice-filter", Input).value = "Install plugin"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, PluginInstallScreen, title="INSTALL PLUGIN")

        install_screen = app.screen
        options = install_screen.query_one("#choice-list", OptionList)
        options.highlighted = options.get_option_index("plugin-source")
        options.focus()
        await pilot.press("enter", *"missing-plugin-source", "enter")
        await pilot.pause()
        options.highlighted = options.get_option_index("plugin-install")
        options.focus()
        await pilot.press("enter")
        await pilot.pause()

        toast = app.screen.query_one("#textual-toastrack").query_one("Toast")
        assert toast.has_class("-error")
        assert "plugin source does not exist" in toast.render().plain
        assert history.entries == entries_before
        assert "plugin source does not exist" not in history.rendered_text

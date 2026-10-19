from __future__ import annotations

from datetime import UTC, datetime
from threading import Event

import pytest
from rich.console import Console
from rich.style import Style

from ouro.core.attachment_input import separate_attachments
from ouro.core.errors import ConfigError
from ouro.core.events import EventRecord
from ouro.core.input_content import InputAttachment
from ouro.core.messages import MessageRecord
from ouro.tui.app import OuroTuiApp
from ouro.tui.conversation_history import ConversationHistory, _MessageContent
from ouro.tui.preferences import TuiPreferencesStore
from ouro.tui.screens import ChoiceScreen
from ouro.tui.state import TuiSessionProjection
from ouro.tui.theme import TUI_THEMES
from ouro.tui.widgets import Composer, ConversationView, project_transcript_entries
from tests.tui.helpers import choose_menu, run_ready_app, wait_for_screen, wait_until
from tests.tui.test_app import _application, _persist_direct_child_session
from tests.tui.test_background_navigation import NavigationProvider, submit
from tests.tui.test_tasks import streaming_application


@pytest.mark.parametrize("theme", TUI_THEMES.values(), ids=lambda theme: theme.theme_id)
@pytest.mark.parametrize("highlighted", [False, True])
def test_scaffold_frame_uses_lowlight_body_and_border_without_changing_layout(theme, highlighted):
    console = Console(force_terminal=True, color_system="truecolor", width=36)
    options = dict(
        text="Long wrapped message with 中文 and **literal markup**\nSecond line",
        theme=theme,
        created_at=datetime(2026, 10, 19, tzinfo=UTC),
        entry_index=2,
        highlighted=highlighted,
    )
    user = console.render_lines(_MessageContent(**options))
    scaffold = console.render_lines(_MessageContent(**options, lowlight=True))
    assert len(user) == len(scaffold)
    body_segments = 0
    for normal, internal in zip(user, scaffold, strict=True):
        for normal_segment, internal_segment in zip(normal[1:], internal[1:], strict=True):
            assert normal_segment.text == internal_segment.text
            expected_style = normal_segment.style
            if expected_style.color == Style(color=theme.text_highlight).color:
                expected_style += Style(color=theme.text_lowlight)
                body_segments += 1
            assert internal_segment.style == expected_style
        assert normal[0].text == internal[0].text == "│"
        assert internal[0].style.color.triplet.hex == theme.text_lowlight
        assert normal[0].style.color.triplet.hex == theme.accent
        assert normal[0].style.meta == internal[0].style.meta
    assert body_segments > 0


@pytest.mark.parametrize("prompt", ["", "Read this file"])
def test_revealing_attachment_scaffolds_preserves_file_labels_and_user_targets(prompt):
    messages = tuple(
        separate_attachments(
            MessageRecord.text(session_id="s", role="user", text=prompt),
            (InputAttachment(resource_id="resource", name="notes.txt"),),
        )
    )
    results = []
    for enabled in (False, True):
        entries, _ = project_transcript_entries(
            messages,
            (),
            (),
            mutable_tool_call_keys=frozenset(),
            visible_scaffold_delegation_id=None,
            show_scaffold_messages=enabled,
        )
        assert len([entry for entry in entries if entry.kind == "user"]) == 1
        assert all("notes.txt" in entry.text for entry in entries)
        results.append(entries)
    assert "User provided the following files" not in results[0][0].text
    assert "User provided the following files" in results[1][0].text


@pytest.mark.parametrize("enabled", [False, True])
async def test_live_inputs_and_loaded_history_share_scaffold_projection(tmp_path, enabled):
    application = _application(tmp_path)
    application.runtime.sessions.create(session_id="ses_inputs")
    app = OuroTuiApp(home=tmp_path, application=application)
    messages = tuple(
        MessageRecord.text(
            session_id="ses_inputs",
            role=role,
            text=text,
            scaffold=scaffold,
            sequence=index,
        )
        for index, (role, text, scaffold) in enumerate(
            (
                ("system", "Environment instructions", True),
                ("user", "Goal instructions", True),
                ("assistant", "Internal assistant scaffold", True),
                ("user", "Actual input", False),
            ),
            start=1,
        )
    )
    async with run_ready_app(app):
        await app._set_scaffold_messages(enabled)
        app._projection = TuiSessionProjection("ses_inputs")
        events = tuple(
            EventRecord(
                session_id=message.session_id,
                event_type="plugin_message_added",
                payload={"message_id": message.message_id},
                sequence=index,
            )
            for index, message in enumerate(messages, start=1)
        )
        await app._apply_runtime_event_batch(
            events, input_messages={message.message_id: message for message in messages}
        )
        history = app.query_one(ConversationHistory)
        expected, _ = project_transcript_entries(
            messages,
            (),
            (),
            mutable_tool_call_keys=frozenset(),
            visible_scaffold_delegation_id=None,
            show_scaffold_messages=enabled,
        )
        assert history.entries == expected
        assert [entry.text for entry in expected] == [
            message.text_content for message in messages if enabled or not message.scaffold
        ]
        await history.wait_until_rendered()
        assert history.user_turns == ("Actual input",)
        assert len(history.user_lines) == 1
        assert [argument.value for argument in app._user_message_arguments()] == ["#1"]


@pytest.mark.parametrize("session_kind", ["main", "subagent", "task"])
async def test_settings_toggle_refreshes_visible_session_and_survives_navigation(
    tmp_path, session_kind
):
    application = _application(tmp_path)
    root_id, child_id = _persist_direct_child_session(application)
    task = application.runtime.sessions.create(title="Scheduled", namespace="task").session
    target_id = {"main": root_id, "subagent": child_id, "task": task.session_id}[session_kind]
    for session_id in (root_id, child_id, task.session_id):
        application.runtime.store.append_messages(
            [
                MessageRecord.text(
                    session_id=session_id, role="user", text=f"Internal {session_id}", scaffold=True
                ),
                MessageRecord.text(session_id=session_id, role="user", text=f"User {session_id}"),
            ]
        )
    app = OuroTuiApp(home=tmp_path, application=application)
    async with run_ready_app(app, size=(100, 34)) as pilot:
        await app._open_session(root_id)
        await app._show_observed_session(target_id)
        history = app.query_one(ConversationHistory)
        assert not any(entry.scaffold for entry in history.entries)
        controlled_id = app._controller.session_id
        await pilot.press("ctrl+p")
        if session_kind == "subagent":
            await pilot.pause()
            assert {choice.label for choice in app.screen._choices} == {
                "Theme",
                "Thinking display",
                "Scaffold messages",
            }
        await choose_menu(pilot, "Scaffold messages", title="SCAFFOLD MESSAGES")
        menu = app.screen
        assert [choice.label for choice in menu._choices] == ["Off", "On"]
        assert menu._choices[0].marker
        await choose_menu(pilot, "On")
        await wait_until(
            lambda: any(entry.scaffold for entry in history.entries),
            pilot,
            description="scaffold messages displayed",
        )
        assert app.screen is menu
        assert app._controller.session_id == controlled_id
        assert app._visible_session_id() == target_id
        assert app.query_one(Composer).disabled is (session_kind == "subagent")
        assert TuiPreferencesStore(tmp_path).load().show_scaffold_messages
        await pilot.press("escape", "escape")
        for session_id in (root_id, child_id, task.session_id, target_id):
            await app._show_observed_session(session_id)
            assert f"Internal {session_id}" in [entry.text for entry in history.entries]
        assert await app._set_scaffold_messages(False)
        for session_id in (root_id, child_id, task.session_id, target_id):
            await app._show_observed_session(session_id)
            assert not any(entry.scaffold for entry in history.entries)
        assert await app._set_scaffold_messages(True)

    restarted = OuroTuiApp(home=tmp_path, application=_application(tmp_path))
    async with run_ready_app(restarted):
        await restarted._open_session(target_id)
        assert any(entry.scaffold for entry in restarted.query_one(ConversationHistory).entries)


async def test_toggle_preserves_loaded_history_window_and_projects_earlier_pages(tmp_path):
    application = _application(tmp_path)
    session = application.runtime.sessions.create(title="Long history").session
    application.runtime.store.append_messages(
        [
            MessageRecord.text(
                session_id=session.session_id,
                role="user",
                text=f"Message {index}",
                scaffold=index % 2 == 0,
            )
            for index in range(405)
        ]
    )
    app = OuroTuiApp(home=tmp_path, application=application)
    async with run_ready_app(app):
        await app._open_session(session.session_id)
        assert await app._set_scaffold_messages(True)
        assert await app._prepend_history_window(app._view_revision)
        history = app.query_one(ConversationHistory)
        texts = [entry.text for entry in history.entries]
        assert len(texts) == 400
        assert await app._set_scaffold_messages(False)
        assert [entry.text for entry in history.entries] == [
            text for text in texts if int(text.split()[-1]) % 2
        ]
        assert await app._set_scaffold_messages(True)
        assert [entry.text for entry in history.entries] == texts
        assert await app._prepend_history_window(app._view_revision)
        assert [entry.text for entry in history.entries] == [
            f"Message {index}" for index in range(405)
        ]


async def test_failed_preference_save_keeps_scaffold_visibility_unchanged(tmp_path, monkeypatch):
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))
    async with run_ready_app(app):

        def fail_save(_preferences):
            raise ConfigError("Cannot save preferences")

        monkeypatch.setattr(app._preferences_store, "save", fail_save)
        assert not await app._set_scaffold_messages(True)
        assert not app._preferences.show_scaffold_messages
        assert not app.query_one(ConversationView).show_scaffold_messages


async def test_closing_menu_does_not_interrupt_scaffold_preference_commit(tmp_path, monkeypatch):
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))
    entered, release = Event(), Event()
    save = app._preferences_store.save

    def held_save(preferences):
        if preferences.show_scaffold_messages:
            entered.set()
            assert release.wait(10)
        save(preferences)

    async with run_ready_app(app) as pilot:
        await pilot.press("ctrl+p")
        await wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        await choose_menu(pilot, "Scaffold messages", title="SCAFFOLD MESSAGES")
        monkeypatch.setattr(app._preferences_store, "save", held_save)
        try:
            await choose_menu(pilot, "On")
            await wait_until(entered.is_set, pilot, description="preference write started")
            await pilot.press("escape", "escape")
            assert not isinstance(app.screen, ChoiceScreen)
        finally:
            release.set()
        await wait_until(
            lambda: app.query_one(ConversationView).show_scaffold_messages,
            pilot,
            description="preference applied after closing menu",
        )
        assert TuiPreferencesStore(tmp_path).load().show_scaffold_messages


@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_toggle_during_stream_preserves_tail_draft_and_event_handoff(tmp_path, backend):
    provider = NavigationProvider()
    application = streaming_application(tmp_path, provider, backend=backend)
    session = application.runtime.sessions.create(title="Streaming").session
    application.runtime.store.append_message(
        MessageRecord.text(
            session_id=session.session_id, role="user", text="Internal context", scaffold=True
        )
    )
    app = OuroTuiApp(home=tmp_path, application=application)
    async with run_ready_app(app) as pilot:
        await app._open_session(session.session_id)
        _, worker = await submit(app, pilot, provider, "Work")
        composer = app.query_one(Composer)
        composer.load_text("Unsent draft")
        try:
            for enabled in (True, False, True):
                assert await app._set_scaffold_messages(enabled)
                assert composer.text == "Unsent draft"
                assert app._projection.assistant_text == "Work prefix. "
                assert (
                    any(
                        entry.text == "Internal context"
                        for entry in app.query_one(ConversationHistory).entries
                    )
                    is enabled
                )
                assert not worker.is_finished
            provider.text("Work", "Continued after toggle.")
            await wait_until(
                lambda: "Continued after toggle." in app._projection.assistant_text,
                pilot,
                description="stream after scaffold setting change",
            )
        finally:
            provider.finish("Work")
            await worker.wait()
        await wait_until(
            lambda: app._projection.activity.status == "completed",
            pilot,
            description="stream completion",
        )
        text = "".join(
            entry.text
            for entry in app.query_one(ConversationHistory).entries
            if entry.kind == "assistant"
        )
        assert text == "Work prefix. Continued after toggle."

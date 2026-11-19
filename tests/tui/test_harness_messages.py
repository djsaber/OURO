from __future__ import annotations

from datetime import UTC, datetime
from threading import Event

import pytest
from rich.console import Console

from ouro.core.attachment_input import separate_attachments
from ouro.core.errors import ConfigError
from ouro.core.events import EventRecord
from ouro.core.input_content import InputAttachment
from ouro.core.messages import MessageRecord
from ouro.tui.app import OuroTuiApp
from ouro.tui.composer.input import Composer
from ouro.tui.conversation.history import ConversationHistory
from ouro.tui.conversation.history_rendering import _MessageContent
from ouro.tui.conversation.transcript import project_transcript_entries
from ouro.tui.conversation.view import ConversationView
from ouro.tui.menus.choice import ChoiceScreen
from ouro.tui.preferences import TuiPreferencesStore
from ouro.tui.state import TuiSessionProjection
from ouro.tui.theme import DEFAULT_TUI_THEME
from tests.tui.app.support import _application, _persist_direct_child_session
from tests.tui.helpers import choose_menu, run_ready_app, wait_for_screen, wait_until
from tests.tui.test_background_navigation import NavigationProvider, submit
from tests.tui.test_tasks import streaming_application


def test_harness_frame_preserves_user_message_layout_and_targets():
    console = Console(force_terminal=True, color_system="truecolor", width=36)
    options = dict(
        text="Long wrapped message with 中文 and **literal markup**\nSecond line",
        theme=DEFAULT_TUI_THEME,
        created_at=datetime(2026, 10, 19, tzinfo=UTC),
    )
    user = console.render_lines(_MessageContent(**options))
    harness = console.render_lines(_MessageContent(**options, lowlight=True))
    assert len(user) == len(harness)
    for normal, internal in zip(user, harness, strict=True):
        for normal_segment, internal_segment in zip(normal[1:], internal[1:], strict=True):
            assert normal_segment.text == internal_segment.text
            assert internal_segment.style.meta == normal_segment.style.meta
        assert normal[0].text == internal[0].text == "│"
        assert normal[0].style.meta == internal[0].style.meta


@pytest.mark.parametrize("prompt", ["", "Read this file"])
def test_revealing_attachment_notices_preserves_file_labels_and_user_targets(prompt):
    messages = tuple(
        separate_attachments(
            MessageRecord.text(session_id="s", role="user", text=prompt),
            (InputAttachment(resource_id="resource", name="notes.txt"),),
        )
    )
    results = []
    for enabled in (False, True):
        entries = project_transcript_entries(
            messages,
            (),
            (),
            mutable_tool_call_keys=frozenset(),
            show_harness_messages=enabled,
        )
        assert len([entry for entry in entries if entry.kind == "user"]) == 1
        assert all("notes.txt" in entry.text for entry in entries)
        results.append(entries)
    assert "User provided the following files" not in results[0][0].text
    assert "User provided the following files" in results[1][0].text


@pytest.mark.parametrize("enabled", [False, True])
async def test_live_inputs_and_loaded_history_share_harness_projection(tmp_path, enabled):
    application = _application(tmp_path)
    application.runtime.sessions.create(session_id="ses_inputs")
    app = OuroTuiApp(home=tmp_path, application=application)
    messages = tuple(
        MessageRecord.text(
            session_id="ses_inputs",
            role=role,
            text=text,
            sequence=index,
        )
        for index, (role, text) in enumerate(
            (
                ("harness", "Environment instructions"),
                ("harness", "Goal instructions"),
                ("harness", "Internal runtime notice"),
                ("assistant", "Previously supplied answer"),
                ("user", "Actual input"),
            ),
            start=1,
        )
    )
    messages += (
        MessageRecord.text(
            session_id="ses_inputs",
            role="harness",
            text="Subagent completed the task",
            semantic_tag="subagent-result",
            semantic_attributes={"delegation_id": "dlg_child", "status": "completed"},
            sequence=len(messages) + 1,
        ).model_copy(
            update={
                "metadata": {
                    "session_input_source": "host",
                    "host_source_id": "ouro.subagents/results/delivery_child",
                }
            }
        ),
    )
    async with run_ready_app(app):
        await app._set_harness_messages(enabled)
        app._projection = TuiSessionProjection("ses_inputs")
        events = tuple(
            EventRecord(
                session_id=message.session_id,
                event_type="context_message_added"
                if message.role == "assistant"
                else "plugin_message_added",
                payload={"message_id": message.message_id},
                sequence=index,
            )
            for index, message in enumerate(messages, start=1)
        )
        await app._apply_runtime_event_batch(
            events, input_messages={message.message_id: message for message in messages}
        )
        history = app.query_one(ConversationHistory)
        expected = project_transcript_entries(
            messages,
            (),
            (),
            mutable_tool_call_keys=frozenset(),
            show_harness_messages=enabled,
        )
        assert history.entries == expected
        assert [entry.text for entry in expected] == [
            message.text_content for message in messages if enabled or message.role != "harness"
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
                    session_id=session_id,
                    role="harness",
                    text=f"Internal {session_id}",
                ),
                MessageRecord.text(session_id=session_id, role="user", text=f"User {session_id}"),
            ]
        )
    app = OuroTuiApp(home=tmp_path, application=application)
    async with run_ready_app(app, size=(100, 34)) as pilot:
        await app._open_session(root_id)
        await app._show_observed_session(target_id)
        history = app.query_one(ConversationHistory)
        assert not any((entry.kind == "harness") for entry in history.entries)
        controlled_id = app._controller.session_id
        await pilot.press("ctrl+p")
        if session_kind == "subagent":
            await pilot.pause()
            assert {choice.label for choice in app.screen._choices} == {
                "Theme",
                "Display",
                "Remember drafts",
                "Session grouping",
                "New session",
                "Open session",
                "Keyboard help",
            }
        await choose_menu(pilot, "Display", title="DISPLAY")
        menu = app.screen
        assert menu._choices[1].enabled_state is False
        await choose_menu(pilot, "Harness messages")
        await wait_until(
            lambda: any((entry.kind == "harness") for entry in history.entries),
            pilot,
            description="harness messages displayed",
        )
        assert app.screen is menu
        assert app._controller.session_id == controlled_id
        assert app._visible_session_id() == target_id
        assert app.query_one(Composer).disabled is (session_kind == "subagent")
        assert TuiPreferencesStore(tmp_path).load().show_harness_messages
        await pilot.press("escape", "escape")
        for session_id in (root_id, child_id, task.session_id, target_id):
            await app._show_observed_session(session_id)
            assert f"Internal {session_id}" in [entry.text for entry in history.entries]
        assert await app._set_harness_messages(False)
        for session_id in (root_id, child_id, task.session_id, target_id):
            await app._show_observed_session(session_id)
            assert not any((entry.kind == "harness") for entry in history.entries)
        assert await app._set_harness_messages(True)

    restarted = OuroTuiApp(home=tmp_path, application=_application(tmp_path))
    async with run_ready_app(restarted):
        await restarted._open_session(target_id)
        assert any(
            (entry.kind == "harness") for entry in restarted.query_one(ConversationHistory).entries
        )


@pytest.mark.parametrize("all_harness", [False, True])
async def test_toggle_preserves_loaded_history_window_and_projects_earlier_pages(
    tmp_path, all_harness
):
    application = _application(tmp_path)
    session = application.runtime.sessions.create(title="Long history").session
    application.runtime.store.append_messages(
        [
            MessageRecord.text(
                session_id=session.session_id,
                role="harness" if all_harness or index % 2 == 0 else "user",
                text=f"Message {index}",
            )
            for index in range(405)
        ]
    )
    app = OuroTuiApp(home=tmp_path, application=application)
    async with run_ready_app(app):
        await app._open_session(session.session_id)
        assert await app._set_harness_messages(True)
        assert await app._prepend_history_window(app._view_revision)
        history = app.query_one(ConversationHistory)
        texts = [entry.text for entry in history.entries]
        assert len(texts) == 400
        assert await app._set_harness_messages(False)
        assert [entry.text for entry in history.entries] == [
            text for text in texts if not all_harness and int(text.split()[-1]) % 2
        ]
        await app._new_session()
        assert await app._set_harness_messages(True)
        await app._open_session(session.session_id)
        assert [entry.text for entry in history.entries] == texts
        assert await app._prepend_history_window(app._view_revision)
        assert [entry.text for entry in history.entries] == [
            f"Message {index}" for index in range(405)
        ]


async def test_failed_preference_save_keeps_harness_visibility_unchanged(tmp_path, monkeypatch):
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))
    async with run_ready_app(app):

        def fail_save(_preferences):
            raise ConfigError("Cannot save preferences")

        monkeypatch.setattr(app._preferences_store, "save", fail_save)
        assert not await app._set_harness_messages(True)
        assert not app._preferences.show_harness_messages
        assert not app.query_one(ConversationView).show_harness_messages


async def test_closing_menu_does_not_interrupt_harness_preference_commit(tmp_path, monkeypatch):
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))
    entered, release = Event(), Event()
    save = app._preferences_store.save

    def held_save(preferences):
        if preferences.show_harness_messages:
            entered.set()
            assert release.wait(10)
        save(preferences)

    async with run_ready_app(app) as pilot:
        await pilot.press("ctrl+p")
        await wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        await choose_menu(pilot, "Display", title="DISPLAY")
        monkeypatch.setattr(app._preferences_store, "save", held_save)
        try:
            await choose_menu(pilot, "Harness messages")
            await wait_until(entered.is_set, pilot, description="preference write started")
            await pilot.press("escape", "escape")
            assert not isinstance(app.screen, ChoiceScreen)
        finally:
            release.set()
        await wait_until(
            lambda: app.query_one(ConversationView).show_harness_messages,
            pilot,
            description="preference applied after closing menu",
        )
        assert TuiPreferencesStore(tmp_path).load().show_harness_messages


@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_toggle_during_stream_preserves_tail_draft_and_event_handoff(tmp_path, backend):
    provider = NavigationProvider()
    application = streaming_application(tmp_path, provider, backend=backend)
    session = application.runtime.sessions.create(title="Streaming").session
    application.runtime.store.append_message(
        MessageRecord.text(
            session_id=session.session_id,
            role="harness",
            text="Internal context",
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
                assert await app._set_harness_messages(enabled)
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
                description="stream after harness setting change",
            )
        finally:
            provider.finish("Work")
            await worker.wait()
        history = app.query_one(ConversationHistory)
        await wait_until(
            lambda: (
                app._projection.activity.status == "completed"
                and bool(history.entries)
                and history.entries[-1].kind == "run"
            ),
            pilot,
            description="stream completion committed to history",
        )
        text = "".join(entry.text for entry in history.entries if entry.kind == "assistant")
        assert text == "Work prefix. Continued after toggle."

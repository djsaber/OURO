"""TUI application scenarios: queue."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from rich.console import Console
from rich.text import Text
from textual.widgets import Input, Static

from ouro.application.configuration.facade import ConfigurationFacade
from ouro.application.inputs.queue import QueuedUserInputView
from ouro.tui.app import OuroTuiApp
from ouro.tui.composer.input import Composer
from ouro.tui.composer.popup import CommandPopup
from ouro.tui.composer.queue import QueuedInputPanel
from ouro.tui.conversation.history import ConversationHistory
from ouro.tui.conversation.view import ConversationView
from ouro.tui.interactions.bar import InteractionBar
from ouro.tui.menus.choice import ChoiceScreen
from ouro.tui.subagent_navigation import SubagentNavigationPanel
from ouro.tui.theme import DEFAULT_TUI_THEME
from tests.tui.app.providers import BlockingStreamProvider, QueuedSubagentApprovalProvider
from tests.tui.app.support import (
    _application,
    _approval_application,
    _blocking_application,
    _queued_subagent_approval_application,
    _wait_for_tool_approval,
)
from tests.tui.helpers import (
    command_popup_text,
    release_on_exit,
    run_ready_app,
    wait_for_events,
    wait_for_menu_action,
)
from tests.tui.helpers import wait_for_screen as _wait_for_screen
from tests.tui.helpers import wait_for_worker_group as _wait_for_worker_group
from tests.tui.helpers import wait_until as _wait_until


@pytest.mark.asyncio
async def test_queued_input_rows_use_single_line_tokenized_previews(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    long_prompt = "first line\nsecond line " + ("queued detail " * 12) + "hidden suffix"
    inputs = (
        QueuedUserInputView(
            queue_id="queue_guide",
            prompt=long_prompt,
            mode="steer",
            queued_at=datetime.now(UTC),
            waiting_on_run_id="active_run",
        ),
        QueuedUserInputView(
            queue_id="queue_follow_up",
            prompt="follow next",
            mode="follow_up",
            queued_at=datetime.now(UTC),
            waiting_on_run_id="active_run",
        ),
    )

    async with run_ready_app(app, size=(48, 24)) as pilot:
        await pilot.pause()
        panel = app.query_one(QueuedInputPanel)
        panel.set_inputs(inputs)
        await pilot.pause()

        content_widget = panel.query_one("#queued-input-content", Static)
        source = content_widget.content
        assert isinstance(source, Text)
        assert source.plain.count("\n") == len(inputs) - 1
        assert source.plain.startswith(
            f"1. Guide{DEFAULT_TUI_THEME.inline_separator}first line ↵ second line"
        )
        assert f"2. Follow-up{DEFAULT_TUI_THEME.inline_separator}follow next" in source.plain

        label_style = source.get_style_at_offset(Console(), source.plain.index("Guide"))
        assert not label_style.bold

        assert content_widget.styles.text_wrap == "nowrap"
        assert content_widget.styles.text_overflow == "ellipsis"
        assert content_widget.content_size.height == len(inputs)
        first_visible_line = content_widget.render_line(0).text.rstrip()
        assert first_visible_line.startswith(f"1. Guide{DEFAULT_TUI_THEME.inline_separator}")
        assert first_visible_line.endswith("…")
        assert "hidden suffix" not in first_visible_line


@pytest.mark.asyncio
async def test_follow_up_stays_in_queue_until_consumed_at_the_next_run(
    tmp_path: Path,
) -> None:
    provider = BlockingStreamProvider()
    application = _blocking_application(tmp_path, provider)
    app = OuroTuiApp(application=application)

    async with run_ready_app(app, size=(100, 36)) as pilot:
        await pilot.pause()
        await pilot.click("#composer")
        try:
            await pilot.press(*"first", "enter")
            await _wait_until(
                lambda: (
                    provider.first_chunk_sent.is_set()
                    and app._projection is not None
                    and app._projection.activity.status == "running"
                ),
                pilot,
                description="active run",
            )

            app.query_one(Composer).load_text("follow next")
            await pilot.press("enter")
            await _wait_until(
                lambda: app.query_one(QueuedInputPanel).display,
                pilot,
                description="queued follow-up",
            )

            queue_panel = app.query_one(QueuedInputPanel)
            conversation = app.query_one(ConversationView)
            assert queue_panel.display is True
            queue_text = queue_panel.query_one("#queued-input-content").render().plain
            assert "QUEUED INPUTS" not in queue_text
            assert "Follow-up" in queue_text
            assert f"Follow-up{DEFAULT_TUI_THEME.inline_separator}follow next" in queue_text
            assert "follow next" in queue_text
            history = conversation.query_one(ConversationHistory)
            assert history.user_turns == ("first",)

        finally:
            provider.release.set()
        await _wait_until(
            lambda: (
                sum(entry.kind == "assistant" for entry in history.entries) == 2
                and app._projection is not None
                and app._projection.activity.status == "completed"
                and not queue_panel.display
            ),
            pilot,
            description="completed initial run and queued follow-up",
        )

        assert queue_panel.display is False
        assert history.user_turns == (
            "first",
            "follow next",
        )
        user_indexes = [
            index for index, entry in enumerate(history.entries) if entry.kind == "user"
        ]
        assistant_indexes = [
            index for index, entry in enumerate(history.entries) if entry.kind == "assistant"
        ]
        assert len(user_indexes) == len(assistant_indexes) == 2
        assert user_indexes[0] < assistant_indexes[0] < user_indexes[1] < assistant_indexes[1]

        snapshot = application.runtime.store.load_runtime_session(app._controller.session_id or "")
        queued_message = next(
            message for message in snapshot.messages if message.text_content == "follow next"
        )
        assert queued_message.metadata["queued_input_mode"] == "follow_up"
        assert len(snapshot.runs) == 2


@pytest.mark.asyncio
async def test_unqueue_command_selects_message_and_returns_it_to_composer(
    tmp_path: Path,
) -> None:
    provider = BlockingStreamProvider()
    application = _blocking_application(tmp_path, provider)
    app = OuroTuiApp(application=application)

    async with run_ready_app(app, size=(100, 36)) as pilot:
        await pilot.pause()
        await pilot.click("#composer")
        try:
            await pilot.press(*"first", "enter")
            await _wait_until(
                lambda: (
                    provider.first_chunk_sent.is_set()
                    and app._projection is not None
                    and app._projection.activity.status == "running"
                ),
                pilot,
                description="active run",
            )

            # This test exercises /unqueue. Establish its accepted-input precondition
            # through the Runtime; composer delivery is covered by the follow-up test.
            assert app._projection is not None
            session_id = app._projection.session_id
            for prompt in ("keep queued", "return to composer"):
                await application.runtime.run(prompt, session_id=session_id, queue_mode="follow_up")
            composer = app.query_one(Composer)
            await _wait_until(
                lambda: all(
                    value
                    in app.query_one(QueuedInputPanel)
                    .query_one("#queued-input-content", Static)
                    .render()
                    .plain
                    for value in ("keep queued", "return to composer")
                ),
                pilot,
                description="queued inputs",
            )

            await pilot.press("slash", *"unqueue", "enter")
            await pilot.pause()
            popup = app.query_one(CommandPopup)
            choices = command_popup_text(popup).plain
            assert popup.argument_command is not None
            assert popup.argument_command.name == "/unqueue"
            assert "1. keep queued" in choices
            assert "2. return to composer" in choices

            await pilot.press("down", "enter")
            await _wait_until(
                lambda: composer.text == "return to composer",
                pilot,
                description="restored queued input",
            )

            assert composer.text == "return to composer"
            queue_panel = app.query_one(QueuedInputPanel)
            queued_text = queue_panel.query_one("#queued-input-content", Static).render().plain
            assert queue_panel.display is True
            assert "keep queued" in queued_text
            assert "return to composer" not in queued_text

        finally:
            provider.release.set()
        await _wait_until(
            lambda: app._projection is not None and app._projection.activity.status == "completed",
            pilot,
            description="run completion after unqueue",
        )

        snapshot = application.runtime.store.load_session(app._controller.session_id or "")
        assert "return to composer" not in [message.text_content for message in snapshot.messages]
        assert any(event.event_type == "queued_input_cancelled" for event in snapshot.events)


@pytest.mark.asyncio
async def test_queued_input_does_not_hide_or_block_subagent_approval(
    tmp_path: Path, monkeypatch
) -> None:
    provider = QueuedSubagentApprovalProvider()
    application = _queued_subagent_approval_application(tmp_path, provider)
    app = OuroTuiApp(application=application)
    checkpoint_waiting = threading.Event()
    release_checkpoint = threading.Event()
    save_batch = application.runtime.run_lifecycle.save_batch_state

    def delayed_checkpoint(**kwargs):
        if kwargs["session"].session_kind == "subagent" and kwargs["status"] == "waiting_approval":
            checkpoint_waiting.set()
            release_checkpoint.wait()
        return save_batch(**kwargs)

    monkeypatch.setattr(application.runtime.run_lifecycle, "save_batch_state", delayed_checkpoint)

    async with (
        run_ready_app(app, size=(100, 38)) as pilot,
        release_on_exit(provider.release_child, release_checkpoint),
    ):
        await pilot.pause()
        await pilot.click("#composer")
        await pilot.press(*"start child", "enter")
        await wait_for_events(
            provider.child_started, pilot=pilot, description="owned child execution"
        )
        await pilot.pause()

        try:
            app.query_one(Composer).load_text("follow after child")
            await pilot.press("enter")
            await _wait_until(
                lambda: app.query_one(QueuedInputPanel).display,
                pilot,
                description="queued follow-up",
            )
            assert app.query_one(QueuedInputPanel).display is True
        finally:
            provider.release_child.set()
        try:
            await _wait_until(
                checkpoint_waiting.is_set, pilot, description="child approval checkpoint"
            )
            # Preflight publishes approval_requested before its batch checkpoint.
            # Consume that notification while the canonical interaction is absent.
            await application.runtime.drain_events()
            await app._flush_pending_runtime_events()
            async with app._runtime_event_apply_lock:
                assert app._interaction is None
        finally:
            release_checkpoint.set()
        await _wait_until(
            lambda: app._interaction is not None and app._interaction.is_subagent,
            pilot,
            description="subagent approval",
        )

        interaction = app._interaction
        assert interaction is not None
        assert interaction.kind == "approval"
        assert interaction.is_subagent is True
        assert interaction.tool_name == "Danger"
        approval_panel = await _wait_for_tool_approval(
            app, pilot, tool_call_id="child_approval_call"
        )
        assert approval_panel.display is True
        subagent_panel = app.query_one(SubagentNavigationPanel)
        queue_panel = app.query_one(QueuedInputPanel)
        assert subagent_panel.display is True
        await pilot.pause()
        assert subagent_panel.region.bottom == approval_panel.region.y
        assert approval_panel.region.bottom == app.query_one(Composer).region.y
        assert app.query_one(Composer).region.bottom == queue_panel.region.y
        assert (
            "Subagent General"
            in approval_panel.query_one("#interaction-summary", Static).render().plain
        )

        await pilot.press("enter")
        await wait_for_events(
            provider.queued_input_consumed, pilot=pilot, description="queued input consumed"
        )
        history = app.query_one(ConversationHistory)
        await _wait_until(
            lambda: (
                provider.queued_input_consumed.is_set()
                and app._projection is not None
                and app._projection.activity.status == "completed"
                and app._interaction is None
                and not app._pending_runtime_events
                and not app._runtime_events_ready_posted
                and not app._runtime_events_frame_scheduled
                and any(
                    entry.kind == "assistant" and "queued input consumed" in entry.text
                    for entry in history.entries
                )
            ),
            pilot,
            description="completed queued follow-up with settled TUI events",
        )

        await _wait_for_worker_group(app, pilot, group="run")
        assert app._interaction is None
        assert app.query_one(InteractionBar).display is False
        assert app.query_one(QueuedInputPanel).display is False
        rendered = "\n".join(entry.text for entry in history.entries if entry.kind == "assistant")
        assert "parent resumed" in rendered
        assert "queued input consumed" in rendered


@pytest.mark.asyncio
async def test_guide_shortcuts_steer_the_active_run_at_the_next_model_turn(
    tmp_path: Path,
) -> None:
    provider = BlockingStreamProvider()
    application = _blocking_application(tmp_path, provider)
    app = OuroTuiApp(application=application)

    async with run_ready_app(app, size=(100, 36)) as pilot:
        await pilot.pause()
        await pilot.click("#composer")
        try:
            await pilot.press(*"first", "enter")
            await _wait_until(
                lambda: (
                    provider.first_chunk_sent.is_set()
                    and app._projection is not None
                    and app._projection.activity.status == "running"
                ),
                pilot,
                description="active run",
            )

            queue_panel = app.query_one(QueuedInputPanel)
            app.query_one(Composer).load_text("guide now")
            await pilot.press("ctrl+g")
            await _wait_until(
                lambda: queue_panel.display,
                pilot,
                description="queued guidance",
            )

            queue_text = queue_panel.query_one("#queued-input-content").render().plain
            assert queue_panel.display is True
            assert "Guide" in queue_text
            assert f"Guide{DEFAULT_TUI_THEME.inline_separator}guide now" in queue_text
            assert "guide now" in queue_text
            assert "guide now" not in app.query_one(ConversationHistory).user_turns
        finally:
            provider.release.set()
        await _wait_until(
            lambda: app._projection is not None and app._projection.activity.status == "completed",
            pilot,
            description="guided run completion",
        )

        assert queue_panel.display is False
        snapshot = application.runtime.store.load_runtime_session(app._controller.session_id or "")
        guided_message = next(
            message for message in snapshot.messages if message.text_content == "guide now"
        )
        assert guided_message.metadata["queued_input_mode"] == "steer"
        assert len(snapshot.runs) == 1


@pytest.mark.asyncio
async def test_resumed_session_restores_its_durable_user_input_queue(
    tmp_path: Path,
) -> None:
    application = _approval_application(tmp_path, tmp_path / "pending.txt")
    await application.start()
    waiting = await application.runtime.run("Start approval")
    await application.runtime.run(
        "Continue after approval",
        session_id=waiting.session.session_id,
    )
    app = OuroTuiApp(
        application=application,
        approval_mode="approval",
        resume_session_id=waiting.session.session_id,
    )

    async with run_ready_app(app, size=(100, 36)) as pilot:
        await _wait_until(
            lambda: app._interaction is not None and app.query_one(QueuedInputPanel).display,
            pilot,
            description="restored input queue",
        )

        queue_panel = app.query_one(QueuedInputPanel)
        queue_text = queue_panel.query_one("#queued-input-content").render().plain
        assert queue_panel.display is True
        assert "Follow-up" in queue_text
        assert "Continue after approval" in queue_text
        assert "Continue after approval" not in app.query_one(ConversationHistory).user_turns
        assert app._interaction is not None
        assert app._interaction.kind == "approval"
        assert app.query_one(InteractionBar).display is True


@pytest.mark.asyncio
async def test_queued_input_delivery_uses_shared_settings_menu_and_persists_mode(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 34), notifications=True) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await pilot.pause()
        app.screen.query_one("#choice-filter", Input).value = "Queued messages"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="QUEUED MESSAGES")

        screen = app.screen
        assert [choice.value for choice in screen._choices] == [
            "one_by_one",
            "all_at_once",
        ]
        assert [choice.trailing for choice in screen._choices] == [
            "Process each message separately",
            "Process waiting messages together",
        ]
        assert screen._choices[0].marker == DEFAULT_TUI_THEME.selected_choice_marker
        assert screen._choices[1].marker == ""

        screen.query_one("#choice-filter", Input).value = "All at once"
        await pilot.pause()
        await pilot.press("enter")
        await wait_for_menu_action(pilot, screen)

        assert ConfigurationFacade(tmp_path).queued_input_config().consumption_mode == "all_at_once"
        assert app.screen is screen
        assert screen._choices[0].marker == ""
        assert screen._choices[1].marker == DEFAULT_TUI_THEME.selected_choice_marker

"""TUI application scenarios: history."""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from textual.widgets import Static

import ouro.tui.conversation.view as conversation_view
from ouro.core.compaction import CompactionRecord
from ouro.core.context import ContextMetrics
from ouro.core.context_budget import ContextBudget
from ouro.core.events import EventRecord
from ouro.core.messages import MessageRecord, ModelResponseRecord, TextContent
from ouro.core.state import RunRecord
from ouro.tui.app import OuroTuiApp
from ouro.tui.composer.input import Composer
from ouro.tui.conversation.history import ConversationHistory
from ouro.tui.conversation.history_models import CompactionPresentation
from ouro.tui.conversation.turn import AssistantTurn
from ouro.tui.conversation.view import ConversationView
from ouro.tui.menus.choice import ChoiceScreen
from ouro.tui.preferences import TuiPreferences, TuiPreferencesStore
from ouro.tui.state import TuiSessionProjection, project_run_presentation
from ouro.tui.theme import DEFAULT_TUI_THEME
from ouro.tui.widgets.activity import ActivityIndicator
from ouro.tui.widgets.brand import BrandHeader
from ouro.tui.widgets.status import StatusLine
from tests.tui.app.support import _application, _wait_for_active_session
from tests.tui.helpers import run_ready_app, wait_for_history_render
from tests.tui.helpers import wait_for_screen as _wait_for_screen
from tests.tui.helpers import wait_for_worker_group as _wait_for_worker_group
from tests.tui.helpers import wait_until as _wait_until


@pytest.mark.asyncio
async def test_assistant_history_restores_reasoning_separately(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    reasoning_text = (
        'Prior reasoning with [data-theme=" remains plain text across wrapping. '
        + "wrapped reasoning " * 8
    )
    message = MessageRecord(
        session_id="ses_history",
        role="assistant",
        content=[TextContent(text="Final answer")],
        reasoning_content=reasoning_text,
    )

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        await conversation.set_reasoning_expanded(True)
        await conversation.load_messages((message,))
        await wait_for_history_render(pilot)

        history = app.query_one(ConversationHistory)
        assert len(history.entries) == 2
        thought, answer = history.entries
        assert thought.kind == "assistant"
        assert thought.reasoning == reasoning_text
        assert answer.kind == "assistant"
        assert answer.text == "Final answer"
        rendered_lines = history.rendered_text.splitlines()
        thought_line = next(
            index for index, line in enumerate(rendered_lines) if line.strip() == "- Thought"
        )
        answer_line = next(
            index for index, line in enumerate(rendered_lines) if line.strip() == "Final answer"
        )
        assert rendered_lines[thought_line + 1] == ""
        reasoning_lines = [
            line for line in rendered_lines[thought_line + 1 : answer_line] if line.strip()
        ]
        assert len(reasoning_lines) > 1
        thought_indent = len(rendered_lines[thought_line]) - len(
            rendered_lines[thought_line].lstrip()
        )
        assert all(
            line.startswith(" " * thought_indent + "\N{BOX DRAWINGS LIGHT VERTICAL} ")
            for line in reasoning_lines
        )
        assert not app.query_one(AssistantTurn).display


@pytest.mark.asyncio
@pytest.mark.parametrize("collapse_traces", [True, False])
async def test_history_restores_each_model_call_timing_with_one_run_summary(
    tmp_path: Path,
    collapse_traces: bool,
) -> None:
    TuiPreferencesStore(tmp_path).save(TuiPreferences(collapse_traces=collapse_traces))
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))
    started_at = datetime(2026, 8, 11, tzinfo=UTC)
    run = RunRecord(
        session_id="ses_multi_model",
        run_id="run_multi_model",
        status="completed",
        started_at=started_at,
        ended_at=started_at + timedelta(seconds=3),
        model="provider-model",
    )
    messages = tuple(
        MessageRecord(
            session_id=run.session_id,
            run_id=run.run_id,
            role="assistant",
            content=[TextContent(text=f"Answer {index}")],
            reasoning_content=f"Thought {index}",
            model_response=ModelResponseRecord(
                provider_id="supplier",
                endpoint_id="chat-endpoint",
                model_id="provider-model",
                started_at=started_at + timedelta(seconds=index),
                reasoning_ended_at=started_at + timedelta(seconds=index, milliseconds=500),
                ended_at=started_at + timedelta(seconds=index, milliseconds=700),
            ),
        )
        for index in (0, 1)
    )

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        presentation = project_run_presentation(run, messages)
        await conversation.load_messages(messages, runs=(presentation,))

        history = conversation.query_one(ConversationHistory)
        await wait_for_history_render(pilot)
        thoughts = [
            entry for entry in history.entries if entry.kind == "assistant" and entry.reasoning
        ]
        summaries = [entry for entry in history.entries if entry.kind == "run"]
        assert [entry.reasoning_seconds for entry in thoughts] == [0.5, 0.5]
        assert len(summaries) == 1
        assert summaries[0].run == presentation
        assert presentation.model == "provider-model"
        assert presentation.duration_seconds == 3
        assert history.rendered_text.count("+ Thought • 500ms") == (1 if collapse_traces else 2)
        assert ("Answer 0" in history.rendered_text) is (not collapse_traces)
        assert "Answer 1" in history.rendered_text
        if collapse_traces:
            await history.toggle_trace(history.entries.index(summaries[0]))
        # Expanding the trace must restore the earlier call's original timing.
        assert history.rendered_text.count("+ Thought • 500ms") == 2
        assert "Answer 0" in history.rendered_text
        assert "provider-model" in history.rendered_text
        assert "chat-endpoint" not in history.rendered_text
        assert "3.0s" in history.rendered_text


@pytest.mark.asyncio
async def test_history_loads_failed_run_summary_with_failure_semantics(tmp_path: Path) -> None:
    started_at = datetime(2026, 8, 11, tzinfo=UTC)
    run = RunRecord(
        session_id="ses_failed_run",
        run_id="run_failed",
        status="failed",
        started_at=started_at,
        ended_at=started_at + timedelta(seconds=316.4),
        model="deepseek-v4-flash",
        error="provider returned an empty assistant message",
    )
    message = MessageRecord.text(
        session_id=run.session_id,
        run_id=run.run_id,
        role="assistant",
        text="Partial answer",
    )
    app = OuroTuiApp(application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        await conversation.load_messages(
            (message,),
            runs=(project_run_presentation(run, (message,)),),
        )
        await wait_for_history_render(pilot)

        history = conversation.query_one(ConversationHistory)
        summary = next(entry for entry in history.entries if entry.kind == "run")
        assert summary.run is not None
        assert summary.run.status == "failed"
        assert (
            f"{DEFAULT_TUI_THEME.brand_mark} deepseek-v4-flash"
            f"{DEFAULT_TUI_THEME.section_separator}5m 16.4s"
            f"{DEFAULT_TUI_THEME.section_separator}provider returned an empty assistant message"
        ) in history.rendered_text
        assert f"{DEFAULT_TUI_THEME.section_separator}failed" not in history.rendered_text


@pytest.mark.asyncio
async def test_reloaded_history_renders_identically_to_streamed_output(
    tmp_path: Path,
) -> None:
    answer = (
        "Here is the plan:\n\n- step one\n- step two\n\n## Details\n\n"
        "```python\nx = 1\n```\n\n| col | val |\n| --- | --- |\n| a | 1 |\n\n"
        "> quoted\n\nSetext Title\n============\n\nFinal paragraph.\n"
    )
    fragments = answer.split("\n\n")
    app = OuroTuiApp(application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 40)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        await conversation.add_user("Question")
        for index, fragment in enumerate(fragments):
            separator = "\n\n" if index < len(fragments) - 1 else ""
            await conversation.append_assistant(fragment + separator)
            await pilot.pause()
        await conversation.finish_assistant()
        await pilot.pause()
        streamed = conversation.query_one(ConversationHistory).rendered_text
        assert "step one" in streamed
        assert "Setext Title" in streamed
        assert "Final paragraph." in streamed

        messages = (
            MessageRecord(
                session_id="ses_consistency",
                role="user",
                content=[TextContent(text="Question")],
            ),
            MessageRecord(
                session_id="ses_consistency",
                role="assistant",
                content=[TextContent(text=answer)],
                run_id="run_consistency",
            ),
        )
        await conversation.load_messages(messages)
        await wait_for_history_render(pilot)
        reloaded = conversation.query_one(ConversationHistory).rendered_text

        assert reloaded == streamed


@pytest.mark.asyncio
async def test_history_batch_mount_preserves_dialogue_order(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    messages = tuple(
        MessageRecord(
            session_id="ses_history_order",
            role=role,
            content=[TextContent(text=text)],
        )
        for role, text in (
            ("user", "First question"),
            ("assistant", "First answer"),
            ("user", "Second question"),
            ("assistant", "Second answer"),
        )
    )

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        await conversation.load_messages(messages)
        await pilot.pause()

        history = conversation.query_one(ConversationHistory)
        transcript = [(entry.kind, entry.text) for entry in history.entries]
        assert transcript == [
            ("user", "First question"),
            ("assistant", "First answer"),
            ("user", "Second question"),
            ("assistant", "Second answer"),
        ]
        assert not conversation.query_one(AssistantTurn).display


@pytest.mark.asyncio
async def test_history_restores_compaction_boundaries_in_chronological_order(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    session_id = "ses_compacted_history"
    messages = (
        MessageRecord.text(
            session_id=session_id,
            role="user",
            text="First question",
        ).model_copy(update={"created_at": datetime(2026, 1, 1, 10, 0, tzinfo=UTC)}),
        MessageRecord.text(
            session_id=session_id,
            role="assistant",
            text="First answer",
        ).model_copy(update={"created_at": datetime(2026, 1, 1, 10, 2, tzinfo=UTC)}),
        MessageRecord.text(
            session_id=session_id,
            role="user",
            text="Second question",
        ).model_copy(update={"created_at": datetime(2026, 1, 1, 10, 4, tzinfo=UTC)}),
    )
    compactions = (
        CompactionRecord(
            session_id=session_id,
            strategy_id="test",
            trigger="auto",
            summary="first summary",
            covered_message_count=1,
            covered_end_message_id=messages[0].message_id,
            history_boundary_message_id=messages[0].message_id,
            estimated_tokens_before=800,
            estimated_tokens_after=300,
            duration_seconds=3_425.4,
            # The durable boundary, not wall-clock ordering, owns placement.
            created_at=datetime(2026, 1, 1, 11, 0, tzinfo=UTC),
        ),
        CompactionRecord(
            session_id=session_id,
            strategy_id="test",
            trigger="manual",
            summary="second summary",
            covered_message_count=2,
            covered_end_message_id=messages[1].message_id,
            history_boundary_message_id=messages[1].message_id,
            estimated_tokens_before=1_500,
            estimated_tokens_after=500,
            duration_seconds=5.4,
            created_at=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
        ),
    )

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        await conversation.load_messages(messages, compactions)
        await wait_for_history_render(pilot)

        history = conversation.query_one(ConversationHistory)
        assert [(entry.kind, entry.compaction) for entry in history.entries] == [
            ("user", None),
            ("compaction", CompactionPresentation(1, 500, 3_425.4, compactions[0].compaction_id)),
            ("assistant", None),
            ("compaction", CompactionPresentation(2, 1_000, 5.4, compactions[1].compaction_id)),
            ("user", None),
        ]
        assert (
            f"{DEFAULT_TUI_THEME.brand_mark} Context compacted"
            f"{DEFAULT_TUI_THEME.section_separator}1 message"
            f"{DEFAULT_TUI_THEME.section_separator}500 tokens"
            f"{DEFAULT_TUI_THEME.section_separator}57m 05.4s"
        ) in history.rendered_text

        invalid = compactions[0].model_copy(update={"history_boundary_message_id": "msg_missing"})
        with pytest.raises(ValueError, match="references missing history boundary"):
            await conversation.load_messages(messages, (invalid,))


@pytest.mark.asyncio
async def test_compaction_completion_updates_context_and_transcript_without_next_request(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    metrics = ContextMetrics(
        estimated_input_tokens=250,
        estimated_fixed_tokens=100,
        budget=ContextBudget(context_window=1000, max_output_tokens=100),
    )

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        session_id = "ses_live_compaction"
        app._projection = TuiSessionProjection(session_id)
        completed = EventRecord(
            session_id=session_id,
            run_id="run_compaction",
            event_type="compaction_completed",
            sequence=1,
            payload={
                "compaction_id": "cmp_live",
                "covered_message_count": 4,
                "tokens_before": 800,
                "tokens_after": 250,
                "duration_seconds": 65.4,
                "context_metrics_after": metrics.model_dump(mode="json"),
            },
        )
        await app._apply_runtime_events((completed,))
        await pilot.pause()

        history = app.query_one(ConversationHistory)
        assert history.entries[-1].kind == "compaction"
        assert history.entries[-1].compaction == CompactionPresentation(
            compaction_id="cmp_live",
            covered_message_count=4,
            tokens_reduced=550,
            duration_seconds=65.4,
        )
        assert (
            f"{DEFAULT_TUI_THEME.brand_mark} Context compacted"
            f"{DEFAULT_TUI_THEME.section_separator}4 messages"
            f"{DEFAULT_TUI_THEME.section_separator}550 tokens"
            f"{DEFAULT_TUI_THEME.section_separator}1m 05.4s"
        ) in history.rendered_text
        assert app.query_one("#context-detail", Static).render().plain.startswith("250 ")


@pytest.mark.asyncio
async def test_persisted_history_uses_a_bounded_widget_tree(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    messages = tuple(
        MessageRecord(
            session_id="ses_large_history",
            role="user" if index % 2 == 0 else "assistant",
            content=[TextContent(text=f"Message {index}")],
            reasoning_content=(f"Reasoning {index}" if index % 2 else ""),
        )
        for index in range(400)
    )

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        initial_widgets = set(conversation.query("*"))
        await conversation.load_messages(messages)
        await pilot.pause()

        history = conversation.query_one(ConversationHistory)
        # Assistant messages with reasoning split into a reasoning entry plus
        # one entry per markdown block: 200 user + 200 reasoning + 200 text.
        assert len(history.entries) == 600
        assert set(conversation.query("*")) == initial_widgets


@pytest.mark.asyncio
async def test_history_projection_does_not_block_the_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    message = MessageRecord(
        session_id="ses_projection",
        role="assistant",
        content=[TextContent(text="Projected in a worker" * 4_096)],
    )

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        entered = threading.Event()
        release = threading.Event()
        projection_threads: list[int] = []
        project_transcript = conversation_view.project_transcript_entries

        def blocking_projection(*args, **kwargs):
            assert threading.get_ident() != event_loop_thread
            projection_threads.append(threading.get_ident())
            entered.set()
            release.wait()
            return project_transcript(*args, **kwargs)

        monkeypatch.setattr(
            conversation_view,
            "project_transcript_entries",
            blocking_projection,
        )
        event_loop_thread = threading.get_ident()
        loading = asyncio.create_task(app.query_one(ConversationView).load_messages((message,)))

        try:
            projection_entered = await asyncio.to_thread(entered.wait, 30)
            event_loop_remained_responsive = not loading.done()
        finally:
            release.set()
            await asyncio.gather(loading, return_exceptions=True)
        await loading

        assert projection_entered is True
        assert event_loop_remained_responsive is True
        assert projection_threads != [event_loop_thread]


@pytest.mark.asyncio
async def test_completed_live_turns_keep_the_widget_tree_bounded(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        live_turns = set()
        for index in range(100):
            await conversation.add_user(f"Question {index}")
            await conversation.append_assistant(f"Answer {index}")
            live_turns.add(conversation.query_one(AssistantTurn))
            await conversation.finish_assistant()
        await pilot.pause()

        history = conversation.query_one(ConversationHistory)
        assert len(history.entries) == 200
        assert len(live_turns) == 1
        turn = live_turns.pop()
        assert not turn.display
        assert not turn.source
        assert not turn.reasoning.text
        assert turn.reasoning._timer is None


@pytest.mark.asyncio
async def test_run_result_replays_events_when_live_sink_is_delayed(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path, "Recovered response"))

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        app._controller._publish_event = lambda _event: None
        await pilot.click("#composer")
        await pilot.press("h", "e", "l", "l", "o", "enter")
        await _wait_until(
            lambda: app._projection is not None and app._projection.activity.status == "completed",
            pilot,
            description="replayed run result",
        )

        assert app._projection is not None
        assert app._projection.assistant_text == "Recovered response"
        assert app._projection.activity.status == "completed"


@pytest.mark.asyncio
async def test_session_picker_closes_modal_before_rendering_selected_history(
    tmp_path: Path,
) -> None:
    application = _application(tmp_path)
    session = application.runtime.sessions.create(title="Readable session").session
    application.runtime.store.append_message(
        MessageRecord.text(
            session_id=session.session_id,
            role="user",
            text="Persisted user message",
            sequence=1,
        )
    )
    app = OuroTuiApp(application=application)

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+o")
        await _wait_for_screen(app, ChoiceScreen, title="SESSION • USER")
        session_menu = app.screen

        await asyncio.wait_for(pilot.press("enter"), timeout=2)
        await pilot.pause()

        assert session_menu not in app.screen_stack
        assert app._controller.session_id == session.session_id
        assert app.query_one(ConversationHistory).user_turns == ("Persisted user message",)


@pytest.mark.asyncio
async def test_session_picker_restores_history_after_returning_to_welcome(
    tmp_path: Path,
) -> None:
    application = _application(tmp_path)
    workspace = tmp_path / "reusable-workspace"
    workspace.mkdir()
    session = application.runtime.sessions.create(
        title="Reusable session",
        workspace=str(workspace),
    ).session
    application.runtime.store.append_message(
        MessageRecord.text(
            session_id=session.session_id,
            role="user",
            text="Persisted user message",
            sequence=1,
        )
    )
    app = OuroTuiApp(application=application)

    async with run_ready_app(app, size=(len(str(workspace)) + 40, 32)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+o")
        await _wait_for_screen(app, ChoiceScreen, title="SESSION • USER")
        await pilot.press("enter")
        await _wait_for_active_session(app, pilot, session.session_id)
        await pilot.pause()
        assert app.query_one(ConversationHistory).user_turns == ("Persisted user message",)
        assert app.query_one(BrandHeader).render_line(0).text.rstrip().endswith(str(workspace))

        await pilot.press("ctrl+n")
        await _wait_until(
            lambda: app.query_one(ConversationView).showing_welcome,
            pilot,
            description="new-session welcome page",
        )
        assert app._controller.draft_workspace == str(workspace.resolve())
        assert app._active_workspace == str(workspace.resolve())
        await pilot.press("ctrl+o")
        await _wait_for_screen(app, ChoiceScreen, title="SESSION • USER")
        await pilot.press("enter")
        await _wait_for_active_session(app, pilot, session.session_id)
        await wait_for_history_render(pilot)

        history = app.query_one(ConversationHistory)
        assert history.user_turns == ("Persisted user message",)
        assert "Persisted user message" in history.rendered_text
        assert app.query_one(BrandHeader).render_line(0).text.rstrip().endswith(str(workspace))


@pytest.mark.asyncio
async def test_session_load_projects_loading_activity_until_history_is_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _application(tmp_path)
    session = application.runtime.sessions.create(title="Long session").session
    app = OuroTuiApp(application=application)

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        load_started = asyncio.Event()
        release_load = asyncio.Event()
        load_session = app._controller.prepare_session_selection

        async def slow_load(session_id: str):
            load_started.set()
            await release_load.wait()
            return await load_session(session_id)

        monkeypatch.setattr(app._controller, "prepare_session_selection", slow_load)
        loading = asyncio.create_task(app._open_session(session.session_id))
        await asyncio.wait_for(load_started.wait(), timeout=1)
        await pilot.pause()

        indicator = app.query_one(StatusLine).query_one(ActivityIndicator)
        assert "Loading session" in indicator.render().plain

        release_load.set()
        await asyncio.wait_for(loading, timeout=2)
        await pilot.pause()
        assert "Ready" in indicator.render().plain


@pytest.mark.asyncio
async def test_workspace_history_records_startup_directory_and_keeps_twenty(
    tmp_path: Path,
) -> None:
    preferences = TuiPreferencesStore(tmp_path)
    previous = tuple(f"C:/workspace/{index}" for index in range(20))
    preferences.save(TuiPreferences(recent_workspaces=previous))
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        stored = preferences.load().recent_workspaces
        assert stored == (str(tmp_path.resolve()), *previous[:19])


@pytest.mark.asyncio
async def test_workspace_clear_argument_forgets_saved_history_without_changing_workspace(
    tmp_path: Path,
) -> None:
    historical = tmp_path / "historical"
    historical.mkdir()
    preferences = TuiPreferencesStore(tmp_path)
    preferences.save(TuiPreferences(recent_workspaces=(str(historical.resolve()),)))
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 32), notifications=True) as pilot:
        await pilot.pause()
        active_workspace = app._active_workspace
        composer = app.query_one(Composer)
        composer.load_text("/workspace clear")
        composer.move_cursor((0, len(composer.document.lines[0])))
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_worker_group(app, pilot, group="composer-command", timeout=15)

        assert preferences.load().recent_workspaces == ()
        assert app._active_workspace == active_workspace
        assert "Recent workspaces cleared" in {
            toast.render().plain
            for toast in app.screen.query_one("#textual-toastrack").query("Toast")
        }

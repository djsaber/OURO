import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Event

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Input, Static

from ouro.application.contracts.execution import RuntimeToolCallActivity
from ouro.core.messages import MessageRecord, TextContent
from ouro.core.provider import ProviderStreamEvent
from ouro.core.state import RunRecord
from ouro.core.tools import ToolCallRequest
from ouro.providers import FauxProvider
from ouro.tools.builtin.files import builtin_file_tools
from ouro.tui.app import OuroTuiApp
from ouro.tui.conversation.history import ConversationHistory
from ouro.tui.conversation.history_models import TranscriptEntry
from ouro.tui.conversation.view import ConversationView
from ouro.tui.menus.choice import ChoiceScreen
from ouro.tui.preferences import TuiPreferences, TuiPreferencesStore
from ouro.tui.state import RunPresentation
from ouro.tui.theme import DEFAULT_TUI_THEME, TUI_THEMES
from tests.tui.app.support import _persist_direct_child_session
from tests.tui.helpers import (
    choose_menu,
    release_on_exit,
    resize_terminal,
    run_ready_app,
    wait_for_events,
    wait_for_screen,
    wait_until,
)
from tests.tui.test_tasks import streaming_application


class TraceApp(App[None]):
    CSS = """
    #conversation { height: 1fr; }
    #conversation-transcript { height: auto; min-height: 100%; }
    #conversation-history { height: auto; overflow: hidden; }
    """

    def __init__(self, *, collapsed=True):
        super().__init__()
        self.tui_theme = DEFAULT_TUI_THEME
        self.conversation = ConversationView(collapse_traces=collapsed)

    def compose(self) -> ComposeResult:
        yield self.conversation


async def test_collapsed_history_prepares_only_visible_entries_and_expands_once(monkeypatch):
    import ouro.tui.conversation.history as history_module

    rendered = []
    render = history_module._render_entry_requests

    def record(requests, **kwargs):
        rendered.extend(request.entry_index for request in requests)
        return render(requests, **kwargs)

    monkeypatch.setattr(history_module, "_render_entry_requests", record)
    app = TraceApp()
    async with app.run_test(size=(80, 24)):
        history = app.query_one(ConversationHistory)
        await history.load(trace_entries())
        assert set(rendered) == {0, 4, 5, 6, 7}
        assert not {1, 2, 3}.intersection(history._piece_indexes_by_entry)
        rendered.clear()
        await history.toggle_trace(7)
        assert set(rendered) == {1, 2, 3, 7}
        expanded = history.rendered_text
        rendered.clear()
        await history.toggle_trace(7)
        await history.toggle_trace(7)
        assert set(rendered) == {7}
        assert history.rendered_text == expanded


async def test_cached_collapsed_history_can_restore_with_expanded_default():
    app = TraceApp()
    async with app.run_test(size=(80, 24)):
        history = app.query_one(ConversationHistory)
        entries = trace_entries()
        await history.load(entries)
        state = history.take_state()
        await history.load(trace_entries("other"))
        await app.conversation.set_collapse_traces(False)
        await history.load(entries, reusable_state=state)
        assert "Earlier thought" in history.rendered_text
        assert "Investigating" in history.rendered_text


@pytest.mark.parametrize("disclosure", ["trace", "reasoning"])
async def test_first_disclosure_keeps_old_frame_and_accepts_input(disclosure, monkeypatch):
    import ouro.tui.conversation.history as history_module

    app = TraceApp()
    async with app.run_test(size=(80, 24)) as pilot:
        history = app.query_one(ConversationHistory)
        field = Input()
        await app.mount(field)
        field.focus()
        if disclosure == "trace":
            await history.load(trace_entries())
        else:
            await app.conversation.set_reasoning_expanded(False)
            await history.load(
                (TranscriptEntry(kind="assistant", reasoning="long thought " * 4000),)
            )
        old_lines = list(history.lines)
        entered, release = Event(), Event()
        render = history_module._render_entry_requests

        def held(requests, **kwargs):
            entered.set()
            assert release.wait(10)
            return render(requests, **kwargs)

        monkeypatch.setattr(history_module, "_render_entry_requests", held)
        change = asyncio.create_task(
            history.toggle_trace(7) if disclosure == "trace" else history.toggle_reasoning(0)
        )
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            await pilot.press("x")
            assert field.value == "x"
            assert history.lines == old_lines
        finally:
            release.set()
            await change
        assert history.lines != old_lines


async def test_new_run_classifies_only_its_own_trace(monkeypatch):
    app = TraceApp()
    async with app.run_test(size=(80, 24)):
        history = app.query_one(ConversationHistory)
        await history.load(trace_entries("old"))

        def unexpected_rebuild():
            raise AssertionError("a new Run must not rescan settled history")

        monkeypatch.setattr(history, "_refresh_traces", unexpected_rebuild)
        history.append_many(trace_entries("new"))
        await history.wait_until_rendered()
        assert set(history._trace_summary_indexes) == {"old", "new"}
        assert history._trace_run_by_entry[9] == "new"


def trace_entries(run_id="run", *, status="completed", final=True):
    entries = [
        TranscriptEntry(kind="user", text="Question", run_id=run_id),
        TranscriptEntry(kind="assistant", reasoning="Earlier thought", run_id=run_id),
        TranscriptEntry(kind="assistant", text="Investigating", run_id=run_id),
        TranscriptEntry(
            kind="tool",
            run_id=run_id,
            tool=RuntimeToolCallActivity(
                tool_call_id=f"call_{run_id}",
                tool_name="Read",
                source_index=0,
                status="completed",
            ),
        ),
    ]
    if final:
        entries.extend(
            (
                TranscriptEntry(kind="assistant", reasoning="Final thought", run_id=run_id),
                TranscriptEntry(kind="assistant", text="Final answer", run_id=run_id),
                TranscriptEntry(kind="assistant", text="Final paragraph", run_id=run_id),
            )
        )
    entries.append(
        TranscriptEntry(
            kind="run",
            run_id=run_id,
            run=RunPresentation(
                run_id=run_id,
                status=status,
                model="Test model",
                duration_seconds=1,
            ),
        )
    )
    return tuple(entries)


@pytest.mark.parametrize("status", ["completed", "failed", "aborted"])
@pytest.mark.parametrize("collapsed", [True, False])
async def test_live_trace_collapses_only_at_summary_and_retains_final_thinking(status, collapsed):
    app = TraceApp(collapsed=collapsed)
    async with app.run_test(size=(80, 24)):
        history = app.query_one(ConversationHistory)
        await app.conversation.set_reasoning_expanded(True)
        entries = trace_entries(status=status)
        history.append_many(entries[:-1])
        await history.wait_until_rendered()
        assert "Investigating" in history.rendered_text
        assert "Earlier thought" in history.rendered_text
        history.append(entries[-1])
        await history.wait_until_rendered()
        assert ("Investigating" not in history.rendered_text) == collapsed
        assert ("Earlier thought" not in history.rendered_text) == collapsed
        assert "Final thought" in history.rendered_text
        assert "Final answer" in history.rendered_text
        assert "Final paragraph" in history.rendered_text
        assert "Question" in history.rendered_text
        live = history.rendered_text
        await history.load(entries)
        assert history.rendered_text == live


@pytest.mark.parametrize("ending", ["tool", "thinking", "empty", "answer_only"])
async def test_runs_without_a_final_text_or_without_trace_cannot_collapse(ending):
    app = TraceApp()
    async with app.run_test():
        history = app.query_one(ConversationHistory)
        entries = list(trace_entries(final=False))
        if ending == "thinking":
            entries.insert(
                -1,
                TranscriptEntry(
                    kind="assistant",
                    run_id="run",
                    reasoning="Unfinished thought",
                ),
            )
        elif ending == "empty":
            entries.insert(-1, TranscriptEntry(kind="assistant", run_id="run", text="  "))
        elif ending == "answer_only":
            entries = list(trace_entries())
            entries = [entries[0], *entries[4:]]
        await history.load(entries)
        before = history.rendered_text
        await history.toggle_trace(len(entries) - 1)
        assert history.rendered_text == before
        assert not any(
            "ouro_trace_entry" in segment.style.meta
            for line in history.lines
            for segment in line
            if segment.style
        )


async def test_trace_toggle_reuses_markdown_and_hidden_tool_updates_stay_hidden(monkeypatch):
    app = TraceApp()
    async with app.run_test(size=(80, 24)):
        history = app.query_one(ConversationHistory)
        entries = trace_entries()
        await history.load(entries)
        answer_rows = history._pieces[history._piece_indexes_by_entry[5][0]].rows

        def reject_markdown(*args, **kwargs):
            raise AssertionError("Trace toggle must reuse Markdown strips")

        monkeypatch.setattr("ouro.tui.markdown.assistant_markdown", reject_markdown)
        tool = entries[3].tool.model_copy(update={"status": "failed"})
        history.update_tools((tool,), run_id="run")
        assert "Read" not in history.rendered_text
        await history.toggle_trace(7)
        assert "Investigating" in history.rendered_text
        assert "Read" in history.rendered_text
        assert history._pieces[history._piece_indexes_by_entry[5][0]].rows is answer_rows
        await history.toggle_trace(7)
        assert "Investigating" not in history.rendered_text


@pytest.mark.parametrize("width", [24, 80])
async def test_hover_and_click_keep_summary_height_and_viewport_anchor(width):
    app = TraceApp()
    app.tui_theme = replace(DEFAULT_TUI_THEME, section_separator_mark="~")
    async with app.run_test(size=(width, 20)) as pilot:
        history = app.query_one(ConversationHistory)
        entries = (
            *(TranscriptEntry(kind="user", text=f"Previous {i}") for i in range(12)),
            *trace_entries(),
        )
        await history.load(entries)
        await pilot.pause()
        summary_index = len(entries) - 1
        screen_y = history.region.y + history.entry_line(summary_index)
        row_count = len(history.lines)
        await pilot.hover(offset=(4, screen_y))
        await pilot.pause()
        assert len(history.lines) == row_count
        if width == 80:
            assert f"{app.tui_theme.section_separator}show trace" in history.rendered_text
        assert any(
            DEFAULT_TUI_THEME.collapsed_disclosure_marker in line.text for line in history.lines
        )
        await pilot.click(offset=(4, screen_y))
        await pilot.pause()
        assert "Investigating" in history.rendered_text
        if width == 80:
            assert f"{app.tui_theme.section_separator}hide trace" in history.rendered_text
        assert history.region.y + history.entry_line(summary_index) == screen_y
        await pilot.click(offset=(4, screen_y))
        await pilot.pause()
        assert "Investigating" not in history.rendered_text
        assert history.region.y + history.entry_line(summary_index) == screen_y
        await pilot.hover(offset=(0, 0))
        await pilot.pause()
        assert "show trace" not in history.rendered_text
        assert "hide trace" not in history.rendered_text
        assert f"{app.tui_theme.brand_mark} Test model" in history.rendered_text


async def test_final_thinking_disclosure_remains_independent_of_trace():
    app = TraceApp()
    async with app.run_test(size=(80, 24)):
        history = app.query_one(ConversationHistory)
        await app.conversation.set_reasoning_expanded(False)
        await history.load(trace_entries())
        assert "Thought" in history.rendered_text
        assert "Final thought" not in history.rendered_text
        await history.toggle_reasoning(4)
        assert "Final thought" in history.rendered_text
        assert "Earlier thought" not in history.rendered_text
        await history.toggle_trace(7)
        assert "Final thought" in history.rendered_text
        assert "Earlier thought" not in history.rendered_text
        await history.toggle_reasoning(1)
        await history.toggle_trace(7)
        assert "Earlier thought" not in history.rendered_text
        assert "Final thought" in history.rendered_text


async def test_trace_choices_survive_session_cache_resize_theme_and_prepend():
    app = TraceApp()
    async with app.run_test(size=(80, 24)) as pilot:
        history = app.query_one(ConversationHistory)
        entries = trace_entries()
        await history.load(entries)
        await history.toggle_trace(7)
        state = history.take_state()
        await history.load(trace_entries("other"))
        assert "Investigating" not in history.rendered_text
        await resize_terminal(pilot, 60, 24)
        await history.load(entries, reusable_state=state)
        assert "Investigating" in history.rendered_text
        app.tui_theme = TUI_THEMES["indigo"]
        history.refresh_theme()
        await history.wait_for_theme_refresh()
        assert "Investigating" in history.rendered_text
        history.prepend(trace_entries("older"))
        await history.wait_until_rendered()
        assert history.rendered_text.count("Investigating") == 1
        await history.toggle_trace(15)
        assert "Investigating" not in history.rendered_text
        await app.conversation.set_collapse_traces(False)
        # Explicit choices stay local; the default applies to untouched Runs.
        assert history.rendered_text.count("Investigating") == 1


async def test_prepend_adds_disclosure_to_previously_answer_only_run():
    app = TraceApp()
    async with app.run_test(size=(80, 24)):
        history = app.query_one(ConversationHistory)
        entries = trace_entries()
        await history.load(entries[4:])
        assert not history._trace_summary_indexes
        history.prepend(entries[:4])
        await history.wait_until_rendered()
        assert "Investigating" not in history.rendered_text
        assert any(
            segment.style.meta.get("ouro_trace_entry") == 7
            for line in history.lines
            for segment in line
            if segment.style
        )
        await history.toggle_trace(7)
        assert "Investigating" in history.rendered_text
        assert "Final answer" in history.rendered_text


async def test_actions_trace_setting_persists_and_is_available_to_child_observers(tmp_path):
    app = OuroTuiApp(home=tmp_path, application=streaming_application(tmp_path, FauxProvider()))
    async with run_ready_app(app, size=(100, 32)) as pilot:
        assert TuiPreferences().collapse_traces is True
        await pilot.press("ctrl+p")
        await wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        await choose_menu(pilot, "Collapse traces", title="COLLAPSE TRACES")
        menu = app.screen
        assert isinstance(menu, ChoiceScreen)
        assert menu.query_one("#choice-title", Static).render().plain == "COLLAPSE TRACES"
        await choose_menu(pilot, "Off")
        await wait_until(
            lambda: not app._preferences.collapse_traces,
            pilot,
            description="trace preference saved",
        )
        assert TuiPreferencesStore(tmp_path).load().collapse_traces is False
        assert app.screen is menu
        app._visible_read_only = True
        assert any(choice.label == "Collapse traces" for choice in app._action_choices())


class TraceProvider:
    provider_id = "faux"

    def __init__(self):
        self.round = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(self, **kwargs):
        self.round += 1
        if self.round == 1:
            yield ProviderStreamEvent(type="reasoning_delta", reasoning_content="Earlier thought")
            yield ProviderStreamEvent(type="text_delta", text="Investigating")
            yield ProviderStreamEvent.from_tool_call(
                {"id": "read_file", "name": "Read", "arguments": {"path": "sample.txt"}}
            )
        else:
            yield ProviderStreamEvent(type="reasoning_delta", reasoning_content="Final thought")
            yield ProviderStreamEvent(type="text_delta", text="Final answer\n\nFinal paragraph")
            self.started.set()
            await self.release.wait()
        yield ProviderStreamEvent(type="done")


@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_real_tool_loop_stays_visible_until_completion_and_matches_reload(tmp_path, backend):
    provider = TraceProvider()
    (tmp_path / "sample.txt").write_text("Evidence", encoding="utf-8")
    application = streaming_application(tmp_path, provider, backend=backend)
    application.runtime.tools.register(builtin_file_tools()[0])
    app = OuroTuiApp(home=tmp_path, application=application)
    async with (
        run_ready_app(app, size=(100, 32)) as pilot,
        release_on_exit(provider.release),
    ):
        await app._set_reasoning_expanded("expand")
        session = application.runtime.sessions.create(title="Tool trace").session
        await app._open_session(session.session_id)
        app._submit_run("Read sample.txt", "follow_up")
        await wait_for_events(
            provider.started, pilot=pilot, description="tool loop resumed model stream"
        )
        history = app.query_one(ConversationHistory)
        await wait_until(
            lambda: "Investigating" in history.rendered_text,
            pilot,
            description="live tool trace",
        )
        assert not any(entry.kind == "run" for entry in history.entries)
        provider.release.set()
        await wait_until(
            lambda: any(entry.kind == "run" for entry in history.entries),
            pilot,
            description="completed tool trace",
        )
        await history.wait_until_rendered()
        assert "Investigating" not in history.rendered_text
        assert "Earlier thought" not in history.rendered_text
        assert "Final thought" in history.rendered_text
        assert "Final answer" in history.rendered_text
        assert "Final paragraph" in history.rendered_text
        assert next(entry.tool for entry in history.entries if entry.tool).status == "completed"

        # Live and durable thinking timers have different clock boundaries.
        # Compare the transcript independently of that existing label.
        def visible_content():
            return [
                line
                for line in history.rendered_text.splitlines()
                if f"Thought{app.tui_theme.section_separator}" not in line
            ]

        live = visible_content()
        await app._open_session(session.session_id)
        await history.wait_until_rendered()
        assert visible_content() == live


@pytest.mark.parametrize("kind", ["main", "subagent", "task"])
async def test_canonical_trace_projection_is_shared_by_all_session_kinds(tmp_path, kind):
    application = streaming_application(tmp_path, FauxProvider())
    root_id, child_id = _persist_direct_child_session(application)
    task = application.runtime.sessions.create(title="Task trace", namespace="task").session
    session_id = {"main": root_id, "subagent": child_id, "task": task.session_id}[kind]
    run_id = f"trace_{kind}"
    application.runtime.store.save_run(
        RunRecord(
            run_id=run_id,
            session_id=session_id,
            status="completed",
            started_at=datetime.now(UTC) - timedelta(seconds=2),
            ended_at=datetime.now(UTC),
        )
    )
    messages = [
        MessageRecord(
            session_id=session_id,
            run_id=run_id,
            role="assistant",
            content=[TextContent(text="Investigating")],
            tool_calls=[ToolCallRequest(id="read", name="Read", arguments={"path": "sample.txt"})],
            reasoning_content="Earlier thought",
        ),
        MessageRecord(
            session_id=session_id,
            run_id=run_id,
            role="assistant",
            content=[TextContent(text="Final answer\n\nFinal paragraph")],
            reasoning_content="Final thought",
        ),
    ]
    from tests.tool_batches import seed_tool_messages

    seed_tool_messages(
        application.runtime.store,
        messages[0],
        [],
        run=application.runtime.store.load_run(session_id, run_id),
    )
    application.runtime.store.append_messages(messages)
    app = OuroTuiApp(home=tmp_path, application=application)
    async with run_ready_app(app, size=(100, 32)):
        await app._open_session(root_id)
        await app._show_observed_session(session_id)
        await app._set_reasoning_expanded("expand")
        history = app.query_one(ConversationHistory)
        await history.wait_until_rendered()
        assert "Investigating" not in history.rendered_text
        assert "Final thought" in history.rendered_text
        assert "Final answer" in history.rendered_text
        assert "Final paragraph" in history.rendered_text
        summary_index = next(
            index
            for index, entry in enumerate(history.entries)
            if entry.kind == "run" and entry.run_id == run_id
        )
        await history.toggle_trace(summary_index)
        assert "Investigating" in history.rendered_text
        assert "Earlier thought" in history.rendered_text


async def test_completion_publishes_fold_and_summary_together(monkeypatch):
    import ouro.tui.conversation.history as history_module

    app = TraceApp()
    started, release = Event(), Event()
    render = history_module._render_entry_requests

    def blocked_render(*args, **kwargs):
        started.set()
        assert release.wait(6)
        return render(*args, **kwargs)

    async with app.run_test(size=(80, 24)) as pilot:
        history = app.query_one(ConversationHistory)
        entries = trace_entries()
        await history.load(entries[:-1])
        before = history.rendered_text
        monkeypatch.setattr(history_module, "_render_entry_requests", blocked_render)
        try:
            history.append_many((entries[-1], TranscriptEntry(kind="notice", text="Done")))
            async with asyncio.timeout(5):
                while not started.is_set():
                    await pilot.pause()
            assert history.rendered_text == before
        finally:
            release.set()
        await history.wait_until_rendered()
        assert "Investigating" not in history.rendered_text
        assert "Test model" in history.rendered_text
        assert "Done" in history.rendered_text

import asyncio
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest
import rich.text as conversation_history_text
from rich.console import Console
from textual.app import App, ComposeResult
from textual.geometry import Size
from textual.widgets import Input

import ouro.tui.conversation.history as conversation_history
import ouro.tui.conversation.markdown_stream as conversation_history_markdown_stream
import ouro.tui.markdown as tui_markdown
from ouro.application.contracts.execution import RuntimeToolCallActivity
from ouro.tui.conversation.history import ConversationHistory
from ouro.tui.conversation.history_models import CompactionPresentation, TranscriptEntry
from ouro.tui.conversation.history_rendering import (
    AssistantTailLines,
    _compaction_summary,
    _format_user_message_timestamp,
    _MessageContent,
    _reasoning_label_text,
    _run_summary,
)
from ouro.tui.conversation.markdown_stream import AssistantMarkdownStream, split_markdown_blocks
from ouro.tui.conversation.messages import ToolNavigationRequested
from ouro.tui.conversation.tools import ToolActivityRow
from ouro.tui.conversation.turn import AssistantTurn
from ouro.tui.conversation.view import ConversationView
from ouro.tui.state import RunPresentation
from ouro.tui.theme import DEFAULT_TUI_THEME, TuiTheme
from tests.tui.helpers import resize_terminal, wait_until

_MULTIBLOCK_ANSWER = """Here is the plan:

- step one
- step two

## Details

```python
x = 1
```

| col | val |
| --- | --- |
| a | 1 |

> quoted text

Setext Title
============

Final paragraph.
"""


def _style_at(text, fragment: str):
    return text.get_style_at_offset(Console(), text.plain.index(fragment))


def test_single_tool_entry_uses_background_rendering() -> None:
    entry = TranscriptEntry(
        kind="tool",
        tool=RuntimeToolCallActivity(
            tool_call_id="tool_background",
            tool_name="Probe",
            source_index=0,
            status="completed",
        ),
    )

    assert ConversationHistory._requires_background_render((entry,))


@pytest.mark.parametrize("title", ["Thinking", "Thought"])
@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(2.5, "2.5s"), (3_661.2, "1h 1m 01.2s"), (93_784.5, "1d 2h 3m 04.5s")],
)
def test_reasoning_label_shows_title_and_duration(
    title: str, seconds: float, expected: str
) -> None:
    label = _reasoning_label_text(title, seconds, expanded=False)

    assert label.plain == f"+ {title} • {expected}"
    title_style = _style_at(label, title)
    duration_style = _style_at(label, expected)
    assert not title_style.bold
    assert not duration_style.bold


def test_user_message_timestamp_uses_local_date_only_before_today() -> None:
    local_zone = timezone(timedelta(hours=8))
    now = datetime(2026, 9, 16, 16, 20, tzinfo=local_zone)

    assert (
        _format_user_message_timestamp(
            datetime(2026, 9, 16, 8, 17, tzinfo=UTC),
            now=now,
        )
        == "16:17"
    )
    assert (
        _format_user_message_timestamp(
            datetime(2026, 9, 15, 8, 17, tzinfo=UTC),
            now=now,
        )
        == "2026/9/15 • 16:17"
    )


def test_user_message_timestamp_is_a_separate_line() -> None:
    theme = DEFAULT_TUI_THEME
    console = Console(force_terminal=True, color_system="truecolor", width=60)
    rendered_lines = console.render_lines(
        _MessageContent(
            "user message",
            theme,
            created_at=datetime.now().astimezone(),
        ),
        console.options.update_width(60),
        pad=False,
    )
    message_line = next(
        line
        for line in rendered_lines
        if "user message" in "".join(segment.text for segment in line)
    )
    timestamp_line = next(
        line for line in rendered_lines if ":" in "".join(segment.text for segment in line)
    )
    timestamp_segment = next(segment for segment in timestamp_line if ":" in segment.text)

    assert message_line is not timestamp_line
    assert timestamp_segment.style is not None


def test_assistant_markdown_and_user_message_preserve_text() -> None:
    theme = DEFAULT_TUI_THEME
    source = """# Heading

Answer paragraph with *emphasis* and **strong**.

- List body

| Column |
| --- |
| Cell value |

`inline`

```python
def example(value):
    return value
```

```unknown-ouro-lexer
plain value
```
"""
    console = Console(force_terminal=True, color_system="truecolor", width=60)
    markdown_segments = list(console.render(tui_markdown.assistant_markdown(source, theme)))

    heading = next(segment for segment in markdown_segments if segment.text == "Heading")
    assert heading.style is not None and heading.style.bold
    rendered = "".join(segment.text for segment in markdown_segments)
    for fragment in (
        "inline",
        "def",
        "plain value",
        "Answer paragraph",
        "emphasis",
        "strong",
        "List body",
        "Cell value",
    ):
        assert fragment in rendered

    user_segments = list(
        console.render(
            _MessageContent("user message", theme),
            console.options.update_width(60),
        )
    )
    assert "user message" in "".join(segment.text for segment in user_segments)


def test_assistant_markdown_failure_degrades_only_to_canonical_text(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    source = "sensitive markdown source"

    class BrokenMarkdown:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError(source)

    monkeypatch.setattr(tui_markdown, "_ProseMarkdown", BrokenMarkdown)
    console = Console(width=40)
    segments = list(console.render(tui_markdown.assistant_markdown(source, DEFAULT_TUI_THEME)))
    rendered = "".join(segment.text for segment in segments)

    assert source in rendered
    assert source not in caplog.text
    assert "Could not render assistant Markdown" in caplog.text


class _HistoryApp(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.history = ConversationHistory(
            lambda tool, **_kwargs: conversation_history_text.Text(tool.tool_name)
        )
        self.navigation: list[tuple[str, str]] = []

    def compose(self) -> ComposeResult:
        yield self.history

    def on_tool_navigation_requested(self, message: ToolNavigationRequested) -> None:
        self.navigation.append((message.parent_run_id, message.parent_tool_call_id))


class _ThemedHistoryApp(_HistoryApp):
    def __init__(self) -> None:
        self._tui_theme = DEFAULT_TUI_THEME
        super().__init__()
        self.console.push_theme(self._tui_theme.rich_theme())

    @property
    def tui_theme(self) -> TuiTheme:
        return self._tui_theme

    def apply_theme(self, theme: TuiTheme) -> None:
        self._tui_theme = theme
        self.console.pop_theme()
        self.console.push_theme(theme.rich_theme())
        self.history.refresh_theme()


class _StreamingLinesApp(App[None]):
    def __init__(self, theme: TuiTheme = DEFAULT_TUI_THEME) -> None:
        super().__init__()
        self.tui_theme = theme
        self.tail = AssistantTailLines()

    def compose(self) -> ComposeResult:
        yield self.tail


class _ConversationApp(App[None]):
    def compose(self) -> ComposeResult:
        yield ConversationView()
        yield Input()


@pytest.mark.parametrize("overflow", [0, 7])
async def test_history_scroll_restore_survives_intermediate_layout_sizes(overflow):
    app = _ConversationApp()
    async with app.run_test(size=(100, 30)) as pilot:
        conversation = app.query_one(ConversationView)
        history = app.query_one(ConversationHistory)
        conversation.styles.scrollbar_size_vertical = 0
        conversation.styles.scrollbar_size_horizontal = 0
        conversation.query_one("#conversation-transcript").styles.height = "auto"
        history.styles.height = "auto"
        history.styles.overflow_x = "hidden"
        history.styles.overflow_y = "hidden"
        await history.load(
            tuple(TranscriptEntry(kind="user", text=f"Message {index}") for index in range(30))
        )
        await pilot.pause()
        size = conversation.scrollable_content_region.size
        target = conversation.max_scroll_y
        assert target > 0
        conversation._restore_scroll_y(target + overflow)
        # One reflow can measure a larger viewport before sibling layout settles.
        conversation.arrange(Size(size.width, size.height + 1))
        conversation.arrange(size)
        assert conversation.scroll_y == target
        await pilot.pause()
        assert conversation.scroll_y == target
        assert conversation._restored_scroll_y is None
        conversation.scroll_to(y=target - 3, animate=False, immediate=True)
        await pilot.pause()
        assert conversation.scroll_y == target - 3


@pytest.mark.parametrize("show_scaffolds", [False, True])
@pytest.mark.parametrize("width", [32, 100])
async def test_command_output_preserves_literal_content_and_frame_layout(show_scaffolds, width):
    app = _ConversationApp()
    source = 'Goal: active\n\n{\n    "value": "**中文**"\n}\n```literal```'
    async with app.run_test(size=(width, 30)) as pilot:
        conversation = app.query_one(ConversationView)
        conversation.show_scaffold_messages = show_scaffolds
        await conversation.add_user("Real input")
        await conversation.append_assistant("Answer before command")
        await conversation.add_command_output(source)
        await conversation.add_command_output("Done")
        history = app.query_one(ConversationHistory)
        await history.wait_until_rendered()
        assert [entry.kind for entry in history.entries] == [
            "user",
            "assistant",
            "command",
            "notice",
        ]
        command = history.entries[2]
        assert command.text == source
        assert command.created_at is None and not command.scaffold
        assert history.user_turns == ("Real input",)
        assert len(history.user_lines) == 1
        assert "▣ Done" in history.rendered_text
        assert "```literal```" in history.rendered_text
        body = next(
            segment for line in history.lines for segment in line if "Goal: active" in segment.text
        )
        assert not body.style.bold and not body.style.italic

        # Both types use the same frame geometry; commands keep their own semantics.
        await history.load((command, replace(command, kind="scaffold", scaffold=True)))
        pieces = history._pieces
        assert len(pieces) == 2
        assert [line.text for line in pieces[0].strips] == [line.text for line in pieces[1].strips]
        await resize_terminal(pilot, width + 12, 30)
        await history.wait_until_rendered()
        assert history.entries[0].text == source
        assert '    "value": "**中文**"' in history.rendered_text


@pytest.mark.parametrize("finish", [False, True])
async def test_live_answer_stays_visible_until_its_history_is_ready(monkeypatch, finish):
    app = _ConversationApp()
    entered, release = threading.Event(), threading.Event()
    render = conversation_history._render_entry_requests

    def held_render(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return render(*args, **kwargs)

    async with app.run_test(size=(100, 30)) as pilot:
        conversation = app.query_one(ConversationView)
        history = app.query_one(ConversationHistory)
        turn = app.query_one(AssistantTurn)
        await conversation.append_assistant("Visible answer " + "content " * 300)
        await pilot.pause()
        monkeypatch.setattr(conversation_history, "_render_entry_requests", held_render)
        transferring = asyncio.create_task(
            conversation.finish_assistant()
            if finish
            else conversation.append_assistant("\n\nNext paragraph")
        )
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            await pilot.pause()
            assert "Visible answer" not in history.rendered_text
            assert turn.display and turn.tail.display
            assert "Visible answer" in turn.tail.text
            app.query_one(Input).focus()
            await pilot.press("x")
            assert app.query_one(Input).value == "x"
        finally:
            release.set()
            await transferring
        await history.wait_until_rendered()
        assert "Visible answer" in history.rendered_text
        if finish:
            assert not turn.display
        else:
            assert turn.tail.text == "Next paragraph"


@pytest.mark.parametrize("content", ["reasoning", "tools"])
async def test_live_reasoning_and_tools_transfer_only_after_history_renders(monkeypatch, content):
    app = _ConversationApp()
    entered, release = threading.Event(), threading.Event()
    render = conversation_history._render_entry_requests

    def held_render(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return render(*args, **kwargs)

    async with app.run_test(size=(100, 30)) as pilot:
        conversation = app.query_one(ConversationView)
        history = app.query_one(ConversationHistory)
        turn = app.query_one(AssistantTurn)
        if content == "reasoning":
            await conversation.append_reasoning("Visible thought " + "reasoning " * 300)
        else:
            tool = RuntimeToolCallActivity(
                tool_call_id="call_transfer", tool_name="Read", status="running", source_index=0
            )
            await conversation.sync_tools((tool,))
        await pilot.pause()
        monkeypatch.setattr(conversation_history, "_render_entry_requests", held_render)
        transferring = asyncio.create_task(
            conversation.finish_assistant()
            if content == "reasoning"
            else conversation.sync_tools((tool.model_copy(update={"status": "completed"}),))
        )
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            await pilot.pause()
            assert not history.lines
            if content == "reasoning":
                assert turn.display and turn.reasoning.display
                assert "Visible thought" in turn.reasoning.text
            else:
                assert conversation.query_one(ToolActivityRow).is_attached
        finally:
            release.set()
            await transferring
        assert history.lines
        assert not turn.display
        assert not conversation.query(ToolActivityRow)


class _LiveToolApp(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.navigation: list[tuple[str, str]] = []

    def compose(self) -> ComposeResult:
        yield ToolActivityRow(
            RuntimeToolCallActivity(
                tool_call_id="call_live_spawn",
                tool_name="SpawnAgent",
                status="running",
                source_index=0,
            ),
            run_id="run_live_parent",
        )

    def on_tool_navigation_requested(self, message: ToolNavigationRequested) -> None:
        self.navigation.append((message.parent_run_id, message.parent_tool_call_id))


@pytest.mark.asyncio
async def test_streaming_tail_strips_carry_styles_and_preserve_text() -> None:
    app = _StreamingLinesApp()

    async with app.run_test(size=(40, 8)) as pilot:
        await pilot.pause()
        app.tail.append("streaming tail\nnext line")
        await pilot.pause()

        strips = (*app.tail._line_strips, *app.tail._pending_strips)
        assert strips
        assert app.tail.rendered_text == "streaming tail\nnext line"
        assert all(segment.style is not None for strip in strips for segment in strip)


@pytest.mark.asyncio
async def test_first_appended_batch_is_registered_before_rendering() -> None:
    app = _HistoryApp()

    async with app.run_test(size=(100, 30)):
        app.history.append_many(
            (
                TranscriptEntry(kind="assistant", reasoning="Inspect the workspace."),
                TranscriptEntry(kind="assistant", text="Before tool."),
            )
        )

        assert len(app.history.entries) == 2
        await app.history.wait_until_rendered()
        assert "Inspect the workspace." in app.history.rendered_text
        assert "Before tool." in app.history.rendered_text


@pytest.mark.asyncio
async def test_wait_until_rendered_recovers_after_a_deferred_layout_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _HistoryApp()

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        schedule_render = app.history._schedule_buffer_render
        layout_available = False

        def render_when_visible(*, rebuild=True) -> None:
            if layout_available:
                schedule_render(rebuild=rebuild)

        monkeypatch.setattr(app.history, "_schedule_buffer_render", render_when_visible)
        # Model the welcome transition after its one-shot deferred render ran
        # while the conversation ancestor still had no layout geometry.
        app.history._reset_buffer([TranscriptEntry(kind="user", text="first message")])
        await pilot.pause()
        assert app.history.entries
        assert app.history.rendered_text == ""

        layout_available = True
        await app.history.wait_until_rendered()

        assert "first message" in app.history.rendered_text


@pytest.mark.asyncio
async def test_same_session_history_reuse_renders_only_the_changed_suffix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _HistoryApp()
    entries = tuple(
        TranscriptEntry(
            kind="user" if index % 2 == 0 else "assistant",
            text=f"history entry {index}",
        )
        for index in range(120)
    )

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await app.history.load(entries)
        state = app.history.take_state()
        rendered_indexes: list[int] = []
        write_entry = app.history._write_entry

        def record_write_entry(*args, entry_index: int, **kwargs) -> None:
            rendered_indexes.append(entry_index)
            write_entry(*args, entry_index=entry_index, **kwargs)

        monkeypatch.setattr(app.history, "_write_entry", record_write_entry)
        render_entries = conversation_history._render_entry_requests

        def record_render_entries(requests, **kwargs):
            rendered_indexes.extend(request.entry_index for request in requests)
            return render_entries(requests, **kwargs)

        monkeypatch.setattr(conversation_history, "_render_entry_requests", record_render_entries)
        await app.history.load(entries, reusable_state=state)

        assert rendered_indexes == []
        assert app.history.entries == entries
        assert "history entry 119" in app.history.rendered_text

        state = app.history.take_state()
        appended = (*entries, TranscriptEntry(kind="assistant", text="new tail"))
        await app.history.load(appended, reusable_state=state)

        assert rendered_indexes == [120]
        assert app.history.entries == appended
        assert "new tail" in app.history.rendered_text


@pytest.mark.parametrize("kind", ["user", "command"])
async def test_prepend_reuses_existing_strips_and_remaps_message_click_targets(monkeypatch, kind):
    app = _HistoryApp()
    entries = tuple(TranscriptEntry(kind=kind, text=f"recent {i}") for i in range(30))
    older = tuple(TranscriptEntry(kind="assistant", text=f"older {i}") for i in range(12))
    rendered = []
    original = conversation_history._render_entry_requests

    def counted(requests, **kwargs):
        rendered.extend(request.entry.text for request in requests)
        return original(requests, **kwargs)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await app.history.load(entries)
        monkeypatch.setattr(conversation_history, "_render_entry_requests", counted)
        app.history.prepend(older)
        app.history.append(TranscriptEntry(kind="assistant", text="live tail"))
        await app.history.wait_until_rendered()
        assert app.history.entries == (
            *older,
            *entries,
            TranscriptEntry(kind="assistant", text="live tail"),
        )
        assert rendered == [*(entry.text for entry in older), "recent 0", "live tail"]
        # This message frame was retained, including its remapped right-click target.
        indexes = {
            segment.style.meta.get("ouro_message")
            for piece in app.history._pieces
            if piece.entry_index == 17
            for strip in piece.strips
            for segment in strip
            if segment.style is not None and "ouro_message" in segment.style.meta
        }
        assert indexes == {17}
        assert app.history.rendered_text.index("older 0") < app.history.rendered_text.index(
            "recent 0"
        )


async def test_late_prefix_render_cannot_enter_a_replacement_history(monkeypatch):
    app = _HistoryApp()
    entered, release = threading.Event(), threading.Event()
    original = conversation_history._render_entry_requests

    def held(requests, **kwargs):
        if requests[0].entry.text == "older":
            entered.set()
            assert release.wait(5)
        return original(requests, **kwargs)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await app.history.load((TranscriptEntry(kind="user", text="current"),))
        monkeypatch.setattr(conversation_history, "_render_entry_requests", held)
        app.history.prepend((TranscriptEntry(kind="user", text="older"),))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            await app.history.load(
                (TranscriptEntry(kind="user", text="replacement"),), wait_for_render=False
            )
        finally:
            release.set()
        await app.history.wait_until_rendered()
        assert "replacement" in app.history.rendered_text
        assert "older" not in app.history.rendered_text
        assert "current" not in app.history.rendered_text


@pytest.mark.asyncio
async def test_replacing_unpublished_history_discards_old_render_and_stops_remaining_work(
    monkeypatch,
):
    app = _HistoryApp()
    old_entries = tuple(TranscriptEntry(kind="assistant", text=f"old {i}") for i in range(160))
    entered, release = threading.Event(), threading.Event()
    rendered = []
    original = conversation_history._render_entry_requests

    def held_render(requests, **kwargs):
        rendered.extend(request.entry.text for request in requests)
        if len(rendered) > 50 and not entered.is_set():
            entered.set()
            assert release.wait(5)
        return original(requests, **kwargs)

    async with app.run_test(size=(100, 30)):
        monkeypatch.setattr(conversation_history, "_render_entry_requests", held_render)
        await app.history.load(old_entries, wait_for_render=False)
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            state = app.history.take_state()
            assert state is not None and state.rendered_count == 0
            assert not state.lines
            await app.history.load(
                (TranscriptEntry(kind="user", text="new target"),), wait_for_render=False
            )
        finally:
            release.set()
        await app.history.wait_until_rendered()
        assert "new target" in app.history.rendered_text
        assert "old" not in app.history.rendered_text
        assert "old 100" not in rendered
        rendered.clear()
        await app.history.load(old_entries, reusable_state=state)
        assert rendered == [entry.text for entry in old_entries]
        assert "old 159" in app.history.rendered_text


@pytest.mark.asyncio
async def test_cached_history_publishes_missing_tail_together_without_replaying_it(monkeypatch):
    app = _HistoryApp()
    prefix = tuple(TranscriptEntry(kind="user", text=f"cached {i}") for i in range(40))
    tail = tuple(TranscriptEntry(kind="assistant", text=f"new {i}") for i in range(120))
    entered, release = threading.Event(), threading.Event()
    rendered = []
    render = conversation_history._render_entry_requests

    def held_render(requests, **kwargs):
        rendered.extend(request.entry.text for request in requests)
        if len(rendered) > 50 and not entered.is_set():
            entered.set()
            assert release.wait(5)
        return render(requests, **kwargs)

    async with app.run_test(size=(100, 30)) as pilot:
        await app.history.load(prefix)
        state = app.history.take_state()
        monkeypatch.setattr(conversation_history, "_render_entry_requests", held_render)
        await app.history.load((*prefix, *tail), reusable_state=state, wait_for_render=False)
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            await pilot.pause()
            assert "cached 39" in app.history.rendered_text
            assert "new 0" not in app.history.rendered_text
        finally:
            release.set()
        await app.history.wait_until_rendered()
        assert "new 119" in app.history.rendered_text
        assert rendered == [entry.text for entry in tail]


@pytest.mark.asyncio
async def test_failed_history_renderer_stops_until_content_is_reopened(monkeypatch):
    app = _HistoryApp()
    entries = tuple(TranscriptEntry(kind="assistant", text=f"entry {i}") for i in range(3))
    render = conversation_history._render_entry_requests
    failures, calls = [], []

    def fail(*args, **kwargs):
        calls.append(True)
        raise ValueError("invalid render input")

    async with app.run_test(size=(100, 30)) as pilot:
        monkeypatch.setattr(
            app.history, "notify", lambda message, **kwargs: failures.append(message)
        )
        monkeypatch.setattr(conversation_history, "_render_entry_requests", fail)
        with pytest.raises(RuntimeError, match="layout did not complete"):
            await app.history.load(entries)
        assert len(failures) == 1 and "ValueError" in failures[0]
        assert app.history.entries == entries
        app.history.append(TranscriptEntry(kind="assistant", text="later"))
        await resize_terminal(pilot, 80, 30)
        assert len(calls) == 1
        assert app.is_running
        monkeypatch.setattr(conversation_history, "_render_entry_requests", render)
        await app.history.load(entries)
        assert "entry 2" in app.history.rendered_text


@pytest.mark.asyncio
async def test_same_session_history_reuse_updates_mutable_entry_in_place(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _HistoryApp()
    running_tool = RuntimeToolCallActivity(
        tool_call_id="call_early",
        tool_name="Read",
        status="running",
        source_index=0,
    )
    entries = (
        TranscriptEntry(kind="user", text="inspect"),
        TranscriptEntry(kind="tool", run_id="run_1", tool=running_tool),
        *(
            TranscriptEntry(kind="assistant", text=f"settled history {index}")
            for index in range(100)
        ),
    )

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await app.history.load(entries)
        state = app.history.take_state()
        render_strips = app.history._render_strips
        rendered_pieces = 0

        def count_rendered_piece(renderable):
            nonlocal rendered_pieces
            rendered_pieces += 1
            return render_strips(renderable)

        monkeypatch.setattr(app.history, "_render_strips", count_rendered_piece)
        completed_tool = running_tool.model_copy(update={"status": "completed"})
        updated = (
            entries[0],
            TranscriptEntry(kind="tool", run_id="run_1", tool=completed_tool),
            *entries[2:],
        )
        await app.history.load(updated, reusable_state=state)

        assert rendered_pieces == 1
        assert app.history.entries == updated
        assert "settled history 99" in app.history.rendered_text


@pytest.mark.asyncio
async def test_cold_history_load_publishes_one_complete_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _HistoryApp()
    entries = tuple(
        TranscriptEntry(kind="assistant", text=f"history entry {index}") for index in range(120)
    )

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        sync_count = 0
        sync_virtual_size = app.history._sync_virtual_size

        def count_size_sync() -> None:
            nonlocal sync_count
            sync_count += 1
            sync_virtual_size()

        monkeypatch.setattr(app.history, "_sync_virtual_size", count_size_sync)
        await app.history.load(entries)

        assert sync_count == 1
        assert app.history.entries == entries


@pytest.mark.asyncio
async def test_large_history_markdown_renders_outside_the_ui_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _HistoryApp()
    source = "```python\n" + "value = 1\n" * 512 + "```\n"

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        entered = threading.Event()
        release = threading.Event()
        render_threads: list[int] = []
        render_entries = conversation_history._render_entry_requests

        def blocking_render(*args, **kwargs):
            render_threads.append(threading.get_ident())
            entered.set()
            release.wait()
            return render_entries(*args, **kwargs)

        monkeypatch.setattr(
            conversation_history,
            "_render_entry_requests",
            blocking_render,
        )
        watchdog = threading.Timer(1, release.set)
        watchdog.start()
        event_loop_thread = threading.get_ident()
        loading = asyncio.create_task(
            app.history.load((TranscriptEntry(kind="assistant", text=source),))
        )

        try:
            render_entered = await asyncio.to_thread(entered.wait, 2)
            event_loop_remained_responsive = not loading.done()
        finally:
            release.set()
            watchdog.cancel()
        await loading

        assert render_entered is True
        assert event_loop_remained_responsive is True
        assert render_threads != [event_loop_thread]
        assert "value = 1" in app.history.rendered_text


@pytest.mark.asyncio
async def test_theme_refresh_is_off_loop_and_coalesces_rapid_theme_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _ThemedHistoryApp()
    source = "```python\n" + "value = 1\n" * 512 + "```\n"

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await app.history.load((TranscriptEntry(kind="assistant", text=source),))
        entered = threading.Event()
        release = threading.Event()
        guard = threading.Lock()
        active_renders = 0
        maximum_active_renders = 0
        render_threads: list[int] = []
        render_entries = conversation_history._render_entry_requests

        def blocking_render(*args, **kwargs):
            nonlocal active_renders, maximum_active_renders
            assert threading.get_ident() != event_loop_thread
            with guard:
                active_renders += 1
                maximum_active_renders = max(maximum_active_renders, active_renders)
            render_threads.append(threading.get_ident())
            if len(render_threads) == 1:
                entered.set()
                release.wait()
            try:
                return render_entries(*args, **kwargs)
            finally:
                with guard:
                    active_renders -= 1

        monkeypatch.setattr(
            conversation_history,
            "_render_entry_requests",
            blocking_render,
        )
        first = replace(
            DEFAULT_TUI_THEME,
            theme_id="first",
            label="First",
            description="First preview",
            syntax_name="#88aacc",
        )
        latest = replace(
            first,
            theme_id="latest",
            label="Latest",
            description="Latest preview",
            syntax_name="#bbccdd",
        )
        event_loop_thread = threading.get_ident()
        app.apply_theme(first)

        try:
            assert await asyncio.to_thread(entered.wait, 30)
            app.apply_theme(latest)
            await pilot.pause()
            assert maximum_active_renders == 1
        finally:
            release.set()
        await wait_until(
            lambda: not app.history._theme_render_active,
            pilot,
            description="latest theme render completion",
        )

        assert app.tui_theme == latest
        assert app.history._theme_render_active is False
        assert render_threads
        assert all(thread_id != event_loop_thread for thread_id in render_threads)
        assert maximum_active_renders == 1
        next(segment for strip in app.history.lines for segment in strip if "value" in segment.text)


@pytest.mark.asyncio
async def test_history_resize_keeps_old_buffer_until_latest_width_is_ready(monkeypatch):
    app = _HistoryApp()
    loop_thread = threading.get_ident()
    async with app.run_test(size=(100, 30)) as pilot:
        await app.history.load((TranscriptEntry(kind="assistant", text="history " * 4096),))
        await pilot.pause()
        old_lines = tuple(app.history.lines)
        entered = threading.Event()
        release = threading.Event()
        threads = []
        render = conversation_history._render_entry_requests

        def blocked_render(*args, **kwargs):
            threads.append(threading.get_ident())
            if len(threads) == 1:
                entered.set()
                assert release.wait(3)
            return render(*args, **kwargs)

        monkeypatch.setattr(conversation_history, "_render_entry_requests", blocked_render)
        try:
            await resize_terminal(pilot, 80, 30)
            assert await asyncio.to_thread(entered.wait, 2)
            assert tuple(app.history.lines) == old_lines
            await resize_terminal(pilot, 60, 30)
            assert tuple(app.history.lines) == old_lines
        finally:
            release.set()
        await asyncio.wait_for(app.history.wait_for_theme_refresh(), 3)
        assert threads and all(thread != loop_thread for thread in threads)
        assert app.history._render_width == app.history.scrollable_content_region.width
        assert app.history._render_width < 80
        assert "history" in app.history.rendered_text


@pytest.mark.asyncio
async def test_historical_spawn_tool_click_preserves_parent_run_identity() -> None:
    app = _HistoryApp()

    async with app.run_test(size=(80, 12)) as pilot:
        app.history.append(
            TranscriptEntry(
                kind="tool",
                run_id="run_parent",
                tool=RuntimeToolCallActivity(
                    tool_call_id="call_spawn",
                    tool_name="SpawnAgent",
                    status="completed",
                    source_index=0,
                ),
            )
        )
        await app.history.wait_until_rendered()
        before_hover = next(
            segment for segment in app.history.render_line(0) if "SpawnAgent" in segment.text
        )
        assert before_hover.style is not None

        await pilot.hover(app.history, offset=(3, 0))
        await pilot.pause()
        hovered = next(
            segment for segment in app.history.render_line(0) if "SpawnAgent" in segment.text
        )
        assert hovered.style is not None

        await pilot.click(app.history, offset=(3, 0))

        assert app.navigation == [("run_parent", "call_spawn")]


@pytest.mark.asyncio
async def test_live_spawn_tool_click_preserves_parent_run_identity() -> None:
    app = _LiveToolApp()

    async with app.run_test(size=(80, 12)) as pilot:
        row = app.query_one(ToolActivityRow)
        assert row.has_class("clickable")
        await pilot.hover(row)
        await pilot.pause()
        row.render()
        await pilot.click(row)

        assert app.navigation == [("run_live_parent", "call_live_spawn")]


@pytest.mark.asyncio
async def test_tool_updates_are_scoped_to_the_parent_run(monkeypatch) -> None:
    app = _HistoryApp()
    requested = RuntimeToolCallActivity(
        tool_call_id="reused_call",
        tool_name="SpawnAgent",
        status="running",
        source_index=0,
    )

    async with app.run_test(size=(80, 12)):
        app.history.append(TranscriptEntry(kind="tool", run_id="run_old", tool=requested))
        app.history.append(TranscriptEntry(kind="tool", run_id="run_new", tool=requested))
        await app.history.wait_until_rendered()

        def reject_full_history_render() -> None:
            raise AssertionError("one tool update must not re-render the complete history")

        monkeypatch.setattr(app.history, "_render_entries", reject_full_history_render)

        matched = app.history.update_tools(
            (requested.model_copy(update={"status": "completed"}),),
            run_id="run_new",
        )

        assert matched == frozenset({"reused_call"})
        assert app.history.entries[0].tool == requested
        assert app.history.entries[1].tool is not None
        assert app.history.entries[1].tool.status == "completed"


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(316.4, "5m 16.4s"), (3_916.4, "1h 5m 16.4s"), (93_784.5, "1d 2h 3m 04.5s")],
)
def test_compaction_and_run_summaries_share_visual_structure_and_failure_semantics(
    seconds: float, expected: str
) -> None:
    compaction = _compaction_summary(
        CompactionPresentation(
            covered_message_count=8,
            tokens_reduced=1_200,
            duration_seconds=seconds,
        ),
        DEFAULT_TUI_THEME,
    )
    failed = _run_summary(
        RunPresentation(
            run_id="run_failed",
            status="failed",
            model="deepseek-v4-flash",
            duration_seconds=seconds,
            error="provider returned an empty assistant message",
        ),
        DEFAULT_TUI_THEME,
    )

    assert compaction.plain == f"▣ Context compacted • 8 messages • 1.2K tokens • {expected}"
    assert failed.plain == (
        f"▣ deepseek-v4-flash • {expected} • provider returned an empty assistant message"
    )
    assert not _style_at(compaction, "Context compacted").bold
    assert not _style_at(compaction, DEFAULT_TUI_THEME.brand_mark).bold
    assert not _style_at(failed, "deepseek-v4-flash").bold
    assert not _style_at(failed, DEFAULT_TUI_THEME.brand_mark).bold


def test_split_markdown_blocks_preserves_every_block() -> None:
    blocks = split_markdown_blocks(_MULTIBLOCK_ANSWER)
    joined = "".join(blocks)
    required = (
        "Here is the plan",
        "- step one",
        "## Details",
        "x = 1",
        "| col | val |",
        "> quoted text",
        "Setext Title",
        "Final paragraph.",
    )
    assert all(fragment in joined for fragment in required)


def test_split_markdown_blocks_of_blank_document_is_empty() -> None:
    assert split_markdown_blocks("") == ()
    assert split_markdown_blocks("\n\n  \n") == ()


def test_stream_keeps_trailing_block_live_until_next_block_starts() -> None:
    stream = AssistantMarkdownStream()

    blocks, tail = stream.feed("First paragraph")
    assert blocks == ()
    assert tail == "First paragraph"

    blocks, tail = stream.feed(" continues\n\nSecond")
    assert blocks == ("First paragraph continues\n\n",)
    assert tail == "Second"

    blocks, tail = stream.feed(" paragraph\n\nThird")
    assert blocks == ("Second paragraph\n\n",)
    assert tail == "Third"

    assert stream.close() == ("Third",)
    assert stream.source == ("First paragraph continues\n\nSecond paragraph\n\nThird")


def test_stream_does_not_freeze_paragraph_before_setext_underline_arrives() -> None:
    stream = AssistantMarkdownStream()

    # A paragraph followed only by its underline is still one trailing block;
    # freezing it early would lock in the wrong structure.
    blocks, _tail = stream.feed("Title\n")
    assert blocks == ()
    blocks, tail = stream.feed("=====\n")
    assert blocks == ()
    assert tail == "Title\n=====\n"

    blocks, _tail = stream.feed("\nBody text\n")
    assert blocks == ("Title\n=====\n\n",)


def test_stream_accumulates_fragmented_lines_without_rebuilding_source() -> None:
    stream = AssistantMarkdownStream()
    fragments = ("alpha", " beta\n", "\nsecond", " line\n", "third")
    blocks: list[str] = []

    for fragment in fragments:
        completed, _ = stream.append(fragment)
        blocks.extend(completed)

    assert stream.source == "".join(fragments)
    blocks.extend(stream.close())
    assert "".join(blocks) == "".join(fragments)


def test_long_open_block_stops_reparsing_after_bounded_prefix(monkeypatch) -> None:
    stream = AssistantMarkdownStream()
    parse_calls: list[int] = []
    real_block_starts = conversation_history_markdown_stream._block_starts

    def counting_block_starts(source: str) -> list[int]:
        parse_calls.append(len(source))
        return real_block_starts(source)

    monkeypatch.setattr(
        conversation_history_markdown_stream, "_block_starts", counting_block_starts
    )

    stream.append("```text\n")
    for _ in range(64):
        stream.append("x" * 1024 + "\n")

    assert parse_calls
    assert (
        max(parse_calls) <= conversation_history_markdown_stream._LIVE_MARKDOWN_BOUNDARY_MAX_CHARS
    )
    calls_before_close = len(parse_calls)
    blocks = stream.close()
    assert len(parse_calls) == calls_before_close + 1
    assert "".join(blocks) == stream.source

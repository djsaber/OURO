"""TUI application scenarios: streaming."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import ouro.tui.conversation.history_rendering as history_rendering
import ouro.tui.conversation.reasoning as tui_reasoning
import ouro.tui.conversation.tools as tui_widgets_tools
from ouro.core.events import EventRecord
from ouro.core.messages import MessageRecord
from ouro.tui.app import OuroTuiApp, _active_stream_tail
from ouro.tui.conversation.history import ConversationHistory
from ouro.tui.conversation.history_rendering import (
    AssistantTailLines,
)
from ouro.tui.conversation.turn import AssistantTurn
from ouro.tui.conversation.view import ConversationView
from ouro.tui.state import TuiSessionProjection
from ouro.tui.theme import DEFAULT_TUI_THEME
from tests.tui.app.support import _application
from tests.tui.helpers import run_ready_app
from tests.tui.helpers import wait_until as _wait_until


async def test_reasoning_freeze_and_reexpand_reuse_live_body(tmp_path, monkeypatch):
    app = OuroTuiApp(application=_application(tmp_path))
    async with run_ready_app(app, size=(100, 32)) as pilot:
        conversation = app.query_one(ConversationView)
        await conversation.set_reasoning_expanded(True)
        await conversation.append_reasoning("Thought 中文 with **literal** syntax. " * 1600)
        await pilot.pause()
        body = app.query_one(AssistantTurn).reasoning._content
        await body.wait_until_rendered()
        assert body._render_width > 0

        def unexpected_preparation(*args, **kwargs):
            raise AssertionError("completed reasoning must not be wrapped again")

        monkeypatch.setattr(history_rendering, "_prepare_reasoning_layout", unexpected_preparation)
        await conversation.finish_assistant()
        history = app.query_one(ConversationHistory)
        assert "Thought 中文" in history.rendered_text
        await history.toggle_reasoning(0)
        await history.toggle_reasoning(0)
        assert "Thought 中文" in history.rendered_text


@pytest.mark.asyncio
async def test_streaming_assistant_renders_completed_source_lines_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    rendered_source: list[str] = []
    real_render = history_rendering._render_stream_line

    def counting_render(line, **kwargs):
        if kwargs["plain"]:
            rendered_source.append(line)
        return real_render(line, **kwargs)

    monkeypatch.setattr(history_rendering, "_render_stream_line", counting_render)
    app = OuroTuiApp(application=_application(tmp_path))

    async with run_ready_app(app, size=(80, 24)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        await conversation.append_assistant("first")
        await conversation.append_assistant("\n")
        for _ in range(20):
            await pilot.pause(0.05)
            if "first" in rendered_source:
                break

        live_turn = conversation.query_one(AssistantTurn)
        completed_renders = rendered_source.count("first")
        assert completed_renders >= 1

        await conversation.append_assistant("second")
        await conversation.append_assistant(" grows")
        await pilot.pause()

        assert live_turn.tail.text == "first\nsecond grows"
        assert rendered_source.count("first") == completed_renders
        assert "first" in live_turn.tail.rendered_text
        assert "second grows" in live_turn.tail.rendered_text

        rendered_source.clear()
        fragments = ("x" * 1024,) * 32
        for fragment in fragments:
            await conversation.append_assistant(fragment)
        await pilot.pause()

        width = live_turn.tail.content_region.width
        assert width > 0
        assert rendered_source
        assert max(len(line) for line in rendered_source) <= width
        assert sum(len(line) for line in rendered_source) <= len("".join(fragments)) + (
            len(fragments) * width * 2
        )


@pytest.mark.asyncio
async def test_first_assistant_chunk_uses_the_mounted_live_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with run_ready_app(app, size=(80, 24)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        turn = conversation.query_one(AssistantTurn)
        assert not turn.display
        original_mount = conversation._transcript.mount
        mounted_sources: list[str] = []

        async def record_mount(*widgets, **kwargs):
            mounted_sources.extend(
                widget.source for widget in widgets if isinstance(widget, AssistantTurn)
            )
            return await original_mount(*widgets, **kwargs)

        monkeypatch.setattr(conversation._transcript, "mount", record_mount)

        await conversation.append_assistant("first ")

        assert mounted_sources == []
        assert conversation.query_one(AssistantTurn) is turn
        assert turn.source == "first "
        assert turn.display


@pytest.mark.asyncio
async def test_streaming_tail_skips_layout_when_visual_height_is_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 24)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        await conversation.append_assistant("prefix")
        tail = conversation.query_one(AssistantTailLines)
        await _wait_until(
            lambda: tail._render_width > 0 and bool(tail.rendered_text),
            pilot,
            description="initial assistant tail layout",
        )
        refresh_layouts: list[bool] = []

        def record_refresh(*_regions, **kwargs):
            refresh_layouts.append(bool(kwargs.get("layout")))
            return tail

        monkeypatch.setattr(tail, "refresh", record_refresh)
        await conversation.append_assistant(" suffix")

        assert refresh_layouts == [False]
        assert tail.text == "prefix suffix"


@pytest.mark.asyncio
async def test_reasoning_stream_renders_markup_syntax_as_plain_text(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        await app.query_one(ConversationView).set_reasoning_expanded(True)
        session_id = "ses_reasoning_markup"
        app._projection = TuiSessionProjection(session_id)
        fragments = (" [", "data", '-theme="')
        events = (
            EventRecord(
                session_id=session_id,
                run_id="run_reasoning_markup",
                event_type="run_started",
                sequence=1,
                payload={"trigger": "user"},
            ),
            *(
                EventRecord(
                    session_id=session_id,
                    run_id="run_reasoning_markup",
                    event_type="model_reasoning_delta",
                    sequence=sequence,
                    payload={"reasoning_content": fragment},
                )
                for sequence, fragment in enumerate(fragments, start=2)
            ),
        )

        await app._apply_runtime_events(events)
        await pilot.pause()

        expected = "".join(fragments)
        turn = app.query_one(AssistantTurn)
        reasoning = turn.reasoning
        assert reasoning.text == expected
        # Strip rendering lands a frame after the mount, so wait for it
        # rather than asserting against an empty buffer.
        for _ in range(10):
            if reasoning._content.rendered_text:
                break
            await pilot.pause(0.05)
        # The live body renders through the same │-prefixed definition the
        # immutable history freezes with, so markup stays literal text.
        assert reasoning._content.rendered_text == f"│ {expected}"
        assert reasoning.label.render().plain.startswith(
            f"{DEFAULT_TUI_THEME.expanded_disclosure_marker} Thinking"
            f"{DEFAULT_TUI_THEME.inline_separator}"
        )
        assert reasoning.label.render().plain.endswith("s")
        assert "Thinking" in reasoning.label.render_line(0).text
        assert reasoning.label.region.x == turn.content_region.x
        assert reasoning._content.region.x == reasoning.label.region.x
        assert reasoning._content.region.y == reasoning.label.region.bottom + 1
        assert not turn.query(".assistant-label")


def _reasoning_delta_events(
    session_id: str,
    run_id: str,
    fragments: tuple[str, ...],
) -> tuple[EventRecord, ...]:
    return (
        EventRecord(
            session_id=session_id,
            run_id=run_id,
            event_type="run_started",
            sequence=1,
            payload={"trigger": "user"},
        ),
        *(
            EventRecord(
                session_id=session_id,
                run_id=run_id,
                event_type="model_reasoning_delta",
                sequence=sequence,
                payload={"reasoning_content": fragment},
            )
            for sequence, fragment in enumerate(fragments, start=2)
        ),
    )


@pytest.mark.asyncio
async def test_reasoning_stream_renders_each_completed_line_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        await app.query_one(ConversationView).set_reasoning_expanded(True)
        session_id = "ses_reasoning_bounded_work"
        app._projection = TuiSessionProjection(session_id)
        fragments = tuple(f"thought {number}\n" for number in range(40))
        events = _reasoning_delta_events(session_id, "run_reasoning_bounded", fragments)

        rendered: list[str] = []
        original_render = history_rendering._render_stream_line

        def counting_render(line, **kwargs):
            if not kwargs["plain"]:
                rendered.append(line)
            return original_render(line, **kwargs)

        monkeypatch.setattr(history_rendering, "_render_stream_line", counting_render)
        # One batch per fragment exercises the append path fragment by
        # fragment, the way runtime event batches arrive while thinking.
        for event in events:
            await app._apply_runtime_events((event,))
        await pilot.pause()

        expected_lines = [f"thought {number}" for number in range(40)]
        body = app.query_one(AssistantTurn).reasoning._content
        for _ in range(10):
            if body.rendered_text:
                break
            await pilot.pause(0.05)
        # A completed line renders exactly once no matter how much more
        # thinking follows; quadratic re-rendering would total N*(N+1)/2.
        assert rendered == expected_lines
        assert body.text == "".join(f"{line}\n" for line in expected_lines)
        assert body.rendered_text.splitlines() == [f"│ {line}" for line in expected_lines]


@pytest.mark.asyncio
async def test_reasoning_stream_lines_match_whole_block_rendering(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        await app.query_one(ConversationView).set_reasoning_expanded(True)
        session_id = "ses_reasoning_parity"
        app._projection = TuiSessionProjection(session_id)
        long_line = " ".join(f"word{number}" for number in range(40))
        text = f'first thought\n\n{long_line}\nmarkup [data-theme=" stays literal\ntrailing partial'
        # Fragments split mid-word and mid-line so line completion and tail
        # growth are exercised separately from the source structure.
        fragments = tuple(text[index : index + 7] for index in range(0, len(text), 7))
        events = _reasoning_delta_events(session_id, "run_reasoning_parity", fragments)

        for event in events:
            await app._apply_runtime_events((event,))
        await pilot.pause()

        body = app.query_one(AssistantTurn).reasoning._content
        assert body.text == text
        for _ in range(10):
            if body.rendered_text:
                break
            await pilot.pause(0.05)
        # Incremental per-line rendering must equal one whole-block render at
        # the same width: that equality is what freezing later preserves.
        console = app.console
        options = console.options.update_width(body._render_width)
        from ouro.tui.text_wrapping import ProseText

        lines = console.render_lines(
            ProseText(text), options.update_width(body._render_width - 2), pad=False
        )
        whole_block = ["│ " + "".join(segment.text for segment in line).rstrip() for line in lines]
        whole_block = [line.rstrip() for line in whole_block]
        assert body.rendered_text.splitlines() == whole_block
        assert body.rendered_text.splitlines()[0] == "│ first thought"
        assert any("word39" in line for line in body.rendered_text.splitlines())
        for strip in (*body._lines, *body._pending_lines):
            for segment in strip:
                if segment.text.strip() and segment.text != "│":
                    assert segment.style is not None
                    assert not segment.style.dim


@pytest.mark.asyncio
async def test_live_surface_resets_round_content_and_reasoning_clock(
    tmp_path: Path, monkeypatch
) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    clock = [100.0]

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(tui_reasoning, "datetime", FixedDateTime)
    monkeypatch.setattr(tui_reasoning, "monotonic", lambda: clock[0])
    app = OuroTuiApp(application=_application(tmp_path))
    async with run_ready_app(app) as pilot:
        conversation = app.query_one(ConversationView)
        conversation.begin_model_request(run_id="previous_run")
        await conversation.append_reasoning(
            "Previous thought", started_at=now - timedelta(seconds=30)
        )
        await conversation.append_assistant("Previous answer")
        turn = conversation.query_one(AssistantTurn)
        assert turn.reasoning.duration_seconds >= 30

        await conversation.show_welcome()
        assert not turn.display
        assert not turn.reasoning.text
        assert not turn.tail.text
        assert turn.reasoning._timer is None

        conversation.begin_model_request(run_id="next_run")
        await conversation.append_reasoning("Next thought", started_at=now)
        await pilot.pause()
        assert conversation.query_one(AssistantTurn) is turn
        assert turn.run_id == "next_run"
        assert turn.reasoning.text == "Next thought"
        assert turn.reasoning.duration_seconds is None
        assert turn.reasoning._elapsed_seconds() == 0
        clock[0] += 2
        assert turn.reasoning._elapsed_seconds() == 2
        assert turn.reasoning._timer is not None
        await conversation.finish_assistant()
        assert turn.reasoning._timer is None


@pytest.mark.asyncio
async def test_streaming_tool_row_mounts_after_its_name_and_updates_in_place_on_start(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    session_id = "ses_streaming_tool"
    run_id = "run_streaming_tool"
    app._projection = TuiSessionProjection(session_id)

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        await app._apply_runtime_events(
            (
                EventRecord(
                    session_id=session_id,
                    run_id=run_id,
                    event_type="run_started",
                    sequence=1,
                ),
                EventRecord(
                    session_id=session_id,
                    run_id=run_id,
                    event_type="model_request_started",
                    sequence=2,
                    payload={"turn_id": "turn_streaming_tool"},
                ),
                EventRecord(
                    session_id=session_id,
                    run_id=run_id,
                    event_type="model_tool_call_delta",
                    sequence=3,
                    payload={
                        "turn_id": "turn_streaming_tool",
                        "index": 0,
                        "name_delta": "Write",
                    },
                ),
            )
        )
        row = conversation.query_one(tui_widgets_tools.ToolActivityRow)
        assert row.tool.status == "streaming"
        assert row.tool.argument_stream_tail is None
        assert "Write" in str(row.render())
        provisional_id = row.tool_call_id

        await app._apply_runtime_events(
            (
                EventRecord(
                    session_id=session_id,
                    run_id=run_id,
                    event_type="model_tool_call_delta",
                    sequence=4,
                    payload={
                        "turn_id": "turn_streaming_tool",
                        "index": 0,
                        "arguments_delta": '{"path":"out',
                    },
                ),
            )
        )
        assert conversation.query_one(tui_widgets_tools.ToolActivityRow) is row
        assert row.tool.status == "streaming"
        assert row.tool.argument_stream_tail == '{"path":"out'
        assert f"Write {DEFAULT_TUI_THEME.inline_separator_mark}" in str(row.render())

        await app._apply_runtime_events(
            (
                EventRecord(
                    session_id=session_id,
                    run_id=run_id,
                    event_type="model_message_completed",
                    sequence=5,
                    payload={"tool_call_count": 1},
                ),
                EventRecord(
                    session_id=session_id,
                    run_id=run_id,
                    event_type="tool_batch_planned",
                    sequence=6,
                    payload={"batch_id": "batch_streaming_tool"},
                ),
                EventRecord(
                    session_id=session_id,
                    run_id=run_id,
                    event_type="tool_call_requested",
                    sequence=7,
                    payload={
                        "tool_call_id": "call_streaming_tool",
                        "tool_name": "Write",
                        "source_index": 0,
                        "arguments": {"path": "out.txt", "content": "hello"},
                    },
                ),
                EventRecord(
                    session_id=session_id,
                    run_id=run_id,
                    event_type="tool_call_started",
                    sequence=8,
                    payload={
                        "tool_call_id": "call_streaming_tool",
                        "tool_name": "Write",
                        "source_index": 0,
                    },
                ),
            )
        )

        assert conversation.query_one(tui_widgets_tools.ToolActivityRow) is row
        assert len(conversation.query(".tool-row")) == 1
        assert provisional_id not in conversation._tools
        assert conversation._tools["call_streaming_tool"] is row
        assert row.tool.argument_stream_tail is None
        assert "out.txt" in str(row.render())
        assert '{"path":"out' not in str(row.render())


def test_active_stream_tail_waits_for_canonical_message_commit() -> None:
    session_id = "ses_restore_boundary"
    run_id = "run_restore_boundary"
    events = (
        EventRecord(
            session_id=session_id,
            run_id=run_id,
            turn_id="turn_committed",
            event_type="model_text_delta",
            sequence=1,
            payload={"text": "already stored"},
        ),
        EventRecord(
            session_id=session_id,
            run_id=run_id,
            turn_id="turn_committed",
            event_type="model_message_completed",
            sequence=2,
        ),
        EventRecord(
            session_id=session_id,
            run_id=run_id,
            turn_id="turn_pending",
            event_type="model_reasoning_delta",
            sequence=3,
            payload={"reasoning_content": "still thinking"},
        ),
        EventRecord(
            session_id=session_id,
            run_id=run_id,
            turn_id="turn_pending",
            event_type="model_text_delta",
            sequence=4,
            payload={"text": "not committed yet"},
        ),
        EventRecord(
            session_id=session_id,
            run_id=run_id,
            turn_id="turn_pending",
            event_type="model_message_completed",
            sequence=5,
        ),
    )
    committed = MessageRecord.text(
        session_id=session_id,
        run_id=run_id,
        turn_id="turn_committed",
        role="assistant",
        text="already stored",
        sequence=1,
    )

    assert _active_stream_tail(events, (committed,)) == (
        "still thinking",
        "not committed yet",
    )

    pending = MessageRecord.text(
        session_id=session_id,
        run_id=run_id,
        turn_id="turn_pending",
        role="assistant",
        text="not committed yet",
        sequence=2,
    )
    assert _active_stream_tail(events, (committed, pending)) == ("", "")

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from rich.console import Console
from textual.app import App, ComposeResult

from ouro.application.supervisor_models import RuntimeToolCallActivity
from ouro.tui.conversation_history import ConversationHistory, TranscriptEntry
from ouro.tui.theme import DEFAULT_TUI_THEME
from ouro.tui.tool_presentations import present_tool
from ouro.tui.widgets import ToolActivityRow, _tool_activity_text


def _tool(
    name: str,
    *,
    arguments: dict[str, object] | None = None,
    result: dict[str, object] | None = None,
) -> RuntimeToolCallActivity:
    return RuntimeToolCallActivity.model_validate(
        {
            "tool_call_id": "call_1",
            "tool_name": name,
            "status": "completed",
            "source_index": 0,
            "arguments": arguments or {},
            "result_data": result or {},
        }
    )


def test_file_tool_presentations_expose_target_and_line_semantics() -> None:
    read = present_tool(_tool("Read", arguments={"path": "src/app.py"}, result={"line_count": 42}))
    write = present_tool(
        _tool(
            "Write",
            arguments={"path": "src/new.py", "content": "one\ntwo\n", "mode": "append"},
        )
    )
    edit = present_tool(
        _tool(
            "Edit",
            arguments={
                "path": "src/edit.py",
                "old": "old one\nold two",
                "new": "new one\nnew two\nnew three",
            },
        )
    )

    assert [(line.marker, line.text) for line in read.lines] == [
        ("└", "src/app.py · 42 lines")
    ]
    assert [(line.marker, line.text) for line in write.lines] == [
        ("└", "src/new.py · +2 lines · append")
    ]
    assert [(part.text, part.tone) for part in write.lines[0].parts] == [
        ("src/new.py", "default"),
        (" · ", "muted"),
        ("+2", "added"),
        (" lines", "muted"),
        (" · ", "muted"),
        ("append", "muted"),
    ]
    assert [(line.marker, line.text) for line in edit.lines] == [
        ("└", "src/edit.py · +3 −2 lines")
    ]
    assert [(part.text, part.tone) for part in edit.lines[0].parts] == [
        ("src/edit.py", "default"),
        (" · ", "muted"),
        ("+3", "added"),
        (" ", "muted"),
        ("−2", "removed"),
        (" lines", "muted"),
    ]


def test_command_presentation_flattens_and_truncates_long_commands() -> None:
    presentation = present_tool(
        _tool("PowerShell", arguments={"command": "Get-ChildItem\n" + "x" * 180})
    )

    assert len(presentation.lines) == 1
    assert presentation.lines[0].text.startswith("Get-ChildItem ↵ ")
    assert presentation.lines[0].text.endswith("…")
    assert len(presentation.lines[0].text) <= 120


def test_todolist_presentation_exposes_progress_and_item_states() -> None:
    presentation = present_tool(
        _tool(
            "TodoList",
            arguments={
                "todos": [
                    {"title": "Inspect architecture", "status": "done"},
                    {"title": "Implement lifecycle", "status": "in_progress"},
                    {"title": "Run tests", "status": "pending"},
                ]
            },
        )
    )

    assert presentation.summary == "1/3 done"
    assert [(line.marker, line.state) for line in presentation.lines] == [
        ("●", "done"),
        ("◉", "active"),
        ("○", "pending"),
    ]


def test_edit_line_counts_use_added_and_removed_semantic_colors() -> None:
    rendered = _tool_activity_text(
        _tool(
            "Edit",
            arguments={"path": "src/app.py", "old": "old\ntext", "new": "new\ntext\nhere"},
        ),
        frame=0,
        theme=DEFAULT_TUI_THEME,
    )
    console = Console()

    added = rendered.get_style_at_offset(console, rendered.plain.index("+3"))
    removed = rendered.get_style_at_offset(console, rendered.plain.index("−2"))

    assert added.color is not None
    assert removed.color is not None
    assert tuple(added.color.get_truecolor()) == (119, 176, 131)
    assert tuple(removed.color.get_truecolor()) == (216, 117, 117)


class _ToolRowApp(App[None]):
    def __init__(self, tool: RuntimeToolCallActivity) -> None:
        super().__init__()
        self.tool = tool

    def compose(self) -> ComposeResult:
        yield ToolActivityRow(self.tool)


class _HistoryApp(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.history = ConversationHistory(_tool_activity_text)

    def compose(self) -> ComposeResult:
        yield self.history


@pytest.mark.asyncio
async def test_tool_groups_only_add_spacing_at_non_tool_boundaries() -> None:
    first = _tool("Read", arguments={"path": "first.py"}, result={"line_count": 1})
    second = _tool("Read", arguments={"path": "second.py"}, result={"line_count": 2}).model_copy(
        update={"tool_call_id": "call_2"}
    )
    app = _HistoryApp()

    async with app.run_test(size=(100, 30)):
        app.history.append(TranscriptEntry(kind="assistant", text="Before."))
        app.history.append(TranscriptEntry(kind="tool", tool=first))
        app.history.append(TranscriptEntry(kind="tool", tool=second))
        app.history.append(TranscriptEntry(kind="assistant", text="After."))
        await app.history.wait_until_rendered()

        lines = [line.strip() for line in app.history.rendered_text.splitlines()]
        before = lines.index("Before.")
        first_header = next(index for index, line in enumerate(lines) if "✓ Read" in line)
        first_detail = lines.index("└ first.py · 1 line")
        second_header = next(
            index
            for index, line in enumerate(lines[first_header + 1 :], first_header + 1)
            if "✓ Read" in line
        )
        second_detail = lines.index("└ second.py · 2 lines")
        after = lines.index("After.")

        assert lines[before + 1 : first_header] == [""]
        assert lines[first_detail + 1 : second_header] == []
        assert lines[second_detail + 1 : after] == [""]


@pytest.mark.asyncio
async def test_reasoning_only_turn_adds_one_message_boundary_before_tool() -> None:
    tool = _tool("Read", arguments={"path": "after-thought.py"})
    app = _HistoryApp()

    async with app.run_test(size=(100, 30)):
        app.history.set_reasoning_expanded(False, above_line=0)
        app.history.append(TranscriptEntry(kind="assistant", reasoning="Inspect the target first."))
        app.history.append(TranscriptEntry(kind="tool", tool=tool))
        await app.history.wait_until_rendered()

        lines = [line.strip() for line in app.history.rendered_text.splitlines()]
        reasoning = lines.index("THOUGHT")
        tool_header = next(index for index, line in enumerate(lines) if "✓ Read" in line)

        assert lines[reasoning + 1 : tool_header] == [""]


@pytest.mark.asyncio
async def test_tool_row_animates_active_state_and_freezes_completed_elapsed_seconds() -> None:
    started_at = datetime.now(UTC) - timedelta(seconds=2)
    running = RuntimeToolCallActivity(
        tool_call_id="call_1",
        tool_name="Read",
        status="running",
        source_index=0,
        arguments={"path": "README.md"},
        started_at=started_at,
    )
    app = _ToolRowApp(running)

    async with app.run_test() as pilot:
        row = app.query_one(ToolActivityRow)
        first = row.render().plain
        await pilot.pause(0.25)
        second = row.render().plain

        assert first != second
        assert "Read" in second
        assert "└ README.md" in second
        assert "running" in second
        assert "s" in second

        row.apply_tool(
            running.model_copy(
                update={
                    "status": "completed",
                    "result_data": {"line_count": 7},
                    "ended_at": started_at + timedelta(seconds=3),
                }
            )
        )

        completed = row.render().plain
        assert "✓ Read  completed · 3s\n    └ README.md · 7 lines" in completed

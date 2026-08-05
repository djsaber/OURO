from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from textual.app import App, ComposeResult

from ouro.application.supervisor_models import RuntimeToolCallActivity
from ouro.tui.tool_presentations import present_tool
from ouro.tui.widgets import ToolActivityRow


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

    assert (read.subject, read.summary) == ("src/app.py", "42 lines")
    assert (write.subject, write.summary) == ("src/new.py", "2 lines · append")
    assert (edit.subject, edit.summary) == ("src/edit.py", "+3 −2 lines")


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
        ("✓", "done"),
        ("◉", "active"),
        ("○", "pending"),
    ]


class _ToolRowApp(App[None]):
    def __init__(self, tool: RuntimeToolCallActivity) -> None:
        super().__init__()
        self.tool = tool

    def compose(self) -> ComposeResult:
        yield ToolActivityRow(self.tool)


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
        assert "Read  README.md" in second
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
        assert "✓ Read  README.md  ·  7 lines  completed · 3s" in completed

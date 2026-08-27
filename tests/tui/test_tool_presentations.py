from __future__ import annotations

import pytest
from rich.console import Console
from textual.app import App, ComposeResult

from ouro.application.agent_models import AgentTreeNode
from ouro.application.supervisor_models import (
    RuntimeActivitySnapshot,
    RuntimeToolCallActivity,
)
from ouro.tui.conversation_history import ConversationHistory, TranscriptEntry
from ouro.tui.theme import DEFAULT_TUI_THEME, TUI_DESIGN
from ouro.tui.tool_presentations import present_tool
from ouro.tui.widgets import _tool_activity_text


def _tool(
    name: str,
    *,
    arguments: dict[str, object] | None = None,
    result: dict[str, object] | None = None,
    status: str = "completed",
) -> RuntimeToolCallActivity:
    return RuntimeToolCallActivity.model_validate(
        {
            "tool_call_id": "call_1",
            "tool_name": name,
            "status": status,
            "source_index": 0,
            "arguments": arguments or {},
            "result_data": result or {},
        }
    )


def _agent(
    activity: RuntimeActivitySnapshot,
    *,
    nickname: str | None = "Scout",
) -> AgentTreeNode:
    return AgentTreeNode(
        revision=1,
        delegation_id="dlg_1",
        root_session_id="ses_root",
        session_id="ses_child",
        parent_session_id="ses_root",
        parent_run_id="run_root",
        parent_tool_call_id="call_1",
        agent_id="general",
        agent_name="General Agent",
        nickname=nickname,
        task_description="Inspect the event pipeline",
        status="running",
        execution_mode="async",
        lifecycle_binding="detached",
        control_state="open",
        depth=1,
        activity=activity,
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

    assert [(line.marker, line.text) for line in read.lines] == [("└", "src/app.py · 42 lines")]
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
    assert [(line.marker, line.text) for line in edit.lines] == [("└", "src/edit.py · +3 −2 lines")]
    assert [(part.text, part.tone) for part in edit.lines[0].parts] == [
        ("src/edit.py", "default"),
        (" · ", "muted"),
        ("+3", "added"),
        (" ", "muted"),
        ("−2", "removed"),
        (" lines", "muted"),
    ]


def test_command_presentation_keeps_full_flattened_command_for_layout_time() -> None:
    command = "Get-ChildItem\n" + "x" * 180
    presentation = present_tool(_tool("PowerShell", arguments={"command": command}))

    assert len(presentation.lines) == 1
    assert presentation.lines[0].text == "Get-ChildItem ↵ " + "x" * 180

    rendered = _tool_activity_text(
        _tool("PowerShell", arguments={"command": command}),
        frame=0,
        theme=DEFAULT_TUI_THEME,
    )
    command_offset = rendered.plain.index("Get-ChildItem")
    command_style = rendered.get_style_at_offset(Console(), command_offset)
    assert rendered.no_wrap is True
    assert rendered.overflow == "ellipsis"
    assert command_style.color is not None
    assert command_style.color.triplet is not None
    assert command_style.color.triplet.hex == TUI_DESIGN.text_faint


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


def test_spawn_agent_uses_agent_identity_and_places_task_below_status() -> None:
    tool = _tool(
        "SpawnAgent",
        arguments={
            "agent_id": "general",
            "nickname": "Fallback nickname",
            "description": "Fallback description",
        },
        status="waiting_child",
    )
    activity = RuntimeActivitySnapshot(
        session_id="ses_child",
        run_id="run_child",
        status="running",
        activity="streaming_text",
    )
    presentation = present_tool(tool)

    assert presentation.title == "Fallback nickname"
    assert (
        "SpawnAgent"
        not in _tool_activity_text(
            tool,
            frame=0,
            theme=DEFAULT_TUI_THEME,
        ).plain
    )

    rendered_text = _tool_activity_text(
        tool,
        frame=0,
        theme=DEFAULT_TUI_THEME,
        agent=_agent(activity),
    )
    rendered = rendered_text.plain

    assert "SpawnAgent" not in rendered
    assert f"General Agent{TUI_DESIGN.inline_separator}Scout" in rendered.splitlines()[0]
    assert "working" in rendered.splitlines()[0]
    assert "waiting for subagent" not in rendered
    assert "Inspect the event pipeline" not in rendered.splitlines()[0]
    assert "└ Responding · Inspect the event pipeline" in rendered
    detail_offset = rendered.index("└ Responding")
    detail_style = rendered_text.get_style_at_offset(Console(), detail_offset)
    assert detail_style.color is not None
    assert detail_style.color.triplet is not None
    assert detail_style.color.triplet.hex == TUI_DESIGN.text_lowlight


def test_spawn_agent_uses_existing_child_tool_activity_status() -> None:
    activity = RuntimeActivitySnapshot(
        session_id="ses_child",
        run_id="run_child",
        status="running",
        activity="executing_tools",
    )

    rendered = _tool_activity_text(
        _tool("SpawnAgent", status="waiting_child"),
        frame=0,
        theme=DEFAULT_TUI_THEME,
        agent=_agent(activity, nickname=None),
    ).plain

    assert "General Agent" in rendered.splitlines()[0]
    assert "Inspect the event pipeline" not in rendered.splitlines()[0]
    assert "└ Running tools · Inspect the event pipeline" in rendered


@pytest.mark.parametrize(
    ("agent_status", "expected"),
    (("completed", "done"), ("failed", "failed"), ("aborted", "failed")),
)
def test_spawn_agent_uses_task_outcome_status(
    agent_status: str,
    expected: str,
) -> None:
    activity = RuntimeActivitySnapshot(
        session_id="ses_child",
        run_id="run_child",
        status="idle",
        activity="idle",
    )
    agent = _agent(activity).model_copy(update={"status": agent_status, "activity": None})

    rendered = _tool_activity_text(
        _tool("SpawnAgent", status="completed"),
        frame=0,
        theme=DEFAULT_TUI_THEME,
        agent=agent,
    ).plain
    header, detail = rendered.splitlines()

    assert expected in header
    assert "completed" not in rendered.casefold()
    assert f"└ {expected.title()} · Inspect the event pipeline" == detail.strip()


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
async def test_subagent_status_replaces_only_its_indexed_history_piece() -> None:
    app = _HistoryApp()
    tool = _tool("SpawnAgent", status="waiting_child")

    async with app.run_test(size=(100, 30)):
        app.history.append(TranscriptEntry(kind="assistant", text="Unrelated history."))
        app.history.append(TranscriptEntry(kind="tool", run_id="run_root", tool=tool))
        app.history.append(TranscriptEntry(kind="assistant", text="Later history."))
        await app.history.wait_until_rendered()
        line_buffer = app.history.lines
        unaffected_line = line_buffer[0]

        first = RuntimeActivitySnapshot(
            session_id="ses_child",
            run_id="run_child",
            status="running",
            activity="streaming_text",
        )
        app.history.update_agents((_agent(first),))

        assert app.history.lines is line_buffer
        assert app.history.lines[0] is unaffected_line
        assert "└ Responding · Inspect the event pipeline" in app.history.rendered_text

        second = first.model_copy(update={"activity": "streaming_reasoning"})
        app.history.update_agents((_agent(second),))

        assert app.history.lines is line_buffer
        assert app.history.lines[0] is unaffected_line
        assert "└ Thinking · Inspect the event pipeline" in app.history.rendered_text


@pytest.mark.asyncio
async def test_tool_detail_uses_available_width_without_wrapping() -> None:
    tool = _tool(
        "PowerShell",
        arguments={"command": "Get-ChildItem " + "long-segment-" * 20},
    )
    app = _HistoryApp()

    async with app.run_test(size=(44, 12)) as pilot:
        app.history.append(TranscriptEntry(kind="tool", tool=tool))
        await app.history.wait_until_rendered()

        narrow_lines = [line.strip() for line in app.history.rendered_text.splitlines()]
        narrow_detail = next(line for line in narrow_lines if line.startswith("└ Get-ChildItem"))
        assert narrow_detail.endswith("…")
        assert len([line for line in narrow_lines if "long-segment" in line]) == 1

        await pilot.resize_terminal(80, 12)
        await pilot.pause()
        wide_lines = [line.strip() for line in app.history.rendered_text.splitlines()]
        wide_detail = next(line for line in wide_lines if line.startswith("└ Get-ChildItem"))
        assert wide_detail.endswith("…")
        assert len(wide_detail) > len(narrow_detail)


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
        reasoning = lines.index("+ Thought")
        tool_header = next(index for index, line in enumerate(lines) if "✓ Read" in line)

        assert lines[reasoning + 1 : tool_header] == [""]

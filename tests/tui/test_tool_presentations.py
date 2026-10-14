from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from rich.cells import cell_len
from rich.console import Console
from textual.app import App, ComposeResult

from ouro.application.agent_models import AgentTreeNode
from ouro.application.supervisor_models import RuntimeActivitySnapshot, RuntimeToolCallActivity
from ouro.tui.conversation_history import ConversationHistory, TranscriptEntry
from ouro.tui.theme import DEFAULT_TUI_THEME
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
    execution_mode: str = "async",
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
        execution_mode=execution_mode,
        lifecycle_binding="detached",
        control_state="open",
        depth=1,
        activity=activity,
    )


@pytest.mark.parametrize(
    "result,expected",
    [({}, "screens/ui.png"), ({"width": 2048, "height": 1024}, "screens/ui.png • 2048 x 1024")],
)
def test_image_tool_presentation_shows_path_and_loaded_resolution(result, expected):
    presentation = present_tool(
        _tool("ViewImage", arguments={"path": "screens/ui.png"}, result=result)
    )
    assert [line.text for line in presentation.lines] == [expected]


def test_file_tool_presentations_expose_target_and_line_semantics() -> None:
    read = present_tool(
        _tool(
            "Read",
            arguments={"path": "src/app.py", "offset": 120},
            result={"line_count": 51},
        )
    )
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
        ("└", "src/app.py • 120 - 170 lines")
    ]
    assert [(line.marker, line.text) for line in write.lines] == [
        ("└", "src/new.py • +2 lines • append")
    ]
    assert [(part.text, part.tone) for part in write.lines[0].parts] == [
        ("src/new.py", "default"),
        (" • ", "muted"),
        ("+2", "added"),
        (" lines", "muted"),
        (" • ", "muted"),
        ("append", "muted"),
    ]
    assert [(line.marker, line.text) for line in edit.lines] == [("└", "src/edit.py • +3 −2 lines")]
    assert [(part.text, part.tone) for part in edit.lines[0].parts] == [
        ("src/edit.py", "default"),
        (" • ", "muted"),
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
    assert command_style.color.triplet.hex == DEFAULT_TUI_THEME.text_faint


def test_todolist_presentation_exposes_progress_and_item_states() -> None:
    tool = _tool(
        "TodoList",
        arguments={
            "todos": [
                {"title": "Inspect architecture", "status": "done"},
                {"title": "Implement lifecycle", "status": "in_progress"},
                {"title": "Run tests", "status": "pending"},
            ]
        },
    )
    presentation = present_tool(tool)

    assert presentation.summary == "1/3 done"
    assert [(line.marker, line.state) for line in presentation.lines] == [
        ("●", "done"),
        ("◉", "active"),
        ("○", "pending"),
    ]

    rendered = _tool_activity_text(tool, frame=0, theme=DEFAULT_TUI_THEME)
    for marker in ("●", "◉"):
        marker_style = rendered.get_style_at_offset(Console(), rendered.plain.index(marker))
        assert marker_style.color is not None
        assert marker_style.color.triplet is not None
        assert marker_style.color.triplet.hex == DEFAULT_TUI_THEME.accent

    empty = present_tool(_tool("TodoList", arguments={"todos": []}))
    assert empty.summary == "empty"
    assert not empty.lines
    missing = present_tool(_tool("TodoList", arguments={}))
    assert missing.summary is None
    assert not missing.lines


def test_spawn_agent_uses_agent_identity_and_places_task_below_header() -> None:
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
    assert rendered.splitlines()[0] == (
        f"{DEFAULT_TUI_THEME.spinner_frames[0]} "
        f"General Agent{DEFAULT_TUI_THEME.inline_separator}Scout  background"
    )
    assert "Inspect the event pipeline" not in rendered.splitlines()[0]
    assert "└ Responding • Inspect the event pipeline" in rendered
    detail_offset = rendered.index("└ Responding")
    detail_style = rendered_text.get_style_at_offset(Console(), detail_offset)
    assert detail_style.color is not None
    assert detail_style.color.triplet is not None
    assert detail_style.color.triplet.hex == DEFAULT_TUI_THEME.text_lowlight


@pytest.mark.parametrize(
    ("execution_mode", "expected_status"),
    (("sync", "working"), ("async", "background")),
)
def test_spawn_agent_first_frame_uses_request_for_complete_preview(
    execution_mode: str,
    expected_status: str,
) -> None:
    tool = _tool(
        "SpawnAgent",
        arguments={
            "agent_id": "general",
            "description": "Inspect the event pipeline",
            "execution_mode": execution_mode,
        },
        status="waiting_child",
    )

    initial = _tool_activity_text(
        tool,
        frame=0,
        theme=DEFAULT_TUI_THEME,
    ).plain.splitlines()
    updated = _tool_activity_text(
        tool,
        frame=0,
        theme=DEFAULT_TUI_THEME,
        agent=_agent(
            RuntimeActivitySnapshot(
                session_id="ses_child",
                run_id="run_child",
                status="queued",
                activity="queued",
            ),
            nickname=None,
            execution_mode=execution_mode,
        ),
    ).plain.splitlines()

    assert initial == [
        f"{DEFAULT_TUI_THEME.spinner_frames[0]} general  {expected_status}",
        f"  └ Dispatching{DEFAULT_TUI_THEME.inline_separator}Inspect the event pipeline",
    ]
    assert len(updated) == len(initial)
    assert updated[0].endswith(f"General Agent  {expected_status}")
    assert updated[1] == initial[1]


@pytest.mark.parametrize(
    ("status", "expected_color"),
    (
        ("completed", DEFAULT_TUI_THEME.text_lowlight),
        ("failed", DEFAULT_TUI_THEME.failure),
        ("rejected", DEFAULT_TUI_THEME.failure),
        ("aborted", DEFAULT_TUI_THEME.failure),
    ),
)
def test_tool_header_omits_status_chrome_and_only_colors_failures(
    status: str,
    expected_color: str,
) -> None:
    rendered = _tool_activity_text(
        _tool("Read", status=status),
        frame=0,
        theme=DEFAULT_TUI_THEME,
    )
    header = rendered.plain.splitlines()[0]

    assert header.strip() == "Read"
    style = rendered.get_style_at_offset(Console(), rendered.plain.index("Read"))
    assert style.color is not None
    assert style.color.triplet is not None
    assert style.color.triplet.hex == expected_color
    assert not style.bold


def test_active_tool_retains_spinner_without_a_right_status_label() -> None:
    tool = _tool("Read", status="running")

    first = _tool_activity_text(
        tool,
        frame=0,
        theme=DEFAULT_TUI_THEME,
    ).plain
    second = _tool_activity_text(
        tool,
        frame=1,
        theme=DEFAULT_TUI_THEME,
    ).plain

    assert first == f"{DEFAULT_TUI_THEME.spinner_frames[0]} Read"
    assert second == f"{DEFAULT_TUI_THEME.spinner_frames[1]} Read"


def test_streaming_tool_uses_lowlight_left_filled_window_without_execution_details() -> None:
    preview = 'prefix:{"content":"实时参数流"}'
    tool = _tool(
        "Write",
        arguments={"path": "should-not-render.txt", "content": "hidden"},
        status="streaming",
    ).model_copy(update={"argument_stream_tail": preview})

    rendered_text = _tool_activity_text(
        tool,
        frame=9,
        theme=DEFAULT_TUI_THEME,
    )
    rendered = rendered_text.plain
    prefix = (
        f"{DEFAULT_TUI_THEME.spinner_frames[9]} Write {DEFAULT_TUI_THEME.inline_separator_mark} "
    )
    window = rendered.removeprefix(prefix)

    assert rendered.startswith(prefix)
    assert window.startswith(preview)
    assert window.rstrip() == preview
    assert len(window) > len(preview)
    assert "should-not-render.txt" not in rendered
    assert "\n" not in rendered
    assert cell_len(window) == 48
    tool_style = rendered_text.get_style_at_offset(Console(), rendered.index("Write"))
    preview_style = rendered_text.get_style_at_offset(Console(), rendered.index(preview))
    assert preview_style.color is not None
    assert preview_style.color.get_truecolor().hex == DEFAULT_TUI_THEME.text_faint
    assert tool_style.color is not None
    assert tool_style.color.triplet is not None
    assert tool_style.color.triplet.hex == DEFAULT_TUI_THEME.text_lowlight


def test_streaming_tool_name_is_active_before_arguments_arrive() -> None:
    rendered = _tool_activity_text(
        _tool("Write", status="streaming"),
        frame=2,
        theme=DEFAULT_TUI_THEME,
    ).plain

    assert rendered == f"{DEFAULT_TUI_THEME.spinner_frames[2]} Write"


def test_streaming_tool_window_keeps_the_latest_fixed_width_tail() -> None:
    tool = _tool("Write", status="streaming").model_copy(
        update={"argument_stream_tail": f"discard-{'x' * 48}"}
    )
    overflow_window = _tool_activity_text(
        tool,
        frame=0,
        theme=DEFAULT_TUI_THEME,
    ).plain.removeprefix(
        f"{DEFAULT_TUI_THEME.spinner_frames[0]} Write {DEFAULT_TUI_THEME.inline_separator_mark} "
    )
    assert overflow_window == "x" * 48


@pytest.mark.parametrize("tool_name", ["Read", "SpawnAgent"])
@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(304.2, "5m 04.2s"), (3_904.2, "1h 5m 04.2s"), (93_784.5, "1d 2h 3m 04.5s")],
)
def test_tool_elapsed_time_omits_zero_and_uses_shared_duration_format(
    tool_name: str, seconds: float, expected: str
) -> None:
    started_at = datetime(2026, 8, 28, tzinfo=UTC)
    zero = _tool("Read").model_copy(update={"started_at": started_at, "ended_at": started_at})
    submillisecond = _tool("Read").model_copy(
        update={
            "started_at": started_at,
            "ended_at": started_at + timedelta(microseconds=1),
        }
    )
    elapsed = _tool(tool_name).model_copy(
        update={
            "started_at": started_at,
            "ended_at": started_at + timedelta(seconds=seconds),
        }
    )

    zero_text = _tool_activity_text(
        zero,
        frame=0,
        theme=DEFAULT_TUI_THEME,
    ).plain
    submillisecond_text = _tool_activity_text(
        submillisecond,
        frame=0,
        theme=DEFAULT_TUI_THEME,
    ).plain
    elapsed_rendered = _tool_activity_text(
        elapsed,
        frame=0,
        theme=DEFAULT_TUI_THEME,
    )
    elapsed_text = elapsed_rendered.plain

    assert "0s" not in zero_text
    assert "0ms" not in submillisecond_text
    assert expected in elapsed_text
    elapsed_style = elapsed_rendered.get_style_at_offset(Console(), elapsed_text.index(expected))
    assert not elapsed_style.bold


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
    assert "└ Running tools • Inspect the event pipeline" in rendered


def test_spawn_agent_uses_dispatching_as_its_initial_detail_status() -> None:
    activity = RuntimeActivitySnapshot(
        session_id="ses_child",
        run_id="run_child",
        status="queued",
        activity="queued",
    )
    agent = _agent(activity).model_copy(update={"status": "queued", "activity": None})

    rendered = _tool_activity_text(
        _tool("SpawnAgent", status="waiting_child"),
        frame=0,
        theme=DEFAULT_TUI_THEME,
        agent=agent,
    ).plain

    assert rendered.splitlines()[0].endswith(
        f"General Agent{DEFAULT_TUI_THEME.inline_separator}Scout  background"
    )
    assert "└ Dispatching • Inspect the event pipeline" in rendered


def test_spawn_agent_distinguishes_foreground_work_from_background_work() -> None:
    activity = RuntimeActivitySnapshot(
        session_id="ses_child",
        run_id="run_child",
        status="running",
        activity="streaming_text",
    )

    rendered = _tool_activity_text(
        _tool("SpawnAgent", status="waiting_child"),
        frame=0,
        theme=DEFAULT_TUI_THEME,
        agent=_agent(activity, execution_mode="sync"),
    ).plain

    assert rendered.splitlines()[0].endswith(
        f"General Agent{DEFAULT_TUI_THEME.inline_separator}Scout  working"
    )


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

    rendered_text = _tool_activity_text(
        _tool("SpawnAgent", status="completed"),
        frame=0,
        theme=DEFAULT_TUI_THEME,
        agent=agent,
    )
    rendered = rendered_text.plain
    header, detail = rendered.splitlines()

    assert header.strip() == (f"General Agent{DEFAULT_TUI_THEME.inline_separator}Scout  {expected}")
    assert f"└ {expected.title()} • Inspect the event pipeline" == detail.strip()
    title_style = rendered_text.get_style_at_offset(Console(), rendered.index("General Agent"))
    assert title_style.color is not None
    assert title_style.color.triplet is not None
    assert title_style.color.triplet.hex == (
        DEFAULT_TUI_THEME.failure if expected == "failed" else DEFAULT_TUI_THEME.text_lowlight
    )
    assert not title_style.bold


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

        raw_lines = app.history.rendered_text.splitlines()
        lines = [line.strip() for line in raw_lines]
        before = lines.index("Before.")
        first_header = next(index for index, line in enumerate(lines) if line == "Read")
        first_detail = lines.index("└ first.py • 1 - 1 lines")
        second_header = next(
            index
            for index, line in enumerate(lines[first_header + 1 :], first_header + 1)
            if line == "Read"
        )
        second_detail = lines.index("└ second.py • 1 - 2 lines")
        after = lines.index("After.")

        assert raw_lines[first_header].startswith("  Read")
        assert not raw_lines[first_header].startswith("    Read")
        assert raw_lines[first_detail].startswith("    └")
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
        assert "└ Responding • Inspect the event pipeline" in app.history.rendered_text

        second = first.model_copy(update={"activity": "streaming_reasoning"})
        app.history.update_agents((_agent(second),))

        assert app.history.lines is line_buffer
        assert app.history.lines[0] is unaffected_line
        assert "└ Thinking • Inspect the event pipeline" in app.history.rendered_text


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

        narrow_raw_lines = app.history.rendered_text.splitlines()
        narrow_lines = [line.strip() for line in narrow_raw_lines]
        narrow_raw_detail = next(
            line for line in narrow_raw_lines if line.strip().startswith("└ Get-ChildItem")
        )
        narrow_detail = next(line for line in narrow_lines if line.startswith("└ Get-ChildItem"))
        assert narrow_detail.endswith("…")
        assert len(narrow_raw_detail) == app.history.scrollable_content_region.width - 2
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
        tool_header = next(index for index, line in enumerate(lines) if line == "Read")

        assert lines[reasoning + 1 : tool_header] == [""]

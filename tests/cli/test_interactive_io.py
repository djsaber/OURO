from __future__ import annotations

from ouro.cli.interactive_io import InteractiveOutput
from ouro.cli.terminal_status import SPINNER_THEMES
from ouro.core.events import EventRecord
from ouro.mcp import MCPServerConfig, MCPServerRecord


def _event(
    sequence: int,
    event_type: str,
    payload: dict[str, object],
) -> EventRecord:
    return EventRecord(
        session_id="ses_1",
        run_id="run_1",
        turn_id="turn_1",
        event_type=event_type,
        sequence=sequence,
        payload={"turn_id": "turn_1", **payload},
    )


async def test_interactive_output_replaces_streamed_arguments_with_tool_input_status() -> None:
    outputs: list[str] = []
    output = InteractiveOutput(outputs.append)

    await output.handle_event(
        _event(
            1,
            "model_tool_call_delta",
            {
                "index": 0,
                "id_delta": "call_1",
                "name_delta": "Read",
                "arguments_delta": '{"path"',
            },
        )
    )

    assert "Receiving tool input · Read" in outputs[-1]
    assert '{"path"' not in outputs[-1]
    output_count = len(outputs)

    await output.handle_event(
        _event(
            2,
            "model_tool_call_delta",
            {
                "index": 0,
                "id_delta": "",
                "name_delta": "",
                "arguments_delta": ':"README.md"}',
            },
        )
    )
    assert len(outputs) == output_count
    assert "README.md" not in "".join(outputs)

    await output.handle_event(_event(3, "model_message_completed", {"tool_call_count": 1}))
    rendered = "".join(outputs)
    assert "Read" in rendered
    assert '{"path": "README.md"}' in rendered


async def test_interactive_output_finalizes_interleaved_tool_calls_in_source_order() -> None:
    outputs: list[str] = []
    output = InteractiveOutput(outputs.append)
    deltas = [
        {"index": 0, "id_delta": "call_1", "name_delta": "Read", "arguments_delta": "{"},
        {
            "index": 1,
            "id_delta": "call_2",
            "name_delta": "Write",
            "arguments_delta": '{"path":"out.txt"}',
        },
        {"index": 0, "id_delta": "", "name_delta": "", "arguments_delta": "}"},
    ]
    for sequence, payload in enumerate(deltas, start=1):
        await output.handle_event(_event(sequence, "model_tool_call_delta", payload))

    await output.handle_event(_event(4, "model_message_completed", {"tool_call_count": 2}))

    final_lines = outputs[-2:]
    assert "Read" in final_lines[0]
    assert "Write" in final_lines[1]


async def test_interactive_output_bounds_and_sanitizes_completed_argument_preview() -> None:
    outputs: list[str] = []
    output = InteractiveOutput(outputs.append)

    await output.handle_event(
        _event(
            1,
            "model_tool_call_delta",
            {
                "index": 0,
                "id_delta": "call_1",
                "name_delta": "Write",
                "arguments_delta": "x\n" + "y" * 300,
            },
        )
    )
    assert "x" not in outputs[-1]

    await output.handle_event(_event(2, "model_message_completed", {"tool_call_count": 1}))
    final = outputs[-1]
    assert "\n" not in final.rstrip("\n")
    assert "\\n" in final
    assert "…" in final
    assert len(final) < 320


async def test_interactive_output_animates_tool_execution_and_reports_result() -> None:
    outputs: list[str] = []
    output = InteractiveOutput(outputs.append)

    await output.handle_event(
        _event(
            1,
            "tool_call_started",
            {"tool_call_id": "call_1", "tool_name": "Read", "source_index": 0},
        )
    )
    assert "Running tool · Read" in outputs[-1]

    await output.handle_event(
        _event(
            2,
            "tool_call_completed",
            {
                "tool_call_id": "call_1",
                "tool_name": "Read",
                "status": "completed",
                "is_error": False,
                "source_index": 0,
            },
        )
    )
    assert outputs[-1] == "✓ Read · completed\n"
    assert output.active_tool_calls == {}


async def test_interactive_output_tracks_parallel_tool_execution() -> None:
    outputs: list[str] = []
    output = InteractiveOutput(outputs.append)

    await output.handle_event(
        _event(
            1,
            "tool_call_started",
            {"tool_call_id": "call_1", "tool_name": "Read", "source_index": 0},
        )
    )
    await output.handle_event(
        _event(
            2,
            "tool_call_started",
            {"tool_call_id": "call_2", "tool_name": "Write", "source_index": 1},
        )
    )
    assert output.status_line.message == "Running 2 tools · Read, Write"

    await output.handle_event(
        _event(
            3,
            "tool_call_completed",
            {
                "tool_call_id": "call_1",
                "tool_name": "Read",
                "status": "completed",
                "is_error": False,
                "source_index": 0,
            },
        )
    )
    execution_frame = SPINNER_THEMES["tool_execution"].frames[0]
    assert outputs[-2:] == [
        "✓ Read · completed\n",
        f"{execution_frame} Running tool · Write\n",
    ]

    await output.handle_event(
        _event(
            4,
            "tool_call_completed",
            {
                "tool_call_id": "call_2",
                "tool_name": "Write",
                "status": "failed",
                "is_error": True,
                "source_index": 1,
            },
        )
    )
    assert outputs[-1] == "✗ Write · failed\n"
    assert output.active_tool_calls == {}


async def test_interactive_output_summarizes_mcp_connection_records() -> None:
    outputs: list[str] = []
    output = InteractiveOutput(outputs.append)
    connected_config = MCPServerConfig(
        server_id="docs",
        name="Docs",
        command="mcp-docs",
        auto_start=True,
    )
    failed_config = MCPServerConfig(
        server_id="broken",
        name="Broken",
        command="missing",
        auto_start=True,
    )

    await output.start_mcp_connection((connected_config, failed_config))
    assert "Connecting MCP · 2 server(s)" in outputs[-1]
    await output.finish_mcp_connection(
        (
            MCPServerRecord(
                config=connected_config,
                status="connected",
                tool_names=("docs__search", "docs__open"),
            ),
            MCPServerRecord(
                config=failed_config,
                status="failed",
                error="process exited",
            ),
        )
    )

    rendered = "".join(outputs)
    assert "MCP · 1 connected · 1 failed · 2 tool(s)" in rendered
    assert "✓ Docs [docs] · stdio · 2 tool(s)" in rendered
    assert "✗ Broken [broken] · failed · process exited" in rendered

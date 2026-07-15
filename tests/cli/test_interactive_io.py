from __future__ import annotations

from ouro.cli.interactive_io import InteractiveOutput
from ouro.cli.terminal_status import TerminalStatusRegion
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
    output = InteractiveOutput(
        outputs.append,
        tool_lines=TerminalStatusRegion(outputs.append, animated=True),
    )

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

    assert "\x1b[1;36mRead\x1b[0m · generating parameters" in outputs[-1]
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
    await output.close()


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
    for index, (tool_call_id, tool_name) in enumerate(
        (("call_1", "Read"), ("call_2", "Write"))
    ):
        await output.handle_event(
            _event(
                5 + index,
                "tool_call_started",
                {
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    "source_index": index,
                },
            )
        )
        await output.handle_event(
            _event(
                7 + index,
                "tool_call_completed",
                {
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    "status": "completed",
                    "is_error": False,
                    "source_index": index,
                },
            )
        )
    await output.handle_event(_event(9, "tool_batch_completed", {"batch_id": "batch_1"}))

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
    assert outputs == []

    await output.handle_event(_event(2, "model_message_completed", {"tool_call_count": 1}))
    await output.handle_event(
        _event(
            3,
            "tool_call_completed",
            {
                "tool_call_id": "call_1",
                "tool_name": "Write",
                "status": "completed",
                "is_error": False,
                "source_index": 0,
            },
        )
    )
    await output.handle_event(_event(4, "tool_batch_completed", {"batch_id": "batch_1"}))
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
            "model_tool_call_delta",
            {
                "index": 0,
                "id_delta": "call_1",
                "name_delta": "Read",
                "arguments_delta": '{"path":"README.md"}',
            },
        )
    )
    await output.handle_event(_event(2, "model_message_completed", {"tool_call_count": 1}))
    await output.handle_event(
        _event(
            3,
            "tool_call_requested",
            {"tool_call_id": "call_1", "tool_name": "Read", "source_index": 0},
        )
    )
    await output.handle_event(
        _event(
            4,
            "tool_call_started",
            {"tool_call_id": "call_1", "tool_name": "Read", "source_index": 0},
        )
    )

    await output.handle_event(
        _event(
            5,
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
    await output.handle_event(_event(6, "tool_batch_completed", {"batch_id": "batch_1"}))

    assert outputs == ['✓ Read · completed · ({"path": "README.md"})\n']
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
    assert output.tool_lines.active is True
    assert outputs == []

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
    assert outputs == []

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
    await output.handle_event(_event(5, "tool_batch_completed", {"batch_id": "batch_1"}))
    assert outputs == [
        "✓ Read · completed · ()\n",
        "✗ Write · failed · ()\n",
    ]
    assert output.active_tool_calls == {}


async def test_interactive_output_animates_compaction_progress_and_completion() -> None:
    outputs: list[str] = []
    output = InteractiveOutput(outputs.append)

    await output.handle_event(
        _event(
            1,
            "compaction_started",
            {"compaction_id": "cmp_1", "trigger": "manual"},
        )
    )
    assert output.status_line.semantic == "compaction"
    assert "Compacting context · manual" in outputs[-1]
    output_count = len(outputs)

    await output.handle_event(
        _event(
            2,
            "compaction_progress",
            {"compaction_id": "cmp_1", "trigger": "manual", "phase": "generating"},
        )
    )
    assert len(outputs) == output_count
    assert output.status_line.message == ("Compacting context · manual · generating summary")

    await output.handle_event(
        _event(
            3,
            "compaction_completed",
            {
                "compaction_id": "cmp_1",
                "trigger": "manual",
                "covered_message_count": 3,
                "tokens_before": 800,
                "tokens_after": 300,
            },
        )
    )
    assert outputs[-1] == ("✓ Context compaction completed · 3 messages · 800 → 300 tokens\n")
    assert output.status_line.active is False


async def test_interactive_output_reports_compaction_failure_and_skip() -> None:
    outputs: list[str] = []
    output = InteractiveOutput(outputs.append)

    await output.handle_event(
        _event(
            1,
            "compaction_started",
            {"compaction_id": "cmp_1", "trigger": "auto"},
        )
    )
    await output.handle_event(
        _event(
            2,
            "compaction_failed",
            {"compaction_id": "cmp_1", "error": "provider\nfailed"},
        )
    )
    assert outputs[-1] == "✗ Context compaction failed · provider failed\n"

    await output.handle_event(
        _event(
            3,
            "compaction_skipped",
            {"trigger": "manual", "reason": "no_eligible_closed_turn"},
        )
    )
    assert outputs[-1] == ("· Context compaction skipped · no eligible closed turn\n")


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

from __future__ import annotations

from ouro.application.dto import ApplicationStartupReport, Diagnostic
from ouro.cli.interactive_io import InteractiveOutput
from ouro.cli.terminal_status import SPINNER_THEMES, TerminalStatusLine, TerminalStatusRegion
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


async def test_interactive_output_reports_preparation_and_model_wait_durations() -> None:
    outputs: list[str] = []
    now = 0.0

    def clock() -> float:
        return now

    output = InteractiveOutput(
        outputs.append,
        status_line=TerminalStatusLine(outputs.append, animated=False, clock=clock),
        clock=clock,
    )

    await output.start_waiting()
    now = 1.25
    await output.handle_event(_event(1, "run_started", {}))
    now = 2.0
    await output.handle_event(_event(2, "model_request_started", {}))
    now = 5.5
    await output.handle_event(_event(3, "model_text_delta", {"text": "Hello"}))

    assert "● Response started · 5.5s total · preparing 2.0s · model 3.5s\n" in outputs
    assert outputs[-1] == "Hello"
    await output.close()


async def test_interactive_output_finishes_wait_when_model_has_no_stream_delta() -> None:
    outputs: list[str] = []
    now = 0.0

    def clock() -> float:
        return now

    output = InteractiveOutput(
        outputs.append,
        status_line=TerminalStatusLine(outputs.append, animated=False, clock=clock),
        clock=clock,
    )

    await output.start_waiting()
    now = 2.0
    await output.handle_event(_event(1, "model_request_started", {}))
    now = 4.0
    await output.handle_event(_event(2, "model_message_completed", {"tool_call_count": 0}))

    assert "● Response started · 4.0s total · preparing 2.0s · model 2.0s\n" in outputs
    assert output.wait_timing is None
    await output.close()


async def test_interactive_output_reports_compaction_as_a_distinct_wait_phase() -> None:
    outputs: list[str] = []
    now = 0.0

    def clock() -> float:
        return now

    output = InteractiveOutput(
        outputs.append,
        status_line=TerminalStatusLine(outputs.append, animated=False, clock=clock),
        clock=clock,
    )

    await output.start_waiting()
    now = 1.0
    await output.handle_event(_event(1, "compaction_started", {"trigger": "auto"}))
    now = 3.0
    await output.handle_event(_event(2, "compaction_completed", {}))
    now = 4.0
    await output.handle_event(_event(3, "model_request_started", {}))
    now = 6.0
    await output.handle_event(_event(4, "model_reasoning_delta", {"reasoning_content": "Think"}))

    assert (
        "● Response started · 6.0s total · preparing 2.0s · compacting 2.0s · model 2.0s\n"
        in outputs
    )
    await output.close()


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
    assert "README.md" not in rendered

    await output.handle_event(
        _event(
            4,
            "tool_call_requested",
            {"tool_call_id": "call_1", "tool_name": "Read", "source_index": 0},
        )
    )
    await output.handle_event(
        _event(
            5,
            "approval_requested",
            {"tool_call_id": "call_1", "tool_name": "Read", "source_index": 0},
        )
    )
    assert '{"path": "README.md"}' in outputs[-1]
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
    for index, (tool_call_id, tool_name) in enumerate((("call_1", "Read"), ("call_2", "Write"))):
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

    final_lines = [line for line in outputs if "completed" in line][-2:]
    assert "Read" in final_lines[0]
    assert "Write" in final_lines[1]


async def test_interactive_output_only_shows_bounded_arguments_while_awaiting_approval() -> None:
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
                "name_delta": "Write",
                "arguments_delta": "x\n" + "y" * 300,
            },
        )
    )
    assert "x" not in outputs[-1]

    await output.handle_event(_event(2, "model_message_completed", {"tool_call_count": 1}))
    await output.handle_event(
        _event(
            3,
            "tool_call_requested",
            {"tool_call_id": "call_1", "tool_name": "Write", "source_index": 0},
        )
    )
    assert "x" not in outputs[-1]

    await output.handle_event(
        _event(
            4,
            "approval_requested",
            {"tool_call_id": "call_1", "tool_name": "Write", "source_index": 0},
        )
    )
    approval = outputs[-1]
    assert "\n" not in approval.rstrip("\n")
    assert "\\n" in approval
    assert "…" in approval
    assert len(approval) < 320

    await output.handle_event(
        _event(
            5,
            "tool_call_started",
            {"tool_call_id": "call_1", "tool_name": "Write", "source_index": 0},
        )
    )
    assert "x\\n" not in outputs[-1]
    await output.handle_event(
        _event(
            6,
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
    await output.handle_event(_event(7, "tool_batch_completed", {"batch_id": "batch_1"}))
    assert "x\\n" not in outputs[-1]


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

    assert outputs == [
        "● Read · completed\n",
        f"{SPINNER_THEMES['model_wait'].frames[0]} Waiting · preparing next model turn · 0.0s\n",
    ]
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
        "● Read · completed\n",
        "● Write · failed\n",
        f"{SPINNER_THEMES['model_wait'].frames[0]} Waiting · preparing next model turn · 0.0s\n",
    ]
    assert output.active_tool_calls == {}


async def test_interactive_output_animates_compaction_progress_and_completion() -> None:
    outputs: list[str] = []
    output = InteractiveOutput(outputs.append)

    await output.handle_event(_event(0, "run_started", {"kind": "compaction"}))
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
    assert outputs[-1] == ("● Context compaction completed · 3 messages · 800 → 300 tokens\n")
    assert output.status_line.active is False
    assert output.wait_timing is None


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
    assert outputs[-1] == "● Context compaction failed · provider failed\n"

    await output.handle_event(
        _event(
            3,
            "compaction_skipped",
            {"trigger": "manual", "reason": "no_eligible_closed_turn"},
        )
    )
    assert outputs[-1] == ("● Context compaction skipped · no eligible closed turn\n")


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
    assert "● Docs [docs] · stdio · 2 tool(s)" in rendered
    assert "● Broken [broken] · failed · process exited" in rendered


async def test_interactive_output_surfaces_plugin_startup_diagnostics() -> None:
    outputs: list[str] = []
    output = InteractiveOutput(outputs.append)

    await output.report_plugin_startup(
        ApplicationStartupReport(
            diagnostics=(
                Diagnostic(
                    code="plugin_state_reset",
                    severity="error",
                    component="plugins",
                    message="plugin state was unreadable and has been quarantined",
                    suggested_action="Inspect state.invalid-1.json and reinstall any plugin.",
                ),
                Diagnostic(
                    code="mcp_startup_failed",
                    severity="error",
                    component="mcp",
                    message="unrelated MCP startup diagnostic",
                    server_id="docs",
                ),
            )
        )
    )

    rendered = "".join(outputs)
    assert "Plugins · 1 startup issue(s)" in rendered
    assert "● plugin-state · plugin state was unreadable and has been quarantined" in rendered
    assert "→ Inspect state.invalid-1.json and reinstall any plugin." in rendered
    assert "unrelated MCP startup diagnostic" not in rendered

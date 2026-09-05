from __future__ import annotations

from types import SimpleNamespace
from typing import Literal

from ouro.application.agent_models import AgentInteractionView
from ouro.application.dto import ApplicationStartupReport, Diagnostic
from ouro.cli.interactive_io import InteractionCoordinator, InteractiveOutput
from ouro.cli.terminal_status import (
    SPINNER_THEMES,
    TerminalStatusLine,
    TerminalStatusRegion,
    TerminalTransientRegion,
)
from ouro.core.approvals import ApprovalRecord
from ouro.core.events import EventRecord
from ouro.core.results import RunResult
from ouro.core.state import RunRecord, SessionRecord
from ouro.core.tool_batches import ToolBatchCall, ToolCallBatchRecord
from ouro.core.tools import ToolCallRequest, ToolInputRequest, ToolInvocation
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


def _coordinator(
    *,
    runtime: object,
    output: InteractiveOutput,
    agents: object | None,
    input_func=lambda _prompt: "",
    output_func=lambda _text: None,
    prompt_func=lambda _prompt: None,
) -> InteractionCoordinator:
    return InteractionCoordinator(
        runtime=runtime,  # type: ignore[arg-type]
        output=output,
        input_func=input_func,
        output_func=output_func,
        prompt_func=prompt_func,
        agents=agents,  # type: ignore[arg-type]
    )


def _waiting_tool_run(
    session: SessionRecord,
    *,
    run_id: str,
    status: Literal["waiting_approval", "waiting_user"],
    tool_name: str,
    arguments: dict[str, object],
    input_request: ToolInputRequest | None = None,
) -> RunRecord:
    call_id = f"{run_id}_call"
    request = ToolCallRequest(id=call_id, name=tool_name, arguments=arguments)
    invocation = ToolInvocation(
        tool_call_id=call_id,
        session_id=session.session_id,
        run_id=run_id,
        tool_name=tool_name,
        arguments=arguments,
    )
    approval = (
        ApprovalRecord(
            session_id=session.session_id,
            run_id=run_id,
            tool_call_id=call_id,
        )
        if status == "waiting_approval"
        else None
    )
    batch = ToolCallBatchRecord(
        turn_id=f"{run_id}_turn",
        assistant_message_id=f"{run_id}_assistant",
        phase="awaiting_interaction",
        calls=[
            ToolBatchCall(
                request=request,
                status="awaiting_approval" if approval is not None else "waiting_user",
                invocation=invocation,
                approval=approval,
                input_request=input_request,
            )
        ],
    )
    return RunRecord(
        session_id=session.session_id,
        run_id=run_id,
        status=status,
        pending_approval_id=approval.approval_id if approval is not None else None,
        active_tool_batch=batch,
    )


def _subagent_interaction(
    *,
    kind: str = "approval",
    delegation_id: str = "dlg_1",
    child_run_id: str = "run_child",
    request_id: str = "apr_1",
    tool_name: str = "PowerShell",
    arguments_preview: str = '{"command": "Get-Date"}',
    input_request: object = None,
) -> AgentInteractionView:
    return AgentInteractionView(
        kind=kind,  # type: ignore[arg-type]
        delegation_id=delegation_id,
        agent_id="agt_1",
        agent_name="General",
        task_description="demo",
        child_session_id="ses_child",
        child_run_id=child_run_id,
        tool_call_id="call_1",
        tool_name=tool_name,
        request_id=request_id,
        requested_at="2026-01-01T00:00:00Z",
        arguments_preview=arguments_preview,
        input_request=input_request,
    )


async def test_waiting_child_follower_ignores_running_continuation_state() -> None:
    session = SessionRecord(session_id="ses_root")
    waiting = RunRecord(session_id=session.session_id, run_id="run_root", status="waiting_child")

    class SequencedStore:
        def __init__(self) -> None:
            self.index = 0
            self.runs = (
                waiting,
                waiting.model_copy(update={"status": "running"}),
                waiting.model_copy(update={"status": "completed"}),
            )

        def run_result(self, session_id: str, run_id: str) -> RunResult:
            assert session_id == session.session_id
            assert run_id == waiting.run_id
            return RunResult(session=session, run=self.runs[self.index])

    store = SequencedStore()

    class Subscription:
        def __init__(self) -> None:
            self.closed = False
            self.get_count = 0

        async def get(self) -> object:
            self.get_count += 1
            store.index += 1
            return object()

        def close(self) -> None:
            self.closed = True

    subscription = Subscription()

    class AgentHost:
        async def subscribe_agent_tree(self, root_session_id: str) -> Subscription:
            assert root_session_id == session.session_id
            return subscription

        def list_agent_interactions(self, root_session_id: str) -> tuple[()]:
            assert root_session_id == session.session_id
            return ()

    output = InteractiveOutput(lambda _text: None)
    coordinator = _coordinator(
        runtime=SimpleNamespace(sessions=store),
        output=output,
        agents=AgentHost(),
    )
    resolved = await coordinator._follow_waiting_subagents(
        RunResult(session=session, run=waiting),
        lambda _prompt: "",
    )

    assert resolved is not None
    assert resolved.run.status == "completed"
    assert subscription.get_count == 2
    await coordinator.aclose()
    assert subscription.closed is True
    await output.close()


async def test_follower_resolves_pending_subagent_while_parent_still_waiting() -> None:
    session = SessionRecord(session_id="ses_root")
    waiting = RunRecord(session_id=session.session_id, run_id="run_root", status="waiting_child")

    class SequencedStore:
        def __init__(self) -> None:
            self.index = 0
            # waiting_child (pending surfaced) -> completed after the approval routes.
            self.runs = (
                waiting,
                waiting.model_copy(update={"status": "completed"}),
            )

        def run_result(self, session_id: str, run_id: str) -> RunResult:
            assert session_id == session.session_id
            assert run_id == waiting.run_id
            return RunResult(session=session, run=self.runs[self.index])

    store = SequencedStore()
    interaction = _subagent_interaction()

    class Subscription:
        def close(self) -> None: ...

    class AgentHost:
        def __init__(self) -> None:
            self.approved: list[bool] = []

        async def subscribe_agent_tree(self, root_session_id: str) -> Subscription:
            return Subscription()

        def list_agent_interactions(self, root_session_id: str) -> tuple[AgentInteractionView, ...]:
            return (interaction,) if store.index == 0 else ()

        async def resolve_agent_approval(self, delegation_id: str, *, approved: bool, **_: object):
            self.approved.append(approved)
            store.index = 1  # parent resumes and completes once the approval is routed.
            return object()

    outputs: list[str] = []
    host = AgentHost()
    output = InteractiveOutput(outputs.append)
    coordinator = _coordinator(
        runtime=SimpleNamespace(sessions=store),
        output=output,
        agents=host,
        input_func=lambda _prompt: "y",
        output_func=outputs.append,
    )

    resolved = await coordinator._follow_waiting_subagents(
        RunResult(session=session, run=waiting),
        lambda _prompt: "y",
    )

    assert resolved is not None
    assert resolved.run.status == "completed"
    assert host.approved == [True]
    assert any("approval required" in line for line in outputs)
    await coordinator.aclose()
    await output.close()


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


async def test_interactive_output_shows_live_context_size_while_waiting() -> None:
    outputs: list[str] = []
    output = InteractiveOutput(
        outputs.append,
        status_line=TerminalStatusLine(outputs.append, animated=False),
    )

    await output.start_waiting()
    await output.handle_event(
        _event(
            1,
            "model_request_started",
            {
                "context_metrics": {
                    "estimated_input_tokens": 123,
                    "input_budget_tokens": 900,
                }
            },
        )
    )

    assert "context 123/900 tokens" in output.status_line.message
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
        {"index": 1, "id_delta": "call_1", "name_delta": "Read", "arguments_delta": "{"},
        {
            "index": 2,
            "id_delta": "call_2",
            "name_delta": "Write",
            "arguments_delta": '{"path":"out.txt"}',
        },
        {"index": 1, "id_delta": "", "name_delta": "", "arguments_delta": "}"},
    ]
    for sequence, payload in enumerate(deltas, start=1):
        await output.handle_event(_event(sequence, "model_tool_call_delta", payload))

    await output.handle_event(_event(4, "model_message_completed", {"tool_call_count": 2}))
    calls = (("call_1", "Read"), ("call_2", "Write"))
    for index, (tool_call_id, tool_name) in enumerate(calls):
        await output.handle_event(
            _event(
                5 + index,
                "tool_call_requested",
                {
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    "source_index": index,
                },
            )
        )
    for index, (tool_call_id, tool_name) in enumerate(calls):
        await output.handle_event(
            _event(
                7 + index,
                "tool_call_started",
                {
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    "source_index": index,
                },
            )
        )
    for index, (tool_call_id, tool_name) in enumerate(calls):
        await output.handle_event(
            _event(
                9 + index,
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
    await output.handle_event(_event(11, "tool_batch_completed", {"batch_id": "batch_1"}))

    assert outputs[:2] == [
        "● Read · completed\n",
        "● Write · completed\n",
    ]
    assert len([line for line in outputs if "completed" in line]) == 2


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
                "index": 1,
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
                "index": 1,
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


async def test_interactive_output_reports_background_mcp_state_without_false_failure() -> None:
    outputs: list[str] = []
    output = InteractiveOutput(outputs.append)
    config = MCPServerConfig(
        server_id="docs",
        name="Docs",
        command="mcp-docs",
        auto_start=True,
    )

    await output.finish_mcp_connection((MCPServerRecord(config=config, status="starting"),))

    rendered = "".join(outputs)
    assert "MCP · 0 connected · 1 connecting in background · 0 tool(s)" in rendered
    assert "Docs [docs] · starting in background · stdio" in rendered
    assert "failed" not in rendered


async def test_interactive_output_surfaces_application_startup_diagnostics() -> None:
    outputs: list[str] = []
    output = InteractiveOutput(outputs.append)

    await output.report_startup_diagnostics(
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
                    code="session_runtime_incompatible",
                    severity="warning",
                    component="runtime.recovery",
                    message="old session execution data was rejected",
                    session_id="ses_old",
                    suggested_action="Remove the old session.",
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
    assert "Startup · 2 issue(s)" in rendered
    assert "● plugin-state · plugin state was unreadable and has been quarantined" in rendered
    assert "→ Inspect state.invalid-1.json and reinstall any plugin." in rendered
    assert "● ses_old · old session execution data was rejected" in rendered
    assert "→ Remove the old session." in rendered
    assert "unrelated MCP startup diagnostic" not in rendered


async def test_parallel_subagent_interactions_resolve_without_prompt_corruption() -> None:
    """Reproduce the parallel-subagent bug: two independent children are blocked,

    one on a question and one on an approval, while the parent is ``waiting_child``.
    Answering the first child emits a transcript event (as its run resumes), which
    previously raced the second child's approval prompt on the terminal and left it
    stuck. Both interactions must now be surfaced and resolved in order, oldest
    first, and every prompt must own the terminal through a prompt lease.
    """

    session = SessionRecord(session_id="ses_root")
    waiting = RunRecord(session_id=session.session_id, run_id="run_root", status="waiting_child")

    class SequencedStore:
        def __init__(self) -> None:
            # Parent stays waiting_child until both children resolve, then completes.
            self.completed = False

        def run_result(self, session_id: str, run_id: str) -> RunResult:
            assert session_id == session.session_id
            assert run_id == waiting.run_id
            status = "completed" if self.completed else "waiting_child"
            return RunResult(
                session=session,
                run=waiting.model_copy(update={"status": status}),
            )

    store = SequencedStore()
    question = _subagent_interaction(
        kind="user_input",
        delegation_id="dlg_q",
        child_run_id="run_q",
        request_id="req_q",
        input_request="What is your favorite language?",
    )
    approval = _subagent_interaction(
        kind="approval",
        delegation_id="dlg_a",
        child_run_id="run_a",
        request_id="apr_a",
    )
    outputs: list[str] = []

    class Subscription:
        def close(self) -> None: ...

    class AgentHost:
        def __init__(self) -> None:
            self.answered: list[str] = []
            self.approved: list[bool] = []

        async def subscribe_agent_tree(self, root_session_id: str) -> Subscription:
            return Subscription()

        def list_agent_interactions(self, root_session_id: str) -> tuple[AgentInteractionView, ...]:
            pending: list[AgentInteractionView] = []
            if "dlg_q" not in self.answered:
                pending.append(question)
            if not self.approved:
                pending.append(approval)
            return tuple(pending)

        async def resolve_agent_input(self, delegation_id: str, *, answer: object, **_: object):
            self.answered.append(delegation_id)
            # The answered child resumes and streams; this event previously
            # corrupted the still-open approval prompt line.
            await output.handle_event(_event(99, "model_request_started", {}))
            return object()

        async def resolve_agent_approval(self, delegation_id: str, *, approved: bool, **_: object):
            self.approved.append(approved)
            store.completed = True  # both children resolved; parent joins and completes
            return object()

    host = AgentHost()
    output = InteractiveOutput(
        outputs.append,
        status_line=TerminalStatusLine(outputs.append, animated=False),
    )
    answers = iter(["python", "y"])
    coordinator = _coordinator(
        runtime=SimpleNamespace(sessions=store),
        output=output,
        agents=host,
        input_func=lambda _prompt: next(answers),
        output_func=outputs.append,
    )

    resolved = await coordinator._follow_waiting_subagents(
        RunResult(session=session, run=waiting),
        lambda _prompt: next(answers),
    )

    assert resolved is not None
    assert resolved.run.status == "completed"
    assert host.answered == ["dlg_q"]
    assert host.approved == [True]
    rendered = "".join(outputs)
    assert "Question from subagent" in rendered
    assert "approval required" in rendered
    # The transcript event emitted mid-resolution was buffered, not written between
    # the announcement and the approval prompt.
    await coordinator.aclose()
    await output.close()


async def test_prompt_scope_defers_transcript_events_until_lease_released() -> None:
    outputs: list[str] = []
    output = InteractiveOutput(
        outputs.append,
        status_line=TerminalStatusLine(outputs.append, animated=False),
    )

    async with output.prompt_scope():
        # A model turn that starts while the prompt owns the terminal must not
        # write anything; the event is buffered.
        await output.handle_event(_event(1, "model_request_started", {}))
        await output.handle_event(_event(2, "model_text_delta", {"text": "Hello"}))
        assert outputs == []
        assert output.status_line.message == ""

    # Released: buffered events replay in order, so the streamed text lands after.
    assert "Hello" in "".join(outputs)
    await output.close()


async def test_output_reset_is_reusable_and_close_rejects_late_events() -> None:
    outputs: list[str] = []
    output = InteractiveOutput(
        outputs.append,
        status_line=TerminalStatusLine(outputs.append, animated=False),
    )

    await output.start_waiting()
    await output.reset()
    assert output.status_line.active is False

    await output.handle_event(_event(1, "model_text_delta", {"text": "continued"}))
    assert "continued" in "".join(outputs)

    await output.close()
    rendered_after_close = "".join(outputs)
    await output.handle_event(_event(2, "model_text_delta", {"text": "late"}))
    assert "".join(outputs) == rendered_after_close


async def test_prompt_scope_is_reentrant_and_only_outer_scope_replays() -> None:
    outputs: list[str] = []
    output = InteractiveOutput(
        outputs.append,
        status_line=TerminalStatusLine(outputs.append, animated=False),
    )

    async with output.prompt_scope():
        async with output.prompt_scope():
            await output.handle_event(_event(1, "model_text_delta", {"text": "inner"}))
        # Leaving the inner scope must not replay while the outer lease is held.
        assert "inner" not in "".join(outputs)
    assert "inner" in "".join(outputs)
    await output.close()


async def test_prompt_scope_resumes_suspended_spinner_when_no_event_supersedes() -> None:
    outputs: list[str] = []
    output = InteractiveOutput(
        outputs.append,
        status_line=TerminalStatusLine(outputs.append, animated=True),
    )

    await output.start_waiting("Waiting · model response")
    assert output.status_line.active is True

    async with output.prompt_scope():
        # Suspending erases the visible spinner line but retains its logical status.
        assert any(item.endswith("\x1b[2K") for item in outputs)
        assert output.status_line.semantic == "model_wait"

    # After release the spinner is redrawn and still animating.
    assert output.status_line.active is True
    assert any("Waiting · model response" in item for item in outputs)
    await output.close()


async def test_approval_releases_terminal_before_runtime_continuation_streams() -> None:
    outputs: list[str] = []
    output = InteractiveOutput(
        outputs.append,
        status_line=TerminalStatusLine(outputs.append, animated=False),
    )
    session = SessionRecord(session_id="ses_1")
    waiting = _waiting_tool_run(
        session,
        run_id="run_1",
        status="waiting_approval",
        tool_name="Write",
        arguments={"path": "notes.md"},
    )

    class Runtime:
        streamed_before_return = False

        async def approve_tool(self, **_: object) -> RunResult:
            await output.handle_event(_event(1, "model_text_delta", {"text": "continued"}))
            self.streamed_before_return = "continued" in outputs
            return RunResult(
                session=session,
                run=waiting.model_copy(update={"status": "completed"}),
            )

    runtime = Runtime()
    coordinator = _coordinator(
        runtime=runtime,
        output=output,
        agents=None,
        input_func=lambda _prompt: "y",
        output_func=outputs.append,
    )

    await coordinator.resolve_run(RunResult(session=session, run=waiting))

    assert runtime.streamed_before_return is True
    await coordinator.aclose()
    await output.close()


async def test_interaction_cards_clear_before_approval_and_answer_continuations() -> None:
    outputs: list[str] = []
    transient_region = TerminalTransientRegion(outputs.append, animated=True)
    output = InteractiveOutput(
        outputs.append,
        status_line=TerminalStatusLine(outputs.append, animated=False),
        transient_region=transient_region,
    )
    session = SessionRecord(session_id="ses_1")
    approval = _waiting_tool_run(
        session,
        run_id="run_1",
        status="waiting_approval",
        tool_name="Write",
        arguments={"path": "notes.md"},
    )
    question = _waiting_tool_run(
        session,
        run_id="run_1",
        status="waiting_user",
        tool_name="CustomInput",
        arguments={},
        input_request=ToolInputRequest(data="Which file?"),
    )

    class Runtime:
        async def approve_tool(self, **_: object) -> RunResult:
            outputs.append("approval continuation")
            return RunResult(session=session, run=question)

        async def answer_user(self, **_: object) -> RunResult:
            outputs.append("answer continuation")
            return RunResult(
                session=session,
                run=question.model_copy(update={"status": "completed"}),
            )

    answers = iter(("y", "notes.md"))
    coordinator = _coordinator(
        runtime=Runtime(),
        output=output,
        agents=None,
        input_func=lambda _prompt: next(answers),
        output_func=outputs.append,
    )

    await coordinator.resolve_run(RunResult(session=session, run=approval))

    clear = "\x1b[u\x1b[J"
    approval_card = outputs.index('Approval required: Write({"path": "notes.md"})')
    approval_continuation = outputs.index("approval continuation")
    question_card = outputs.index("Question: Which file?")
    answer_continuation = outputs.index("answer continuation")
    assert "\r\x1b[s" in outputs[:approval_card]
    assert clear in outputs[approval_card:approval_continuation]
    assert "\r\x1b[s" in outputs[approval_continuation:question_card]
    assert clear in outputs[question_card:answer_continuation]
    assert transient_region.active is False
    await coordinator.aclose()
    await output.close()


async def test_transient_prompt_does_not_restore_stale_waiting_status() -> None:
    outputs: list[str] = []
    tool_lines = TerminalStatusRegion(outputs.append, animated=True)
    output = InteractiveOutput(
        outputs.append,
        status_line=TerminalStatusLine(outputs.append, animated=False),
        tool_lines=tool_lines,
        transient_region=TerminalTransientRegion(outputs.append, animated=True),
    )
    await tool_lines.upsert("call_1", "tool_execution", "Write · approval required")
    outputs.clear()

    async with output.transient_prompt_scope():
        outputs.append("Approval required: Write")

    clear_index = outputs.index("\x1b[u\x1b[J")
    assert not any("approval required" in item for item in outputs[clear_index + 1 :])

    await tool_lines.upsert("call_1", "tool_execution", "Write · running")
    assert any("Write · running" in item for item in outputs[clear_index + 1 :])
    await output.close()

import asyncio
import json
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any, Literal, cast

import pytest

from ouro.application.compaction import LLMSummaryCompactionStrategy
from ouro.application.configuration import ConfigurationFacade
from ouro.application.runtime import RuntimeService
from ouro.application.runtime_environment import RuntimeEnvironment
from ouro.cli.interactive import run_interactive
from ouro.cli.main import main
from ouro.cli.run import RuntimeProviderOptions, build_default_runtime, run_json, run_print
from ouro.config import openai_effort_reasoning
from ouro.core.adapter import RuntimeAdapter
from ouro.core.errors import PersistenceError
from ouro.core.provider import ModelMetadata, ProviderCheckResult, ProviderStreamEvent
from ouro.core.reasoning import ReasoningRequest
from ouro.mcp import MCPConfigStore, MCPServerConfig
from ouro.prompts import DEFAULT_SYSTEM_PROMPT
from ouro.providers import FauxProvider, OpenAICompatibleProvider
from ouro.sessions import LocalSessionStore
from ouro.tools import ToolRegistry, builtin_tools
from ouro.tools.builtin_shell import detect_command_shell

MCP_ECHO_SERVER = Path(__file__).parents[1] / "mcp" / "fixtures" / "echo_server.py"


def _expected_builtin_tool_names() -> list[str]:
    shell = detect_command_shell()
    names = ["AskUser", "Edit", "Read", "Write"]
    if shell is not None:
        names.append(shell.tool_name)
    return sorted(names)


def _ask_user_arguments(
    question: str,
    *,
    question_id: str = "choice",
    header: str = "Choice",
    options: tuple[str, ...] = ("A", "B"),
) -> dict[str, object]:
    return {
        "questions": [
            {
                "id": question_id,
                "header": header,
                "question": question,
                "options": [
                    {
                        "label": label,
                        "description": f"Choose {label}.",
                    }
                    for label in options
                ],
            }
        ]
    }


class CapturingProvider:
    provider_id = "capture"

    def __init__(self) -> None:
        self.messages: list[Any] = []
        self.system_prompt = ""

    async def stream(self, *, model, system_prompt="", messages, tools=None):
        del model, tools
        self.system_prompt = system_prompt
        self.messages = list(messages)
        yield ProviderStreamEvent(type="text_delta", text="ok")
        yield ProviderStreamEvent(type="done")


class ApprovalStreamingProvider:
    provider_id = "approval-streaming"

    def __init__(self, target: Path) -> None:
        self.target = target
        self.round = 0
        self.first_continuation_delta_consumed = asyncio.Event()
        self.release_continuation = asyncio.Event()

    async def stream(self, *, model, system_prompt="", messages, tools=None):
        del model, system_prompt, messages, tools
        if self.round == 0:
            self.round += 1
            yield ProviderStreamEvent(
                type="tool_call",
                tool_call={
                    "id": "approval_stream_write",
                    "name": "Write",
                    "arguments": {"path": str(self.target), "content": "approved"},
                },
            )
            yield ProviderStreamEvent(type="done")
            return

        yield ProviderStreamEvent(type="text_delta", text="Streaming after approval")
        # An async generator resumes here only after the consumer has processed
        # the yielded delta, giving the test a deterministic observation point.
        self.first_continuation_delta_consumed.set()
        await self.release_continuation.wait()
        yield ProviderStreamEvent(type="text_delta", text=" completed.")
        yield ProviderStreamEvent(type="done")

    async def check_connection(self, model: ModelMetadata) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            model_id=model.model_id,
            ok=True,
            message="ok",
        )


class SubagentInteractionProvider:
    provider_id = "subagent-interaction"

    def __init__(
        self,
        *,
        mode: Literal["approval", "question"],
        execution_mode: Literal["sync", "async"] = "sync",
        target: Path | None = None,
    ) -> None:
        self.mode = mode
        self.execution_mode = execution_mode
        self.target = target
        self.root_round = 0
        self.subagent_round = 0

    async def stream(self, *, model, system_prompt="", messages, tools=None):
        del model, system_prompt, messages
        names = [item["name"] for item in (tools or [])]
        if "SpawnAgent" not in names:
            if self.subagent_round == 0:
                self.subagent_round += 1
                if self.mode == "approval":
                    assert self.target is not None
                    yield ProviderStreamEvent(
                        type="tool_call",
                        tool_call={
                            "id": "subagent_write",
                            "name": "Write",
                            "arguments": {
                                "path": str(self.target),
                                "content": "written by subagent",
                            },
                        },
                    )
                else:
                    yield ProviderStreamEvent(
                        type="tool_call",
                        tool_call={
                            "id": "subagent_question",
                            "name": "AskUser",
                            "arguments": _ask_user_arguments("Choose A or B?"),
                        },
                    )
                yield ProviderStreamEvent(type="done")
                return
            yield ProviderStreamEvent(type="text_delta", text="subagent interaction resolved")
            yield ProviderStreamEvent(type="done")
            return

        if self.root_round == 0:
            self.root_round += 1
            yield ProviderStreamEvent(
                type="tool_call",
                tool_call={
                    "id": "spawn_subagent",
                    "name": "SpawnAgent",
                    "arguments": {
                        "agent_id": "general",
                        "task": "Request one host interaction.",
                        "description": "Interactive subagent",
                        "execution_mode": self.execution_mode,
                    },
                },
            )
            yield ProviderStreamEvent(type="done")
            return
        if self.execution_mode == "async" and self.root_round == 1:
            self.root_round += 1
            yield ProviderStreamEvent(
                type="text_delta",
                text="parent continued after subagent dispatch",
            )
            yield ProviderStreamEvent(type="done")
            return
        text = (
            "parent resumed after subagent"
            if self.execution_mode == "sync"
            else "parent received subagent result"
        )
        yield ProviderStreamEvent(type="text_delta", text=text)
        yield ProviderStreamEvent(type="done")

    async def check_connection(self, model: ModelMetadata) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            model_id=model.model_id,
            ok=True,
            message="ok",
        )


class ConcurrentSubagentQuestionProvider:
    provider_id = "concurrent-subagent-interaction"

    def __init__(self) -> None:
        self.root_round = 0
        self.completed_subagents: set[str] = set()
        self.children_completed = threading.Event()

    async def stream(self, *, model, system_prompt="", messages, tools=None):
        del model, system_prompt
        names = [item["name"] for item in (tools or [])]
        if "SpawnAgent" not in names:
            task = " ".join(str(getattr(message, "text_content", "")) for message in messages)
            key = "A" if "Question A task" in task else "B"
            answered = any(
                getattr(getattr(message, "tool_result", None), "tool_name", None) == "AskUser"
                for message in messages
            )
            if not answered:
                yield ProviderStreamEvent(
                    type="tool_call",
                    tool_call={
                        "id": f"subagent_question_{key.lower()}",
                        "name": "AskUser",
                        "arguments": _ask_user_arguments(
                            f"Question {key}?",
                            question_id=f"question_{key.lower()}",
                        ),
                    },
                )
                yield ProviderStreamEvent(type="done")
                return
            self.completed_subagents.add(key)
            if self.completed_subagents == {"A", "B"}:
                self.children_completed.set()
            yield ProviderStreamEvent(
                type="text_delta",
                text=f"subagent {key} interaction resolved",
            )
            yield ProviderStreamEvent(type="done")
            return

        if self.root_round == 0:
            self.root_round += 1
            for key in ("A", "B"):
                yield ProviderStreamEvent(
                    type="tool_call",
                    tool_call={
                        "id": f"spawn_subagent_{key.lower()}",
                        "name": "SpawnAgent",
                        "arguments": {
                            "agent_id": "general",
                            "task": f"Question {key} task",
                            "description": f"Concurrent question {key}",
                            "nickname": f"Agent-{key}",
                            "execution_mode": "async",
                        },
                    },
                )
            yield ProviderStreamEvent(type="done")
            return
        self.root_round += 1
        yield ProviderStreamEvent(
            type="text_delta",
            text=f"parent concurrent round {self.root_round}",
        )
        yield ProviderStreamEvent(type="done")

    async def check_connection(self, model: ModelMetadata) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            model_id=model.model_id,
            ok=True,
            message="ok",
        )


def _runtime(tmp_path, event_batches) -> RuntimeService:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    registry = ToolRegistry()
    for tool in builtin_tools():
        registry.register(tool)
    return RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(event_batches=event_batches),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
        tools=registry,
        default_workspace=tmp_path,
        environment=RuntimeEnvironment.local(tmp_path, store.sessions_dir),
    )


def _agent_runtime(
    tmp_path,
    store: LocalSessionStore,
    provider: Any,
) -> RuntimeService:
    registry = ToolRegistry()
    for tool in builtin_tools():
        registry.register(tool)
    return RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="agent-hitl", provider_id=provider.provider_id),
        ),
        tools=registry,
        default_workspace=tmp_path,
        environment=RuntimeEnvironment.local(tmp_path, store.sessions_dir),
    )


def test_run_print_mode_outputs_final_text(tmp_path, capsys) -> None:
    code = main(["run", "--print", "--home", str(tmp_path), "hello"])

    captured = capsys.readouterr()

    assert code == 0
    assert captured.out.strip() == "Hello from OURO."


def test_run_json_mode_outputs_machine_readable_result(tmp_path, capsys) -> None:
    code = main(["run", "--json", "--home", str(tmp_path), "hello"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert code == 0
    assert payload["status"] == "completed"
    assert payload["final_text"] == "Hello from OURO."
    assert payload["session_id"].startswith("ses_")
    assert payload["run_id"].startswith("run_")
    assert payload["usage"]["input_tokens"] == 1
    assert payload["usage"]["output_tokens"] == 4
    assert [event["event_type"] for event in payload["events"]][-1] == "run_completed"
    assert payload["startup_diagnostics"] == []


def test_run_print_surfaces_plugin_state_recovery_before_running(tmp_path, capsys) -> None:
    plugin_root = tmp_path / "plugins"
    plugin_root.mkdir()
    (plugin_root / "state.json").write_text(
        json.dumps({"schema_version": 1, "plugins": {}}),
        encoding="utf-8",
    )

    code = main(["run", "--print", "--home", str(tmp_path), "hello"])

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.strip() == "Hello from OURO."
    assert "Warning: plugin state was unreadable and has been quarantined" in captured.err
    assert "Action: Inspect" in captured.err
    assert not (plugin_root / "state.json").exists()
    assert len(list(plugin_root.glob("state.invalid-*.json"))) == 1


def test_run_json_includes_plugin_state_recovery_diagnostic(tmp_path, capsys) -> None:
    plugin_root = tmp_path / "plugins"
    plugin_root.mkdir()
    (plugin_root / "state.json").write_text(
        json.dumps({"schema_version": 1, "plugins": {}}),
        encoding="utf-8",
    )

    code = main(["run", "--json", "--home", str(tmp_path), "hello"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == 0
    assert captured.err == ""
    assert payload["status"] == "completed"
    assert payload["startup_diagnostics"][0]["code"] == "plugin_state_reset"
    assert payload["startup_diagnostics"][0]["component"] == "plugins"
    assert payload["startup_diagnostics"][0]["recoverable"] is True


async def test_run_print_reports_failed_runtime_result(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    def fake_runtime(
        home=None,
        *,
        provider_options=None,
        system_prompt=None,
        session_backend=None,
    ):
        del provider_options, system_prompt, session_backend
        return _runtime(home or tmp_path, [[ProviderStreamEvent(type="done")]])

    monkeypatch.setattr("ouro.cli.run.build_default_runtime", fake_runtime)

    code = await run_print("hello", home=tmp_path)

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert captured.err.strip() == "Run failed: provider returned an empty assistant message"


async def test_run_print_composes_configured_mcp_tool_and_closes_it(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    MCPConfigStore(tmp_path).upsert(
        MCPServerConfig(
            server_id="echo",
            name="Echo",
            command=sys.executable,
            args=[str(MCP_ECHO_SERVER), "--transport", "stdio"],
            auto_start=True,
            permission_mode="auto",
            timeout_seconds=5,
        )
    )
    runtime = _runtime(
        tmp_path,
        [
            [
                ProviderStreamEvent(
                    type="tool_call",
                    tool_call={
                        "id": "mcp_call",
                        "name": "echo__echo",
                        "arguments": {"value": "assembled"},
                    },
                ),
                ProviderStreamEvent(type="done"),
            ],
            [
                ProviderStreamEvent(type="text_delta", text="MCP completed."),
                ProviderStreamEvent(type="done"),
            ],
        ],
    )

    monkeypatch.setattr("ouro.cli.run.build_default_runtime", lambda *args, **kwargs: runtime)

    code = await run_print("use MCP", home=tmp_path)

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.strip() == "MCP completed."
    assert runtime.tools.names() == _expected_builtin_tool_names()
    session = runtime.store.list_sessions()[0]
    snapshot = runtime.store.load_session(session.session_id)
    tool_message = next(message for message in snapshot.messages if message.role == "tool_result")
    assert tool_message.tool_result is not None
    assert tool_message.tool_result.tool_name == "echo__echo"
    assert tool_message.tool_result.is_error is False
    assert "assembled" in tool_message.text_content


async def test_run_json_composes_mcp_without_corrupting_machine_output(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    MCPConfigStore(tmp_path).upsert(
        MCPServerConfig(
            server_id="echo",
            name="Echo",
            command=sys.executable,
            args=[str(MCP_ECHO_SERVER), "--transport", "stdio"],
            auto_start=True,
            permission_mode="auto",
            timeout_seconds=5,
        )
    )
    runtime = _runtime(
        tmp_path,
        [
            [
                ProviderStreamEvent(
                    type="tool_call",
                    tool_call={
                        "id": "json_mcp_call",
                        "name": "echo__echo",
                        "arguments": {"value": "json"},
                    },
                ),
                ProviderStreamEvent(type="done"),
            ],
            [
                ProviderStreamEvent(type="text_delta", text="JSON MCP done."),
                ProviderStreamEvent(type="done"),
            ],
        ],
    )
    monkeypatch.setattr("ouro.cli.run.build_default_runtime", lambda *args, **kwargs: runtime)

    code = await run_json("use MCP", home=tmp_path)

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == 0
    assert payload["status"] == "completed"
    assert payload["final_text"] == "JSON MCP done."
    assert captured.err == ""
    assert runtime.tools.names() == _expected_builtin_tool_names()


async def test_run_print_isolates_mcp_start_failure(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    MCPConfigStore(tmp_path).upsert(
        MCPServerConfig(
            server_id="broken",
            name="Broken",
            command=sys.executable,
            args=["-c", "raise SystemExit(2)"],
            auto_start=True,
            auto_reconnect=False,
            timeout_seconds=1,
        )
    )
    runtime = _runtime(
        tmp_path,
        [
            [
                ProviderStreamEvent(type="text_delta", text="still running"),
                ProviderStreamEvent(type="done"),
            ]
        ],
    )
    monkeypatch.setattr("ouro.cli.run.build_default_runtime", lambda *args, **kwargs: runtime)

    code = await run_print("hello", home=tmp_path)

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.strip() == "still running"
    assert "Warning: MCP server broken failed to start" in captured.err


def test_run_mode_can_continue_existing_session(tmp_path, capsys) -> None:
    first_code = main(["run", "--json", "--home", str(tmp_path), "--title", "First", "hello"])
    first = json.loads(capsys.readouterr().out)

    second_code = main(
        [
            "run",
            "--json",
            "--home",
            str(tmp_path),
            "--session",
            first["session_id"],
            "again",
        ]
    )
    second = json.loads(capsys.readouterr().out)

    assert first_code == 0
    assert second_code == 0
    assert second["session_id"] == first["session_id"]
    assert second["run_id"] != first["run_id"]


async def test_interactive_runs_prompts_and_commands(tmp_path) -> None:
    inputs = iter(
        [
            "hello",
            "/sessions",
            "/models",
            "/model",
            "/context",
            "/context full",
            "/export",
            "/new",
            "/exit",
        ]
    )
    outputs: list[str] = []

    async_code = await run_interactive(
        home=tmp_path,
        input_func=lambda _prompt: next(inputs),
        output_func=outputs.append,
    )

    assert async_code == 0
    assert "Hello from OURO." in outputs
    assert any(output.startswith("Sessions:") for output in outputs)
    assert "No configured models available." in outputs
    assert "No configured model selected." in outputs
    assert any(output.startswith("Context: current") for output in outputs)
    assert any("Provider context:" in output for output in outputs)
    assert any(str(tmp_path) in output for output in outputs)
    assert "Started a new session." in outputs


async def test_interactive_composes_mcp_for_process_lifetime(
    tmp_path,
    monkeypatch,
) -> None:
    MCPConfigStore(tmp_path).upsert(
        MCPServerConfig(
            server_id="echo",
            name="Echo",
            command=sys.executable,
            args=[str(MCP_ECHO_SERVER), "--transport", "stdio"],
            auto_start=True,
            permission_mode="auto",
            timeout_seconds=5,
        )
    )
    runtime = _runtime(
        tmp_path,
        [
            [
                ProviderStreamEvent(
                    type="tool_call",
                    tool_call={
                        "id": "interactive_mcp",
                        "name": "echo__echo",
                        "arguments": {"value": "interactive"},
                    },
                ),
                ProviderStreamEvent(type="done"),
            ],
            [
                ProviderStreamEvent(type="text_delta", text="Interactive MCP done."),
                ProviderStreamEvent(type="done"),
            ],
        ],
    )

    def fake_runtime(*args, event_sink=None, **kwargs):
        if event_sink is not None:
            runtime.add_event_sink(event_sink)
        return runtime

    monkeypatch.setattr("ouro.cli.interactive.build_default_runtime", fake_runtime)
    inputs = iter(["use MCP", "/exit"])
    outputs: list[str] = []

    code = await run_interactive(
        home=tmp_path,
        input_func=lambda _prompt: next(inputs),
        output_func=outputs.append,
    )

    assert code == 0
    assert "Interactive MCP done." in outputs
    rendered = "".join(outputs)
    assert "Connecting MCP · 1 server(s) · Echo" in rendered
    assert "MCP · 1 connected · 1 tool(s)" in rendered
    assert "● Echo [echo] · stdio · 1 tool(s)" in rendered
    assert runtime.tools.names() == _expected_builtin_tool_names()


async def test_interactive_reports_runtime_error_and_keeps_prompt_alive(tmp_path) -> None:
    class FailingStore(LocalSessionStore):
        def save_run(self, run) -> None:
            del run
            raise PersistenceError("disk unavailable")

    store = FailingStore(tmp_path)
    store.initialize()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider([ProviderStreamEvent(type="done")]),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
        tools=ToolRegistry(),
    )
    inputs = iter(["hello", "/exit"])
    outputs: list[str] = []

    code = await run_interactive(
        runtime=runtime,
        input_func=lambda _prompt: next(inputs),
        output_func=outputs.append,
    )

    assert code == 0
    assert "Error: disk unavailable" in outputs


async def test_interactive_streams_model_text_deltas(tmp_path) -> None:
    runtime = _runtime(
        tmp_path,
        [
            [
                ProviderStreamEvent(type="text_delta", text="Hel"),
                ProviderStreamEvent(type="text_delta", text="lo"),
                ProviderStreamEvent(type="done"),
            ],
        ],
    )
    inputs = iter(["hello", "/exit"])
    outputs: list[str] = []

    code = await run_interactive(
        runtime=runtime,
        input_func=lambda _prompt: next(inputs),
        output_func=outputs.append,
    )

    assert code == 0
    assert "Hel" in outputs
    assert "lo" in outputs
    assert "Hello" not in outputs


async def test_interactive_approves_tool_and_continues_run(tmp_path) -> None:
    target = tmp_path / "approved.txt"
    runtime = _runtime(
        tmp_path,
        [
            [
                ProviderStreamEvent(
                    type="tool_call",
                    tool_call={
                        "id": "call_1",
                        "name": "Write",
                        "arguments": {"path": str(target), "content": "approved"},
                    },
                ),
                ProviderStreamEvent(type="done"),
            ],
            [
                ProviderStreamEvent(type="text_delta", text="Done."),
                ProviderStreamEvent(type="done"),
            ],
        ],
    )
    inputs = iter(["write file", "y", "/exit"])
    outputs: list[str] = []

    code = await run_interactive(
        runtime=runtime,
        input_func=lambda _prompt: next(inputs),
        output_func=outputs.append,
    )

    assert code == 0
    assert target.read_text(encoding="utf-8") == "approved"
    assert any(output.startswith("Approval required: Write(") for output in outputs)
    assert "Done." in outputs


async def test_interactive_streams_before_approval_continuation_returns(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    target = tmp_path / "approval-streamed.txt"
    provider = ApprovalStreamingProvider(target)
    runtime = _agent_runtime(tmp_path, store, provider)
    inputs = iter(["write file", "y", "/exit"])
    outputs: list[str] = []

    interactive = asyncio.create_task(
        run_interactive(
            runtime=runtime,
            input_func=lambda _prompt: next(inputs),
            output_func=outputs.append,
        )
    )
    try:
        await asyncio.wait_for(provider.first_continuation_delta_consumed.wait(), timeout=5)
        assert "Streaming after approval" in outputs
        assert interactive.done() is False
    finally:
        provider.release_continuation.set()

    assert await asyncio.wait_for(interactive, timeout=5) == 0
    assert target.read_text(encoding="utf-8") == "approved"


async def test_interactive_modifies_tool_arguments_before_approval(tmp_path) -> None:
    target = tmp_path / "modified.txt"
    runtime = _runtime(
        tmp_path,
        [
            [
                ProviderStreamEvent(
                    type="tool_call",
                    tool_call={
                        "id": "call_1",
                        "name": "Write",
                        "arguments": {"path": str(target), "content": "original"},
                    },
                ),
                ProviderStreamEvent(type="done"),
            ],
            [
                ProviderStreamEvent(type="text_delta", text="Modified and written."),
                ProviderStreamEvent(type="done"),
            ],
        ],
    )
    inputs = iter(
        [
            "write file",
            "m",
            json.dumps({"path": str(target), "content": "modified"}),
            "/exit",
        ]
    )
    outputs: list[str] = []

    code = await run_interactive(
        runtime=runtime,
        input_func=lambda _prompt: next(inputs),
        output_func=outputs.append,
    )

    assert code == 0
    assert target.read_text(encoding="utf-8") == "modified"
    assert "Modified and written." in outputs


async def test_interactive_aborts_waiting_approval(tmp_path) -> None:
    target = tmp_path / "aborted.txt"
    runtime = _runtime(
        tmp_path,
        [
            [
                ProviderStreamEvent(
                    type="tool_call",
                    tool_call={
                        "id": "call_1",
                        "name": "Write",
                        "arguments": {"path": str(target), "content": "nope"},
                    },
                ),
                ProviderStreamEvent(type="done"),
            ],
        ],
    )
    inputs = iter(["write file", "/abort", "/exit"])
    outputs: list[str] = []

    code = await run_interactive(
        runtime=runtime,
        input_func=lambda _prompt: next(inputs),
        output_func=outputs.append,
    )

    assert code == 0
    assert not target.exists()
    assert any(output.startswith("Approval required: Write(") for output in outputs)
    assert "Run aborted: User aborted" in outputs


async def test_interactive_keeps_running_when_approval_resume_fails(
    tmp_path,
    monkeypatch,
) -> None:
    target = tmp_path / "not-written.txt"
    runtime = _runtime(
        tmp_path,
        [
            [
                ProviderStreamEvent(
                    type="tool_call",
                    tool_call={
                        "id": "call_1",
                        "name": "Write",
                        "arguments": {"path": str(target), "content": "nope"},
                    },
                ),
                ProviderStreamEvent(type="done"),
            ],
        ],
    )

    async def fail_approval_resume(**kwargs):
        del kwargs
        raise PersistenceError("approval resume failed")

    monkeypatch.setattr(runtime, "approve_tool", fail_approval_resume)
    inputs = iter(["write file", "n", "", "/exit"])
    outputs: list[str] = []

    code = await run_interactive(
        runtime=runtime,
        input_func=lambda _prompt: next(inputs),
        output_func=outputs.append,
    )

    assert code == 0
    assert not target.exists()
    assert any(output.startswith("Approval required: Write(") for output in outputs)
    assert "Error: approval resume failed" in outputs


async def test_interactive_open_waiting_session_drains_follow_up_after_rejection(tmp_path) -> None:
    target = tmp_path / "not-written.txt"
    runtime = _runtime(
        tmp_path,
        [
            [
                ProviderStreamEvent(
                    type="tool_call",
                    tool_call={
                        "id": "call_1",
                        "name": "Write",
                        "arguments": {"path": str(target), "content": "nope"},
                    },
                ),
                ProviderStreamEvent(type="done"),
            ],
            [
                ProviderStreamEvent(type="text_delta", text="Write was rejected."),
                ProviderStreamEvent(type="done"),
            ],
            [
                ProviderStreamEvent(type="text_delta", text="Hello from follow-up."),
                ProviderStreamEvent(type="done"),
            ],
        ],
    )
    waiting = await runtime.run("write file")
    inputs = iter(
        [
            f"/open {waiting.session.session_id}",
            "hi",
            "n",
            "",
            "/exit",
        ]
    )
    outputs: list[str] = []

    code = await run_interactive(
        runtime=runtime,
        input_func=lambda _prompt: next(inputs),
        output_func=outputs.append,
    )
    snapshot = runtime.store.load_session(waiting.session.session_id)

    assert code == 0
    assert not target.exists()
    assert [run.status for run in snapshot.runs] == ["completed", "completed"]
    assert [message.text_content for message in snapshot.messages if message.role == "user"] == [
        "write file",
        "hi",
    ]
    assert snapshot.messages[-1].text_content == "Hello from follow-up."
    assert "follow_up_queue" not in snapshot.session.metadata
    assert f"Opened session: {waiting.session.session_id}" in outputs
    assert any(output.startswith("Approval required: Write(") for output in outputs)


async def test_interactive_answers_ask_user_and_continues_run(tmp_path) -> None:
    runtime = _runtime(
        tmp_path,
        [
            [
                ProviderStreamEvent(
                    type="tool_call",
                    tool_call={
                        "id": "ask_1",
                        "name": "AskUser",
                        "arguments": _ask_user_arguments(
                            "Which file?",
                            question_id="file",
                            header="File",
                            options=("notes.md", "README.md"),
                        ),
                    },
                ),
                ProviderStreamEvent(type="done"),
            ],
            [
                ProviderStreamEvent(type="text_delta", text="Using notes.md."),
                ProviderStreamEvent(type="done"),
            ],
        ],
    )
    inputs = iter(["need info", "notes.md", "/exit"])
    outputs: list[str] = []

    code = await run_interactive(
        runtime=runtime,
        input_func=lambda _prompt: next(inputs),
        output_func=outputs.append,
    )

    assert code == 0
    assert any("Question · File\nWhich file?" in output for output in outputs)
    assert "Using notes.md." in outputs


async def test_interactive_approves_sync_subagent_tool_and_resumes_parent(
    tmp_path,
    monkeypatch,
) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    target = tmp_path / "subagent-approved.txt"
    provider = SubagentInteractionProvider(mode="approval", target=target)
    runtime = _agent_runtime(tmp_path, store, provider)

    def fake_runtime(*args, event_sink=None, **kwargs):
        del args, kwargs
        if event_sink is not None:
            runtime.add_event_sink(event_sink)
        return runtime

    monkeypatch.setattr("ouro.cli.interactive.build_default_runtime", fake_runtime)
    inputs = iter(["test subagent approval", "y", "/exit"])
    outputs: list[str] = []

    code = await run_interactive(
        home=tmp_path,
        input_func=lambda _prompt: next(inputs),
        output_func=outputs.append,
    )

    assert code == 0
    assert target.read_text(encoding="utf-8") == "written by subagent"
    assert any(
        output.startswith("Subagent General") and "approval required: Write(" in output
        for output in outputs
    )
    assert "parent resumed after subagent" in outputs
    assert "Run stopped with status: waiting_child" not in outputs
    assert "Run stopped with status: running" not in outputs
    assert "subagent interaction resolved" not in outputs
    assert not any(output.startswith("● Write ·") for output in outputs)
    assert sum(output.startswith("● Response started") for output in outputs) == 2


async def test_interactive_answers_sync_subagent_question_and_resumes_parent(
    tmp_path,
    monkeypatch,
) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    provider = SubagentInteractionProvider(mode="question")
    runtime = _agent_runtime(tmp_path, store, provider)

    def fake_runtime(*args, event_sink=None, **kwargs):
        del args, kwargs
        if event_sink is not None:
            runtime.add_event_sink(event_sink)
        return runtime

    monkeypatch.setattr("ouro.cli.interactive.build_default_runtime", fake_runtime)
    inputs = iter(["test subagent question", "A", "/exit"])
    outputs: list[str] = []

    code = await run_interactive(
        home=tmp_path,
        input_func=lambda _prompt: next(inputs),
        output_func=outputs.append,
    )

    assert code == 0
    assert any(
        output.startswith("Question from subagent General") and "Choose A or B" in output
        for output in outputs
    )
    assert "parent resumed after subagent" in outputs
    assert "Run stopped with status: waiting_child" not in outputs
    assert "Run stopped with status: running" not in outputs
    assert "subagent interaction resolved" not in outputs
    assert not any(output.startswith("● AskUser ·") for output in outputs)
    assert sum(output.startswith("● Response started") for output in outputs) == 2


@pytest.mark.parametrize(
    ("mode", "answer"),
    [
        ("approval", "y"),
        ("question", "A"),
    ],
)
async def test_interactive_routes_async_subagent_hitl_without_rendering_trajectory(
    tmp_path,
    monkeypatch,
    mode: Literal["approval", "question"],
    answer: str,
) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    target = tmp_path / "async-subagent-approved.txt" if mode == "approval" else None
    provider = SubagentInteractionProvider(
        mode=mode,
        execution_mode="async",
        target=target,
    )
    runtime = _agent_runtime(tmp_path, store, provider)

    def fake_runtime(*args, event_sink=None, **kwargs):
        del args, kwargs
        if event_sink is not None:
            runtime.add_event_sink(event_sink)
        return runtime

    monkeypatch.setattr("ouro.cli.interactive.build_default_runtime", fake_runtime)
    interaction_announced = threading.Event()
    result_rendered = threading.Event()
    outputs: list[str] = []
    input_count = 0

    def output_func(text: str) -> None:
        outputs.append(text)
        if (
            mode == "approval"
            and text.startswith("Subagent General")
            and "approval required: Write(" in text
        ):
            interaction_announced.set()
        if (
            mode == "question"
            and text.startswith("Question from subagent General")
            and "Choose A or B" in text
        ):
            interaction_announced.set()
        if text == "parent received subagent result":
            result_rendered.set()

    def input_func(_prompt: str) -> str:
        nonlocal input_count
        input_count += 1
        if input_count == 1:
            return f"test async subagent {mode}"
        if input_count == 2:
            assert interaction_announced.wait(timeout=5)
            return answer
        if input_count == 3:
            assert result_rendered.wait(timeout=5)
            return "/exit"
        raise EOFError

    code = await run_interactive(
        home=tmp_path,
        input_func=input_func,
        output_func=output_func,
    )

    assert code == 0
    if target is not None:
        assert target.read_text(encoding="utf-8") == "written by subagent"
    assert interaction_announced.is_set()
    assert "parent continued after subagent dispatch" in outputs
    assert "parent received subagent result" in outputs
    assert "subagent interaction resolved" not in outputs
    assert not any(output.startswith("● Write ·") for output in outputs)
    assert not any(output.startswith("● AskUser ·") for output in outputs)
    assert "Run stopped with status: waiting_child" not in outputs
    assert "Run stopped with status: running" not in outputs


async def test_interactive_does_not_submit_empty_async_subagent_answer(
    tmp_path,
    monkeypatch,
) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    provider = SubagentInteractionProvider(mode="question", execution_mode="async")
    runtime = _agent_runtime(tmp_path, store, provider)

    def fake_runtime(*args, event_sink=None, **kwargs):
        del args, kwargs
        if event_sink is not None:
            runtime.add_event_sink(event_sink)
        return runtime

    monkeypatch.setattr("ouro.cli.interactive.build_default_runtime", fake_runtime)
    interaction_announced = threading.Event()
    answer_prompted = threading.Event()
    result_rendered = threading.Event()
    outputs: list[str] = []
    input_count = 0

    def output_func(text: str) -> None:
        outputs.append(text)
        if text.startswith("Question from subagent General"):
            interaction_announced.set()
        if text.startswith("Select [1-"):
            answer_prompted.set()
        if text == "parent received subagent result":
            result_rendered.set()

    def input_func(_prompt: str) -> str:
        nonlocal input_count
        input_count += 1
        if input_count == 1:
            return "test async subagent empty answer"
        if input_count == 2:
            assert answer_prompted.wait(timeout=5)
            return ""
        if input_count == 3:
            return "A"
        if input_count == 4:
            assert result_rendered.wait(timeout=5)
            return "/exit"
        raise EOFError

    code = await run_interactive(
        home=tmp_path,
        input_func=input_func,
        output_func=output_func,
    )

    assert code == 0
    root = next(session for session in store.list_sessions() if session.session_kind == "primary")
    relation = store.list_delegations(root.session_id)[0]
    assert relation.child_session_id is not None
    resolved_answers = [
        event.payload["answer"]
        for event in store.load_session(relation.child_session_id).events
        if event.event_type == "ask_user_resolved"
    ]
    assert resolved_answers == [{"answers": {"choice": ["A"]}}]
    assert interaction_announced.is_set()
    assert any("cannot be empty" in output.lower() for output in outputs)
    assert sum(output.startswith("Question from subagent General") for output in outputs) == 1


async def test_interactive_resolves_concurrent_async_subagent_questions_once(
    tmp_path,
    monkeypatch,
) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    provider = ConcurrentSubagentQuestionProvider()
    runtime = _agent_runtime(tmp_path, store, provider)

    def fake_runtime(*args, event_sink=None, **kwargs):
        del args, kwargs
        if event_sink is not None:
            runtime.add_event_sink(event_sink)
        return runtime

    monkeypatch.setattr("ouro.cli.interactive.build_default_runtime", fake_runtime)
    announced_questions: queue.Queue[str] = queue.Queue()
    outputs: list[str] = []
    input_count = 0

    def output_func(text: str) -> None:
        outputs.append(text)
        if text.startswith("Question from subagent"):
            if "Question A?" in text:
                announced_questions.put("A")
            elif "Question B?" in text:
                announced_questions.put("B")

    def input_func(_prompt: str) -> str:
        nonlocal input_count
        input_count += 1
        if input_count == 1:
            return "test concurrent async subagent questions"
        if input_count in {2, 3}:
            key = announced_questions.get(timeout=5)
            return f"answer-{key}"
        if input_count == 4:
            assert provider.children_completed.wait(timeout=5)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                roots = [
                    session
                    for session in store.list_sessions()
                    if session.session_kind == "primary"
                ]
                if roots:
                    relations = store.list_delegations(roots[0].session_id)
                    if len(relations) == 2 and all(item.result is not None for item in relations):
                        return "/exit"
                time.sleep(0.01)
            raise AssertionError("concurrent subagent results were not finalized")
        raise EOFError

    code = await run_interactive(
        home=tmp_path,
        input_func=input_func,
        output_func=output_func,
    )

    assert code == 0
    root = next(session for session in store.list_sessions() if session.session_kind == "primary")
    relations = store.list_delegations(root.session_id)
    assert len(relations) == 2
    for relation in relations:
        assert relation.child_session_id is not None
        assert relation.nickname is not None
        key = relation.nickname.removeprefix("Agent-")
        resolved_answers = [
            event.payload["answer"]
            for event in store.load_session(relation.child_session_id).events
            if event.event_type == "ask_user_resolved"
        ]
        assert resolved_answers == [
            {"answers": {f"question_{key.lower()}": [f"answer-{key}"]}}
        ]
    assert sum("Question A?" in output for output in outputs) == 1
    assert sum("Question B?" in output for output in outputs) == 1
    assert not any(output.startswith("Error:") for output in outputs), "\n".join(outputs)
    assert not any(output.startswith("● AskUser ·") for output in outputs)


async def test_interactive_aborts_waiting_user(tmp_path) -> None:
    runtime = _runtime(
        tmp_path,
        [
            [
                ProviderStreamEvent(
                    type="tool_call",
                    tool_call={
                        "id": "ask_1",
                        "name": "AskUser",
                        "arguments": _ask_user_arguments(
                            "Which file?",
                            question_id="file",
                            header="File",
                            options=("notes.md", "README.md"),
                        ),
                    },
                ),
                ProviderStreamEvent(type="done"),
            ],
        ],
    )
    inputs = iter(["need info", "/abort", "/exit"])
    outputs: list[str] = []

    code = await run_interactive(
        runtime=runtime,
        input_func=lambda _prompt: next(inputs),
        output_func=outputs.append,
    )

    assert code == 0
    assert any("Question · File\nWhich file?" in output for output in outputs)
    assert "Run aborted: User aborted" in outputs


def test_main_without_subcommand_enters_interactive(monkeypatch, tmp_path) -> None:
    calls = []

    async def fake_interactive(*, home, system_prompt, session_backend=None):
        assert session_backend is None
        calls.append((home, system_prompt))
        return 0

    monkeypatch.setattr("ouro.cli.main.run_interactive", fake_interactive)

    code = main(["--home", str(tmp_path), "--system-prompt", "Custom"])

    assert code == 0
    assert calls == [(str(tmp_path), "Custom")]


def test_run_mode_passes_system_prompt_override(monkeypatch) -> None:
    calls = []

    async def fake_run_print(prompt, **kwargs):
        calls.append((prompt, kwargs["system_prompt"]))
        return 0

    monkeypatch.setattr("ouro.cli.main.run_print", fake_run_print)

    code = main(["run", "--system-prompt", "Custom", "hello"])

    assert code == 0
    assert calls == [("hello", "Custom")]


def test_run_mode_passes_repeatable_resource_references(monkeypatch) -> None:
    calls = []

    async def fake_run_print(prompt, **kwargs):
        calls.append((prompt, kwargs["session_id"], kwargs["resource_refs"]))
        return 0

    monkeypatch.setattr("ouro.cli.main.run_print", fake_run_print)

    code = main(
        [
            "run",
            "--session",
            "ses_existing",
            "--resource",
            "res_" + "1" * 64,
            "--resource",
            "res_" + "2" * 64,
            "use",
            "attachments",
        ]
    )

    assert code == 0
    assert calls == [
        (
            "use attachments",
            "ses_existing",
            ["res_" + "1" * 64, "res_" + "2" * 64],
        )
    ]


def test_run_mode_passes_endpoint_capability_overrides(monkeypatch) -> None:
    calls = []

    async def fake_run_print(prompt, **kwargs):
        calls.append((prompt, kwargs["provider_options"]))
        return 0

    monkeypatch.setattr("ouro.cli.main.run_print", fake_run_print)

    code = main(
        [
            "run",
            "--endpoint",
            "reasoning",
            "--temperature",
            "0",
            "--context-window",
            "200000",
            "--max-output-tokens",
            "16000",
            "--no-tools",
            "--vision",
            "--no-streaming",
            "--structured-output",
            "--thinking-mode",
            "enabled",
            "--thinking-effort",
            "high",
            "hello",
        ]
    )

    assert code == 0
    options = calls[0][1]
    assert options.endpoint_id == "reasoning"
    assert options.temperature == 0
    assert options.context_window == 200_000
    assert options.max_output_tokens == 16_000
    assert options.supports_tools is False
    assert options.supports_vision is True
    assert options.supports_streaming is False
    assert options.supports_structured_output is True
    assert options.reasoning_request == ReasoningRequest(mode="enabled", effort="high")


def test_run_mode_rejects_effort_when_reasoning_is_disabled(tmp_path, capsys) -> None:
    code = main(
        [
            "run",
            "--json",
            "--home",
            str(tmp_path),
            "--thinking-mode",
            "disabled",
            "--thinking-effort",
            "high",
            "hello",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["status"] == "failed"
    assert payload["error_type"] == "ConfigError"
    assert "disabled reasoning cannot specify an effort" in payload["error"]


def test_build_runtime_with_openai_compatible_provider_options(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OURO_TEST_KEY", "secret")

    runtime = build_default_runtime(
        tmp_path,
        provider_options=RuntimeProviderOptions(
            protocol="openai-chat-completions",
            provider_id="glm",
            model="glm-test",
            base_url="https://example.test/v1",
            api_key_env="OURO_TEST_KEY",
            timeout=12.0,
        ),
    )

    provider = runtime.adapter.provider
    assert isinstance(provider, OpenAICompatibleProvider)
    assert runtime.adapter.model.model_id == "glm-test"
    assert runtime.adapter.model.provider_id == "glm"
    assert provider.base_url == "https://example.test/v1"
    assert provider.api_key == "secret"
    assert provider.timeout == 12.0


def test_build_runtime_reads_local_config_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OURO_TEST_KEY", "secret")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "providers": {
                    "default": "configured",
                    "items": {
                        "configured": {
                            "provider_type": "openai-compatible",
                            "protocol": "openai-chat-completions",
                            "provider_id": "configured",
                            "credential_ref": "env:OURO_TEST_KEY",
                            "timeout_seconds": 9.0,
                            "default_endpoint": "default",
                            "endpoints": {
                                "default": {
                                    "model_id": "configured-model",
                                    "base_url": "https://configured.test/v1",
                                }
                            },
                        }
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    runtime = build_default_runtime(tmp_path)

    provider = runtime.adapter.provider
    assert isinstance(provider, OpenAICompatibleProvider)
    assert runtime.adapter.model.model_id == "configured-model"
    assert runtime.adapter.model.provider_id == "configured"
    assert provider.base_url == "https://configured.test/v1"
    assert provider.timeout == 9.0


def test_config_prompt_command_persists_system_prompt(tmp_path, capsys) -> None:
    set_code = main(
        [
            "config",
            "prompt",
            "--home",
            str(tmp_path),
            "--set",
            "Custom prompt.",
            "--json",
        ]
    )
    set_payload = json.loads(capsys.readouterr().out)

    assert set_code == 0
    assert set_payload["configured"] is True
    assert set_payload["system_prompt"] == "Custom prompt."
    assert build_default_runtime(tmp_path).system_prompt == "Custom prompt."

    show_code = main(["config", "prompt", "--home", str(tmp_path), "--json"])
    show_payload = json.loads(capsys.readouterr().out)

    assert show_code == 0
    assert show_payload["effective_system_prompt"] == "Custom prompt."

    clear_code = main(["config", "prompt", "--home", str(tmp_path), "--clear", "--json"])
    clear_payload = json.loads(capsys.readouterr().out)

    assert clear_code == 0
    assert clear_payload["configured"] is False
    assert build_default_runtime(tmp_path).system_prompt == DEFAULT_SYSTEM_PROMPT


async def test_build_runtime_injects_available_skills_from_home_and_workspace(
    tmp_path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_skill(home / "skills" / "pdf", "pdf", "PDF", "Extract text from PDFs.")
    _write_skill(workspace / ".ouro" / "skills" / "xlsx", "xlsx", "XLSX", "Process spreadsheets.")
    monkeypatch.chdir(workspace)
    provider = CapturingProvider()

    def fake_adapter(_options):
        return RuntimeAdapter(
            provider,
            ModelMetadata(model_id="capture", provider_id="capture"),
        )

    monkeypatch.setattr("ouro.local_application.build_runtime_adapter", fake_adapter)
    runtime = build_default_runtime(home)

    assert runtime.hooks.registered("after_context_build") == []

    await runtime.run("help me with files")

    assert provider.system_prompt.startswith(DEFAULT_SYSTEM_PROMPT.strip())
    assert "<skills_instructions>" in provider.system_prompt
    assert "<name>pdf</name>" in provider.system_prompt
    assert "<name>xlsx</name>" in provider.system_prompt
    assert all(message.role != "system_synthetic" for message in provider.messages)


def test_skills_commands_list_show_roots_and_prompt(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_skill(home / "skills" / "pdf", "pdf", "PDF", "Extract text from PDFs.")
    _write_skill(
        workspace / ".ouro" / "skills" / "xlsx",
        "xlsx",
        "XLSX",
        "Process spreadsheets.",
    )
    monkeypatch.chdir(workspace)

    list_code = main(["skills", "list", "--home", str(home), "--json"])
    list_payload = json.loads(capsys.readouterr().out)

    assert list_code == 0
    assert [skill["name"] for skill in list_payload["skills"]] == ["pdf", "xlsx"]
    assert {root["source"] for root in list_payload["roots"]} == {"user", "workspace"}

    show_code = main(["skills", "show", "xlsx", "--home", str(home), "--json"])
    show_payload = json.loads(capsys.readouterr().out)

    assert show_code == 0
    assert show_payload["skill"]["name"] == "xlsx"
    assert show_payload["skill"]["content"] == "XLSX instructions."

    roots_code = main(["skills", "roots", "--home", str(home), "--json"])
    roots_payload = json.loads(capsys.readouterr().out)

    assert roots_code == 0
    assert str((home / "skills").resolve()) in {root["path"] for root in roots_payload["roots"]}

    prompt_code = main(["skills", "prompt", "--home", str(home), "--json"])
    prompt_payload = json.loads(capsys.readouterr().out)

    assert prompt_code == 0
    assert prompt_payload["skill_count"] == 2
    assert "<name>pdf</name>" in prompt_payload["prompt"]
    assert "<name>xlsx</name>" in prompt_payload["prompt"]


def test_skills_show_reports_missing_skill(tmp_path, capsys) -> None:
    code = main(["skills", "show", "missing", "--home", str(tmp_path), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 1
    assert payload["status"] == "failed"
    assert payload["error"] == "skill not found: missing"


def test_run_json_reports_provider_config_error(tmp_path, capsys) -> None:
    code = main(
        [
            "run",
            "--json",
            "--home",
            str(tmp_path),
            "--provider",
            "missing",
            "hello",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert code == 2
    assert payload["status"] == "failed"
    assert payload["error_type"] == "ConfigError"
    assert "provider not found" in payload["error"]


def test_provider_check_requires_user_configured_default(tmp_path, capsys) -> None:
    code = main(["provider", "check", "--home", str(tmp_path)])

    captured = capsys.readouterr()

    assert code == 2
    assert "default provider is not configured" in captured.err


def test_provider_check_json_hides_internal_default(tmp_path, capsys) -> None:
    code = main(["provider", "check", "--json", "--home", str(tmp_path)])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert code == 2
    assert payload["status"] == "failed"
    assert "default provider is not configured" in payload["error"]


def test_provider_check_json_reports_provider_config_error(tmp_path, capsys) -> None:
    code = main(
        [
            "provider",
            "check",
            "--json",
            "--home",
            str(tmp_path),
            "missing",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert code == 2
    assert payload["status"] == "failed"
    assert payload["error_type"] == "ConfigError"
    assert "provider not found" in payload["error"]


def test_provider_commands_manage_config_and_runtime_default(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("OURO_TEST_KEY", "secret")

    set_code = main(
        [
            "provider",
            "add",
            "glm",
            "--home",
            str(tmp_path),
            "--type",
            "openai-compatible",
            "--base-url",
            "https://example.test/v1",
            "--api-key-env",
            "OURO_TEST_KEY",
            "--timeout",
            "7",
            "--json",
        ]
    )
    set_payload = json.loads(capsys.readouterr().out)

    assert set_code == 0
    assert set_payload["provider"]["provider_id"] == "glm"
    assert set_payload["provider"]["credential"] == "env:OURO_TEST_KEY"

    endpoint_code = main(
        [
            "provider",
            "endpoint",
            "set",
            "glm",
            "default",
            "--home",
            str(tmp_path),
            "--model",
            "glm-test",
            "--default",
            "--json",
        ]
    )
    endpoint_payload = json.loads(capsys.readouterr().out)
    assert endpoint_code == 0
    assert endpoint_payload["endpoint"]["model_id"] == "glm-test"
    assert endpoint_payload["endpoint"]["default"] is True

    use_code = main(["provider", "use", "glm", "--home", str(tmp_path), "--json"])
    use_payload = json.loads(capsys.readouterr().out)
    assert use_code == 0
    assert use_payload["default"] == "glm"

    list_code = main(["provider", "list", "--home", str(tmp_path), "--json"])
    list_payload = json.loads(capsys.readouterr().out)

    assert list_code == 0
    assert list_payload["default"] == "glm"
    assert {provider["provider_id"] for provider in list_payload["providers"]} == {"glm"}

    show_code = main(["provider", "show", "glm", "--home", str(tmp_path), "--json"])
    show_output = capsys.readouterr().out
    show_payload = json.loads(show_output)

    assert show_code == 0
    assert "secret" not in show_output
    assert show_payload["provider"]["base_url"] == "https://example.test/v1"

    runtime = build_default_runtime(tmp_path)

    provider = runtime.adapter.provider
    assert isinstance(provider, OpenAICompatibleProvider)
    assert runtime.adapter.model.provider_id == "glm"
    assert runtime.adapter.model.model_id == "glm-test"
    assert provider.base_url == "https://example.test/v1"
    assert provider.api_key == "secret"
    assert provider.timeout == 7


def test_provider_endpoint_commands_manage_capabilities(tmp_path, monkeypatch, capsys) -> None:
    reasoning = openai_effort_reasoning(
        supported_efforts=("low", "high"),
        default_effort="high",
    )
    monkeypatch.setenv("OURO_TEST_KEY", "secret")
    assert (
        main(
            [
                "provider",
                "add",
                "glm",
                "--home",
                str(tmp_path),
                "--type",
                "openai-compatible",
                "--api-key-env",
                "OURO_TEST_KEY",
            ]
        )
        == 0
    )
    capsys.readouterr()

    list_code = main(["provider", "list", "--home", str(tmp_path), "--json"])
    list_payload = json.loads(capsys.readouterr().out)
    assert list_code == 0
    assert list_payload["default"] is None

    set_code = main(
        [
            "provider",
            "endpoint",
            "set",
            "glm",
            "reasoning",
            "--home",
            str(tmp_path),
            "--model",
            "glm-reasoning",
            "--base-url",
            "https://example.test/v1",
            "--api-path",
            "/chat/completions",
            "--temperature",
            "0.2",
            "--context-window",
            "200000",
            "--max-output-tokens",
            "16000",
            "--no-tools",
            "--vision",
            "--no-streaming",
            "--structured-output",
            "--reasoning-config",
            json.dumps(reasoning.model_dump(mode="json")),
            "--request-options",
            '{"seed": 7}',
            "--default",
            "--json",
        ]
    )
    set_payload = json.loads(capsys.readouterr().out)

    assert set_code == 0
    endpoint = set_payload["endpoint"]
    assert endpoint["endpoint_id"] == "reasoning"
    assert endpoint["supports_tools"] is False
    assert endpoint["supports_vision"] is True
    assert endpoint["supports_streaming"] is False
    assert endpoint["reasoning"] == reasoning.model_dump(mode="json")

    list_code = main(["provider", "endpoint", "list", "glm", "--home", str(tmp_path), "--json"])
    list_payload = json.loads(capsys.readouterr().out)
    assert list_code == 0
    assert list_payload["default_endpoint"] == "reasoning"

    show_code = main(
        [
            "provider",
            "endpoint",
            "show",
            "glm",
            "reasoning",
            "--home",
            str(tmp_path),
            "--json",
        ]
    )
    show_payload = json.loads(capsys.readouterr().out)
    assert show_code == 0
    assert show_payload["endpoint"]["api_path"] == "/chat/completions"
    assert show_payload["endpoint"]["supports_structured_output"] is True

    use_code = main(["provider", "use", "glm", "--home", str(tmp_path), "--json"])
    use_payload = json.loads(capsys.readouterr().out)
    assert use_code == 0
    assert use_payload["default"] == "glm"

    runtime = build_default_runtime(tmp_path)
    assert runtime.adapter.model.endpoint_id == "reasoning"
    assert runtime.adapter.model.temperature == 0.2
    assert runtime.adapter.model.context_window == 200_000
    assert runtime.adapter.model.max_output_tokens == 16_000
    assert runtime.adapter.model.supports_tools is False
    assert runtime.adapter.model.supports_vision is True
    assert runtime.adapter.model.supports_streaming is False
    assert runtime.adapter.model.supports_structured_output is True
    assert runtime.adapter.model.reasoning == reasoning.capability
    assert runtime.adapter.model.reasoning_request == ReasoningRequest()
    assert runtime.adapter.model.reasoning_resolution.effective == ReasoningRequest(
        mode="enabled", effort="high"
    )
    provider = cast(OpenAICompatibleProvider, runtime.adapter.provider)
    assert provider.endpoint == "/chat/completions"
    assert provider.request_options == {"seed": 7}

    remove_code = main(
        [
            "provider",
            "endpoint",
            "remove",
            "glm",
            "reasoning",
            "--home",
            str(tmp_path),
            "--json",
        ]
    )
    remove_payload = json.loads(capsys.readouterr().out)
    assert remove_code == 0
    assert remove_payload["default_endpoint"] is None
    assert remove_payload["default_provider"] is None


def test_provider_endpoint_reasoning_preset_configures_custom_model(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("CUSTOM_KEY", "secret")
    assert (
        main(
            [
                "provider",
                "add",
                "custom",
                "--home",
                str(tmp_path),
                "--type",
                "openai-compatible",
                "--api-key-env",
                "CUSTOM_KEY",
            ]
        )
        == 0
    )
    capsys.readouterr()

    code = main(
        [
            "provider",
            "endpoint",
            "set",
            "custom",
            "chat",
            "--home",
            str(tmp_path),
            "--model",
            "custom-thinking-model",
            "--base-url",
            "https://example.test/v1",
            "--reasoning-preset",
            "thinking-toggle",
            "--default",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    reasoning = payload["endpoint"]["reasoning"]
    assert reasoning["capability"]["activation"] == "toggle"
    assert reasoning["profile"]["variants"]["enabled"] == {"thinking": {"type": "enabled"}}


def test_provider_check_cannot_target_internal_faux(tmp_path, capsys) -> None:
    code = main(
        [
            "provider",
            "check",
            "faux",
            "--endpoint",
            "faux",
            "--home",
            str(tmp_path),
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)

    assert code == 2
    assert payload["status"] == "failed"
    assert "provider not found: faux" in payload["error"]


def test_provider_remove_clears_user_visible_default(tmp_path, capsys) -> None:
    assert (
        main(
            [
                "provider",
                "add",
                "local",
                "--type",
                "openai",
                "--home",
                str(tmp_path),
                "--default",
            ]
        )
        == 0
    )
    capsys.readouterr()

    remove_code = main(["provider", "remove", "local", "--home", str(tmp_path), "--json"])
    remove_payload = json.loads(capsys.readouterr().out)

    assert remove_code == 0
    assert remove_payload["default"] is None


def _write_skill(root, skill_name: str, name: str, description: str) -> None:
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        f"""---
name: {skill_name}
description: {description}
---
{name} instructions.""",
        encoding="utf-8",
    )


async def test_interactive_streams_model_reasoning_deltas_in_italics(tmp_path) -> None:
    runtime = _runtime(
        tmp_path,
        [
            [
                ProviderStreamEvent(type="reasoning_delta", reasoning_content="Let me think"),
                ProviderStreamEvent(type="reasoning_delta", reasoning_content="..."),
                ProviderStreamEvent(type="text_delta", text="Answer"),
                ProviderStreamEvent(type="done"),
            ],
        ],
    )
    inputs = iter(["hello", "/exit"])
    outputs: list[str] = []

    code = await run_interactive(
        runtime=runtime,
        input_func=lambda _prompt: next(inputs),
        output_func=outputs.append,
    )

    assert code == 0
    all_output = "".join(outputs)
    assert "\x1b[2;3m" in all_output
    assert "\x1b[0m" in all_output
    assert "Let me think..." in all_output
    assert "Answer" in all_output


async def test_interactive_prints_tool_calls(tmp_path) -> None:
    target = tmp_path / "written.txt"
    runtime = _runtime(
        tmp_path,
        [
            [
                ProviderStreamEvent(
                    type="tool_call",
                    tool_call={
                        "id": "call_1",
                        "name": "Write",
                        "arguments": {"path": str(target), "content": "hello"},
                    },
                ),
                ProviderStreamEvent(type="done"),
            ],
            [
                ProviderStreamEvent(type="text_delta", text="Done."),
                ProviderStreamEvent(type="done"),
            ],
        ],
    )
    inputs = iter(["write", "y", "/exit"])
    outputs: list[str] = []

    code = await run_interactive(
        runtime=runtime,
        input_func=lambda _prompt: next(inputs),
        output_func=outputs.append,
    )

    assert code == 0
    tool_outputs = [output for output in outputs if "Write · completed" in output]
    assert len(tool_outputs) == 1
    assert tool_outputs[0] == "● Write · completed\n"
    approval_outputs = [output for output in outputs if output.startswith("Approval required:")]
    assert len(approval_outputs) == 1
    assert '"path"' in approval_outputs[0] and '"content"' in approval_outputs[0]


def test_provider_add_api_path_defaults_and_endpoint_inherits(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("OURO_TEST_KEY", "secret")

    assert (
        main(
            [
                "provider",
                "add",
                "glm",
                "--home",
                str(tmp_path),
                "--type",
                "openai-compatible",
                "--base-url",
                "https://example.test/v1",
                "--api-path",
                "/responses/chat",
                "--api-key-env",
                "OURO_TEST_KEY",
            ]
        )
        == 0
    )

    assert (
        main(
            [
                "provider",
                "endpoint",
                "set",
                "glm",
                "default",
                "--home",
                str(tmp_path),
                "--model",
                "glm-test",
                "--base-url",
                "https://example.test/v1",
                "--default",
            ]
        )
        == 0
    )

    # Create an endpoint without --api-path; it should inherit the provider default.
    assert (
        main(
            [
                "provider",
                "endpoint",
                "set",
                "glm",
                "reasoning",
                "--home",
                str(tmp_path),
                "--model",
                "glm-reasoning",
                "--base-url",
                "https://reasoning.example.test/v1",
                "--default",
            ]
        )
        == 0
    )

    assert main(["provider", "use", "glm", "--home", str(tmp_path)]) == 0

    runtime = build_default_runtime(tmp_path)
    provider = cast(OpenAICompatibleProvider, runtime.adapter.provider)
    assert provider.endpoint == "/responses/chat"


def test_provider_endpoint_set_api_path_overrides_provider_default(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OURO_TEST_KEY", "secret")

    assert (
        main(
            [
                "provider",
                "add",
                "glm",
                "--home",
                str(tmp_path),
                "--type",
                "openai-compatible",
                "--base-url",
                "https://example.test/v1",
                "--api-path",
                "/responses/chat",
                "--api-key-env",
                "OURO_TEST_KEY",
            ]
        )
        == 0
    )

    assert (
        main(
            [
                "provider",
                "endpoint",
                "set",
                "glm",
                "default",
                "--home",
                str(tmp_path),
                "--model",
                "glm-test",
                "--base-url",
                "https://example.test/v1",
                "--default",
            ]
        )
        == 0
    )

    assert (
        main(
            [
                "provider",
                "endpoint",
                "set",
                "glm",
                "chat",
                "--home",
                str(tmp_path),
                "--model",
                "glm-chat",
                "--base-url",
                "https://chat.example.test/v1",
                "--api-path",
                "/chat/completions",
                "--default",
            ]
        )
        == 0
    )

    assert main(["provider", "use", "glm", "--home", str(tmp_path)]) == 0

    runtime = build_default_runtime(tmp_path)
    provider = cast(OpenAICompatibleProvider, runtime.adapter.provider)
    assert provider.endpoint == "/chat/completions"


def test_provider_add_base_url_without_model_creates_empty_provider(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("OURO_TEST_KEY", "secret")

    code = main(
        [
            "provider",
            "add",
            "glm",
            "--home",
            str(tmp_path),
            "--type",
            "openai-compatible",
            "--base-url",
            "https://example.test/v1",
            "--api-key-env",
            "OURO_TEST_KEY",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["provider"]["provider_id"] == "glm"
    assert payload["provider"]["base_url"] == "https://example.test/v1"
    assert payload["provider"]["endpoint_count"] == 0


def test_provider_add_and_update_configure_validated_retry_policy(tmp_path, capsys) -> None:
    code = main(
        [
            "provider",
            "add",
            "glm",
            "--home",
            str(tmp_path),
            "--type",
            "openai-compatible",
            "--base-url",
            "https://example.test/v1",
            "--retry-attempts",
            "3",
            "--retry-initial-backoff",
            "0.25",
            "--retry-max-backoff",
            "2",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    provider = ConfigurationFacade(tmp_path).provider("glm")

    assert code == 0
    assert payload["provider"]["provider_id"] == "glm"
    assert provider is not None
    assert provider.retry_policy.max_attempts == 3
    assert provider.retry_policy.initial_backoff_seconds == 0.25
    assert provider.retry_policy.max_backoff_seconds == 2

    code = main(
        [
            "provider",
            "update",
            "glm",
            "--home",
            str(tmp_path),
            "--retry-attempts",
            "0",
            "--json",
        ]
    )
    error = json.loads(capsys.readouterr().out)

    assert code == 2
    assert error["error_type"] == "ConfigError"
    assert ConfigurationFacade(tmp_path).provider("glm") == provider


def test_provider_endpoint_set_default_updates_existing_endpoint(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OURO_TEST_KEY", "secret")

    assert (
        main(
            [
                "provider",
                "add",
                "glm",
                "--home",
                str(tmp_path),
                "--type",
                "openai-compatible",
                "--api-key-env",
                "OURO_TEST_KEY",
            ]
        )
        == 0
    )

    assert (
        main(
            [
                "provider",
                "endpoint",
                "set",
                "glm",
                "first",
                "--home",
                str(tmp_path),
                "--model",
                "first-model",
                "--base-url",
                "https://first.example.test/v1",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "provider",
                "endpoint",
                "set",
                "glm",
                "second",
                "--home",
                str(tmp_path),
                "--model",
                "second-model",
                "--base-url",
                "https://second.example.test/v1",
                "--default",
            ]
        )
        == 0
    )

    assert main(["provider", "use", "glm", "--home", str(tmp_path)]) == 0

    runtime = build_default_runtime(tmp_path)
    assert runtime.adapter.model.endpoint_id == "second"
    provider = cast(OpenAICompatibleProvider, runtime.adapter.provider)
    assert provider.base_url == "https://second.example.test/v1"


def test_run_print_mode_shows_reasoning_deltas_in_dim_italics(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    runtime = _runtime(
        tmp_path,
        [
            [
                ProviderStreamEvent(type="reasoning_delta", reasoning_content="Let me think"),
                ProviderStreamEvent(type="reasoning_delta", reasoning_content="..."),
                ProviderStreamEvent(type="text_delta", text="Answer"),
                ProviderStreamEvent(type="done"),
            ]
        ],
    )

    def fake_runtime(
        home=None,
        *,
        provider_options=None,
        system_prompt=None,
        event_sink=None,
        session_backend=None,
    ):
        del session_backend
        if event_sink is not None:
            runtime.add_event_sink(event_sink)
        return runtime

    monkeypatch.setattr("ouro.cli.run.build_default_runtime", fake_runtime)

    code = main(["run", "--print", "--home", str(tmp_path), "hello"])
    captured = capsys.readouterr()

    assert code == 0
    assert "\x1b[2;3m" in captured.out
    assert "\x1b[0m" in captured.out
    assert "Let me think..." in captured.out
    assert "Answer" in captured.out


def test_runtime_composes_configured_compaction_endpoint(tmp_path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "compaction": {
                    "model": {
                        "provider_id": "glm",
                        "endpoint_id": "summary",
                        "temperature": 0.1,
                    }
                },
                "providers": {
                    "default": "faux",
                    "items": {
                        "glm": {
                            "provider_id": "glm",
                            "provider_type": "openai-compatible",
                            "protocol": "openai-chat-completions",
                            "default_endpoint": "summary",
                            "endpoints": {
                                "summary": {
                                    "model_id": "summary-model",
                                    "base_url": "https://summary.example.test/v1",
                                }
                            },
                        }
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    runtime = build_default_runtime(tmp_path)
    strategy = runtime.compaction_strategies.get("builtin.llm_summary")

    assert isinstance(strategy, LLMSummaryCompactionStrategy)
    assert runtime.adapter.model.provider_id == "faux"
    assert strategy.configured_adapter is not None
    assert strategy.configured_adapter.model.provider_id == "glm"
    assert strategy.configured_adapter.model.endpoint_id == "summary"


def test_missing_compaction_endpoint_does_not_break_runtime_composition(tmp_path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "compaction": {
                    "model": {
                        "provider_id": "missing",
                        "endpoint_id": "missing",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    runtime = build_default_runtime(tmp_path)
    strategy = runtime.compaction_strategies.get("builtin.llm_summary")

    assert isinstance(strategy, LLMSummaryCompactionStrategy)
    assert strategy.configured_adapter is None
    assert "provider not found" in (strategy.configured_adapter_error or "")

import json
import sys
from pathlib import Path
from typing import Any, cast

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
from ouro.core.provider import ModelMetadata, ProviderStreamEvent
from ouro.core.reasoning import ReasoningRequest
from ouro.mcp import MCPConfigStore, MCPServerConfig
from ouro.prompts import DEFAULT_SYSTEM_PROMPT
from ouro.providers import FauxProvider, OpenAICompatibleProvider
from ouro.sessions import LocalSessionStore
from ouro.tools import ToolRegistry, builtin_tools

MCP_ECHO_SERVER = Path(__file__).parents[1] / "mcp" / "fixtures" / "echo_server.py"


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


async def test_run_print_reports_failed_runtime_result(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    def fake_runtime(home=None, *, provider_options=None, system_prompt=None):
        del provider_options, system_prompt
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
    assert runtime.tools.names() == ["AskUser", "Bash", "Edit", "Read", "Write"]
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
    assert runtime.tools.names() == ["AskUser", "Bash", "Edit", "Read", "Write"]


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
    inputs = iter(["hello", "/sessions", "/models", "/model", "/export", "/new", "/exit"])
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
    assert runtime.tools.names() == ["AskUser", "Bash", "Edit", "Read", "Write"]


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
                        "arguments": {"question": "Which file?"},
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
    assert "Question: Which file?" in outputs
    assert "Using notes.md." in outputs


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
                        "arguments": {"question": "Which file?"},
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
    assert "Question: Which file?" in outputs
    assert "Run aborted: User aborted" in outputs


def test_main_without_subcommand_enters_interactive(monkeypatch, tmp_path) -> None:
    calls = []

    async def fake_interactive(*, home, system_prompt):
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

    def fake_runtime(home=None, *, provider_options=None, system_prompt=None, event_sink=None):
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

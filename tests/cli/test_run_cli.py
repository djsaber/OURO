import json
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from ouro.application.configuration.facade import ConfigurationFacade
from ouro.application.context.summary import LLMSummaryCompactionStrategy
from ouro.application.runtime.environment import RuntimeEnvironment
from ouro.application.runtime.service import RuntimeService
from ouro.cli.main import main
from ouro.cli.run import RuntimeProviderOptions, build_default_runtime, run_json, run_print
from ouro.config import ProviderConfigManager, openai_chat_completions_effort_reasoning
from ouro.core.adapter import RuntimeAdapter
from ouro.core.provider import ModelMetadata, ProviderStreamEvent
from ouro.core.reasoning import ReasoningRequest
from ouro.mcp import MCPConfigStore, MCPServerConfigUpdate
from ouro.prompts import DEFAULT_SYSTEM_PROMPT
from ouro.providers import FauxProvider, OpenAIChatCompletionsProvider
from ouro.sessions import LocalSessionStore
from ouro.tools import ToolRegistry, builtin_tools
from ouro.tools.builtin.shell import detect_command_shell

MCP_ECHO_SERVER = Path(__file__).parents[1] / "mcp" / "fixtures" / "echo_server.py"


def _only_provider_id(home: Path) -> str:
    providers = ProviderConfigManager(home).snapshot(include_builtin=False).providers
    assert len(providers) == 1
    return providers[0].provider_id


def _endpoint_id_for_model(home: Path, provider_id: str, model_id: str) -> str:
    provider = ProviderConfigManager(home).get_provider(provider_id, include_builtin=False)
    assert provider is not None
    endpoint = next(item for item in provider.endpoints if item.model_id == model_id)
    return endpoint.endpoint_id


def _expected_builtin_tool_names() -> list[str]:
    shell = detect_command_shell()
    names = ["AskUser", "Edit", "Glob", "Grep", "Read", "TodoList", "ViewImage", "Write"]
    if shell is not None:
        names.append(shell.tool_name)
    return sorted(names)


class CapturingProvider:
    provider_id = "capture"

    def __init__(self) -> None:
        self.messages: list[Any] = []
        self.system_prompt = ""

    async def stream(self, *, model, system_prompt="", messages, tools=None, request_context=None):
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
        environment=RuntimeEnvironment.local(tmp_path),
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
    assert "events" not in payload
    assert payload["had_tool_error"] is False
    assert payload["error_code"] is None
    assert payload["startup_diagnostics"] == []


async def test_run_print_reports_invalid_live_prompt_configuration(tmp_path, capsys, monkeypatch):
    runtime = build_default_runtime(tmp_path)
    path = tmp_path / "config" / "prompts.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{broken-json", encoding="utf-8")
    monkeypatch.setattr("ouro.cli.run.build_default_runtime", lambda *args, **kwargs: runtime)

    code = await run_print("hello", home=tmp_path)

    captured = capsys.readouterr()
    assert code == 4
    assert captured.out == ""
    assert "Cannot prepare system instructions" in captured.err
    assert "prompts.json" in captured.err
    assert "Run stopped with status: paused" in captured.err


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


async def test_run_print_does_not_wait_for_configured_mcp_tool(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    config = MCPConfigStore(tmp_path).update(
        None,
        MCPServerConfigUpdate(
            name="Echo",
            command=sys.executable,
            args=["-c", "raise SystemExit(2)"],
            auto_start=True,
            permission_mode="auto",
            timeout_seconds=5,
        ),
    )
    runtime = _runtime(
        tmp_path,
        [
            [
                ProviderStreamEvent.from_tool_call(
                    {
                        "id": "mcp_call",
                        "name": f"{config.server_id}__echo",
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
    assert tool_message.tool_result.tool_name == f"{config.server_id}__echo"
    assert tool_message.tool_result.is_error is True
    assert f"unknown tool: {config.server_id}__echo" in tool_message.text_content


async def test_run_json_composes_mcp_without_corrupting_machine_output(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    config = MCPConfigStore(tmp_path).update(
        None,
        MCPServerConfigUpdate(
            name="Echo",
            command=sys.executable,
            args=[str(MCP_ECHO_SERVER), "--transport", "stdio"],
            auto_start=True,
            permission_mode="auto",
            timeout_seconds=5,
        ),
    )
    runtime = _runtime(
        tmp_path,
        [
            [
                ProviderStreamEvent.from_tool_call(
                    {
                        "id": "json_mcp_call",
                        "name": f"{config.server_id}__echo",
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
    MCPConfigStore(tmp_path).update(
        None,
        MCPServerConfigUpdate(
            name="Broken",
            command=sys.executable,
            args=["-c", "raise SystemExit(2)"],
            auto_start=True,
            auto_reconnect=False,
            timeout_seconds=1,
        ),
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
    assert captured.err == ""


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
            "--image-input",
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
    assert options.supports_image_input is True
    assert options.supports_streaming is False
    assert options.supports_structured_output is True
    assert options.reasoning_request == ReasoningRequest(mode="enabled", effort="high")


def test_run_mode_rejects_reasoning_controls_on_unsupported_endpoint(tmp_path, capsys) -> None:
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
    assert "endpoint does not support reasoning controls" in payload["error"]


def test_build_runtime_with_openai_chat_completions_provider_options(
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
    assert isinstance(provider, OpenAIChatCompletionsProvider)
    assert runtime.adapter.model.model_id == "glm-test"
    assert runtime.adapter.model.provider_id == "glm"
    assert provider.base_url == "https://example.test/v1"
    assert provider.api_key == "secret"
    assert provider.timeout == 12.0


def test_build_runtime_reads_local_config_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OURO_TEST_KEY", "secret")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "providers.json").write_text(
        json.dumps(
            {
                "providers": {
                    "default": "provider_generated",
                    "items": {
                        "provider_generated": {
                            "display_name": "Configured",
                            "provider_type": "custom",
                            "protocol": "openai-chat-completions",
                            "credential_ref": "env:OURO_TEST_KEY",
                            "timeout_seconds": 9.0,
                            "default_endpoint": "endpoint_generated",
                            "endpoints": {
                                "endpoint_generated": {
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
    assert isinstance(provider, OpenAIChatCompletionsProvider)
    assert runtime.adapter.model.model_id == "configured-model"
    assert runtime.adapter.model.provider_id == "provider_generated"
    assert provider.base_url == "https://configured.test/v1"
    assert provider.timeout == 9.0


def test_config_prompt_command_persists_system_prompt(tmp_path, capsys) -> None:
    set_code = main(
        [
            "config",
            "prompt",
            "--home",
            str(tmp_path),
            "add",
            "replacement",
            "--enabled",
            "--text",
            "Custom prompt.",
            "--json",
        ]
    )
    set_payload = json.loads(capsys.readouterr().out)

    assert set_code == 0
    assert set_payload["effective_system_prompt"] == "Custom prompt."
    assert build_default_runtime(tmp_path).system_prompt == "Custom prompt."

    show_code = main(["config", "prompt", "--home", str(tmp_path), "--json"])
    show_payload = json.loads(capsys.readouterr().out)

    assert show_code == 0
    assert show_payload["effective_system_prompt"] == "Custom prompt."

    clear_code = main(
        ["config", "prompt", "--home", str(tmp_path), "disable", "replacement", "1", "--json"]
    )
    clear_payload = json.loads(capsys.readouterr().out)

    assert clear_code == 0
    assert clear_payload["effective_system_prompt"] == DEFAULT_SYSTEM_PROMPT
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

    def fake_adapter(_options, *, plugin_provider=None):
        return RuntimeAdapter(
            provider,
            ModelMetadata(model_id="capture", provider_id="capture"),
        )

    monkeypatch.setattr("ouro.local_application.build_runtime_adapter", fake_adapter)
    runtime = build_default_runtime(home)

    assert runtime.observers.registered() == []

    await runtime.run("help me with files")

    assert provider.system_prompt.startswith(DEFAULT_SYSTEM_PROMPT.strip())
    assert "<skills_instructions>" in provider.system_prompt
    assert "<name>pdf</name>" in provider.system_prompt
    assert "<name>xlsx</name>" in provider.system_prompt
    assert all(message.role != "system" for message in provider.messages)


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
    assert show_payload["skill"]["content"] == (
        workspace / ".ouro" / "skills" / "xlsx" / "SKILL.md"
    ).read_text(encoding="utf-8")

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
            "custom",
            "--protocol",
            "openai-chat-completions",
            "--base-url",
            "https://example.test/v1",
            "--api-key-env",
            "OURO_TEST_KEY",
            "--timeout",
            "7",
            "--provider-options",
            '{"nested":{"password":"body-secret"},"seed":1,"top_p":0.8}',
            "--json",
        ]
    )
    set_payload = json.loads(capsys.readouterr().out)

    assert set_code == 0
    provider_id = set_payload["provider"]["provider_id"]
    assert provider_id.startswith("provider_")
    assert set_payload["provider"]["display_name"] == "glm"
    assert set_payload["provider"]["credential"] == "env:OURO_TEST_KEY"
    assert set_payload["provider"]["provider_options"] == {
        "nested": {"password": "***"},
        "seed": 1,
        "top_p": 0.8,
    }

    endpoint_code = main(
        [
            "provider",
            "endpoint",
            "add",
            provider_id,
            "--home",
            str(tmp_path),
            "--model",
            "glm-test",
            "--request-options",
            '{"seed":7}',
            "--default",
            "--json",
        ]
    )
    endpoint_payload = json.loads(capsys.readouterr().out)
    assert endpoint_code == 0
    assert endpoint_payload["endpoint"]["model_id"] == "glm-test"
    assert endpoint_payload["endpoint"]["default"] is True
    endpoint_id = endpoint_payload["endpoint"]["endpoint_id"]
    assert endpoint_id.startswith("endpoint_")

    use_code = main(["provider", "use", provider_id, "--home", str(tmp_path), "--json"])
    use_payload = json.loads(capsys.readouterr().out)
    assert use_code == 0
    assert use_payload["default"] == provider_id

    list_code = main(["provider", "list", "--home", str(tmp_path), "--json"])
    list_payload = json.loads(capsys.readouterr().out)

    assert list_code == 0
    assert list_payload["default"] == provider_id
    assert {provider["provider_id"] for provider in list_payload["providers"]} == {provider_id}

    show_code = main(["provider", "show", provider_id, "--home", str(tmp_path), "--json"])
    show_output = capsys.readouterr().out
    show_payload = json.loads(show_output)

    assert show_code == 0
    assert "secret" not in show_output
    assert show_payload["provider"]["base_url"] == "https://example.test/v1"
    assert show_payload["provider"]["provider_options"]["top_p"] == 0.8

    runtime = build_default_runtime(tmp_path)

    provider = runtime.adapter.provider
    assert isinstance(provider, OpenAIChatCompletionsProvider)
    assert runtime.adapter.model.provider_id == provider_id
    assert runtime.adapter.model.endpoint_id == endpoint_id
    assert runtime.adapter.model.model_id == "glm-test"
    assert provider.base_url == "https://example.test/v1"
    assert provider.api_key == "secret"
    assert provider.timeout == 7
    assert provider.request_options == {
        "nested": {"password": "body-secret"},
        "seed": 7,
        "top_p": 0.8,
    }

    update_code = main(
        [
            "provider",
            "update",
            provider_id,
            "--home",
            str(tmp_path),
            "--provider-options",
            "{}",
            "--json",
        ]
    )
    update_payload = json.loads(capsys.readouterr().out)
    assert update_code == 0
    assert update_payload["provider"]["provider_options"] == {}


def test_provider_endpoint_commands_manage_capabilities(tmp_path, monkeypatch, capsys) -> None:
    reasoning = openai_chat_completions_effort_reasoning(
        supported_efforts=("low", "high"),
        default=ReasoningRequest(mode="enabled", effort="high"),
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
                "custom",
                "--protocol",
                "openai-chat-completions",
                "--api-key-env",
                "OURO_TEST_KEY",
            ]
        )
        == 0
    )
    capsys.readouterr()
    provider_id = _only_provider_id(tmp_path)

    list_code = main(["provider", "list", "--home", str(tmp_path), "--json"])
    list_payload = json.loads(capsys.readouterr().out)
    assert list_code == 0
    assert list_payload["default"] is None

    set_code = main(
        [
            "provider",
            "endpoint",
            "add",
            provider_id,
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
            "--image-input",
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
    endpoint_id = endpoint["endpoint_id"]
    assert endpoint_id.startswith("endpoint_")
    assert endpoint["supports_tools"] is False
    assert endpoint["supports_image_input"] is True
    assert endpoint["supports_streaming"] is False
    assert endpoint["reasoning"] == reasoning.model_dump(mode="json")

    list_code = main(
        ["provider", "endpoint", "list", provider_id, "--home", str(tmp_path), "--json"]
    )
    list_payload = json.loads(capsys.readouterr().out)
    assert list_code == 0
    assert list_payload["default_endpoint"] == endpoint_id

    show_code = main(
        [
            "provider",
            "endpoint",
            "show",
            provider_id,
            endpoint_id,
            "--home",
            str(tmp_path),
            "--json",
        ]
    )
    show_payload = json.loads(capsys.readouterr().out)
    assert show_code == 0
    assert show_payload["endpoint"]["api_path"] == "/chat/completions"
    assert show_payload["endpoint"]["supports_structured_output"] is True

    use_code = main(["provider", "use", provider_id, "--home", str(tmp_path), "--json"])
    use_payload = json.loads(capsys.readouterr().out)
    assert use_code == 0
    assert use_payload["default"] == provider_id

    runtime = build_default_runtime(tmp_path)
    assert runtime.adapter.model.endpoint_id == endpoint_id
    assert runtime.adapter.model.temperature == 0.2
    assert runtime.adapter.model.context_window == 200_000
    assert runtime.adapter.model.max_output_tokens == 16_000
    assert runtime.adapter.model.supports_tools is False
    assert runtime.adapter.model.supports_image_input is True
    assert runtime.adapter.model.supports_streaming is False
    assert runtime.adapter.model.supports_structured_output is True
    assert runtime.adapter.model.reasoning == reasoning.capability
    assert runtime.adapter.model.reasoning_request == ReasoningRequest()
    assert runtime.adapter.model.reasoning_resolution.effective == ReasoningRequest(
        mode="enabled", effort="high"
    )
    provider = cast(OpenAIChatCompletionsProvider, runtime.adapter.provider)
    assert provider.api_path == "/chat/completions"
    assert provider.request_options == {"seed": 7}

    remove_code = main(
        [
            "provider",
            "endpoint",
            "remove",
            provider_id,
            endpoint_id,
            "--home",
            str(tmp_path),
            "--json",
        ]
    )
    remove_payload = json.loads(capsys.readouterr().out)
    assert remove_code != 0
    assert "cannot remove the default endpoint" in remove_payload["error"]
    assert ConfigurationFacade(tmp_path).provider_catalog().default_provider_id == provider_id


@pytest.mark.parametrize(
    ("protocol", "preset", "enabled_payload"),
    [
        ("openai-chat-completions", "thinking-toggle", {"thinking": {"type": "enabled"}}),
        ("openai-responses", "openai-responses-effort", {"reasoning": {"effort": "medium"}}),
        (
            "anthropic-messages",
            "anthropic-messages-adaptive-effort",
            {"thinking": {"type": "adaptive"}},
        ),
    ],
)
def test_provider_endpoint_reasoning_effort_preset_configures_custom_model(
    tmp_path,
    monkeypatch,
    capsys,
    protocol,
    preset,
    enabled_payload,
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
                "custom",
                "--api-key-env",
                "CUSTOM_KEY",
                "--protocol",
                protocol,
            ]
        )
        == 0
    )
    capsys.readouterr()
    provider_id = _only_provider_id(tmp_path)

    code = main(
        [
            "provider",
            "endpoint",
            "add",
            provider_id,
            "--home",
            str(tmp_path),
            "--model",
            "custom-thinking-model",
            "--base-url",
            "https://example.test/v1",
            "--reasoning-preset",
            preset,
            "--default",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    reasoning = payload["endpoint"]["reasoning"]
    endpoint_id = payload["endpoint"]["endpoint_id"]
    assert reasoning["capability"]["activation"] == "toggle"
    assert reasoning["profile"]["effort"]["enabled"] == enabled_payload
    assert (
        ConfigurationFacade(tmp_path)
        .endpoint(provider_id, endpoint_id)
        .reasoning.model_dump(mode="json", exclude_none=True)
        == reasoning
    )


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


def test_provider_remove_rejects_user_visible_default(tmp_path, capsys) -> None:
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
                "--json",
            ]
        )
        == 0
    )
    provider_id = json.loads(capsys.readouterr().out)["provider"]["provider_id"]

    disable_code = main(
        [
            "provider",
            "update",
            provider_id,
            "--no-enabled",
            "--home",
            str(tmp_path),
            "--json",
        ]
    )
    disable_payload = json.loads(capsys.readouterr().out)
    assert disable_code != 0
    assert "cannot disable the default provider" in disable_payload["error"]

    remove_code = main(["provider", "remove", provider_id, "--home", str(tmp_path), "--json"])
    remove_payload = json.loads(capsys.readouterr().out)

    assert remove_code != 0
    assert "cannot remove the default provider" in remove_payload["error"]
    assert ConfigurationFacade(tmp_path).provider_catalog().default_provider_id == provider_id


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
                "custom",
                "--protocol",
                "openai-chat-completions",
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
    provider_id = _only_provider_id(tmp_path)

    assert (
        main(
            [
                "provider",
                "endpoint",
                "add",
                provider_id,
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
                "add",
                provider_id,
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

    assert main(["provider", "use", provider_id, "--home", str(tmp_path)]) == 0

    runtime = build_default_runtime(tmp_path)
    provider = cast(OpenAIChatCompletionsProvider, runtime.adapter.provider)
    assert provider.api_path == "/responses/chat"


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
                "custom",
                "--protocol",
                "openai-chat-completions",
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
    provider_id = _only_provider_id(tmp_path)

    assert (
        main(
            [
                "provider",
                "endpoint",
                "add",
                provider_id,
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
                "add",
                provider_id,
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

    assert main(["provider", "use", provider_id, "--home", str(tmp_path)]) == 0

    runtime = build_default_runtime(tmp_path)
    provider = cast(OpenAIChatCompletionsProvider, runtime.adapter.provider)
    assert provider.api_path == "/chat/completions"


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
            "custom",
            "--protocol",
            "openai-chat-completions",
            "--base-url",
            "https://example.test/v1",
            "--api-key-env",
            "OURO_TEST_KEY",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    provider_id = payload["provider"]["provider_id"]
    assert provider_id.startswith("provider_")
    assert payload["provider"]["display_name"] == "glm"
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
            "custom",
            "--protocol",
            "openai-chat-completions",
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
    provider_id = payload["provider"]["provider_id"]
    provider = ConfigurationFacade(tmp_path).provider(provider_id)

    assert code == 0
    assert provider_id.startswith("provider_")
    assert provider is not None
    assert provider.retry_policy.max_attempts == 3
    assert provider.retry_policy.initial_backoff_seconds == 0.25
    assert provider.retry_policy.max_backoff_seconds == 2

    code = main(
        [
            "provider",
            "update",
            provider_id,
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
    assert ConfigurationFacade(tmp_path).provider(provider_id) == provider


def test_provider_endpoint_add_default_selects_generated_endpoint(
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
                "custom",
                "--protocol",
                "openai-chat-completions",
                "--api-key-env",
                "OURO_TEST_KEY",
            ]
        )
        == 0
    )
    provider_id = _only_provider_id(tmp_path)

    assert (
        main(
            [
                "provider",
                "endpoint",
                "add",
                provider_id,
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
                "add",
                provider_id,
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
    second_endpoint_id = _endpoint_id_for_model(tmp_path, provider_id, "second-model")

    assert main(["provider", "use", provider_id, "--home", str(tmp_path)]) == 0

    runtime = build_default_runtime(tmp_path)
    assert runtime.adapter.model.endpoint_id == second_endpoint_id
    provider = cast(OpenAIChatCompletionsProvider, runtime.adapter.provider)
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
    (config_dir / "providers.json").write_text(
        json.dumps(
            {
                "providers": {
                    "default": "faux",
                    "items": {
                        "provider_generated": {
                            "display_name": "GLM",
                            "provider_type": "custom",
                            "protocol": "openai-chat-completions",
                            "default_endpoint": "endpoint_generated",
                            "endpoints": {
                                "endpoint_generated": {
                                    "display_name": "Summary",
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

    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "compaction": {
                    "model": {
                        "provider_id": "provider_generated",
                        "endpoint_id": "endpoint_generated",
                        "temperature": 0.1,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    runtime = build_default_runtime(tmp_path)
    strategy = runtime.compaction_strategies.get("builtin.llm_summary")

    assert isinstance(strategy, LLMSummaryCompactionStrategy)
    assert runtime.adapter.model.provider_id == "faux"
    assert strategy.configured_adapter is not None
    assert strategy.configured_adapter.model.provider_id == "provider_generated"
    assert strategy.configured_adapter.model.endpoint_id == "endpoint_generated"


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

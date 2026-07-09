import json

from ouro.application.runtime import RuntimeService
from ouro.cli.interactive import run_interactive
from ouro.cli.main import main
from ouro.cli.run import RuntimeProviderOptions, build_default_runtime, run_print
from ouro.core.adapter import RuntimeAdapter
from ouro.providers import (
    FauxProvider,
    ModelMetadata,
    OpenAICompatibleProvider,
    ProviderStreamEvent,
)
from ouro.sessions import LocalSessionStore
from ouro.tools import ToolRegistry, builtin_tools


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
    inputs = iter(["hello", "/sessions", "/model", "/export", "/new", "/exit"])
    outputs: list[str] = []

    async_code = await run_interactive(
        home=tmp_path,
        input_func=lambda _prompt: next(inputs),
        output_func=outputs.append,
    )

    assert async_code == 0
    assert "Hello from OURO." in outputs
    assert any(output.startswith("Sessions:") for output in outputs)
    assert "faux/faux" in outputs
    assert any(str(tmp_path) in output for output in outputs)
    assert "Started a new session." in outputs


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
    assert "Approval required: Write" in outputs
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
    assert "Approval required: Write" in outputs
    assert "Run aborted: User aborted" in outputs


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

    async def fake_interactive(*, home, provider_options, system_prompt):
        calls.append((home, provider_options.provider, system_prompt))
        return 0

    monkeypatch.setattr("ouro.cli.main.run_interactive", fake_interactive)

    code = main(["--home", str(tmp_path), "--provider", "faux", "--system-prompt", "Custom"])

    assert code == 0
    assert calls == [(str(tmp_path), "faux", "Custom")]


def test_run_mode_passes_system_prompt_override(monkeypatch) -> None:
    calls = []

    async def fake_run_print(prompt, **kwargs):
        calls.append((prompt, kwargs["system_prompt"]))
        return 0

    monkeypatch.setattr("ouro.cli.main.run_print", fake_run_print)

    code = main(["run", "--system-prompt", "Custom", "hello"])

    assert code == 0
    assert calls == [("hello", "Custom")]


def test_build_runtime_with_openai_compatible_provider_options(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OURO_TEST_KEY", "secret")

    runtime = build_default_runtime(
        tmp_path,
        provider_options=RuntimeProviderOptions(
            provider="openai-compatible",
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
                "provider": {
                    "kind": "openai-compatible",
                    "provider_id": "configured",
                    "model": "configured-model",
                    "base_url": "https://configured.test/v1",
                    "api_key_env": "OURO_TEST_KEY",
                    "timeout": 9.0,
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


def test_run_json_reports_provider_config_error(tmp_path, capsys) -> None:
    code = main(
        [
            "run",
            "--json",
            "--home",
            str(tmp_path),
            "--provider",
            "openai-compatible",
            "hello",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert code == 2
    assert payload["status"] == "failed"
    assert payload["error_type"] == "ConfigError"
    assert "--base-url" in payload["error"]


def test_config_check_faux_provider_outputs_status(tmp_path, capsys) -> None:
    code = main(["config", "check", "--home", str(tmp_path)])

    captured = capsys.readouterr()

    assert code == 0
    assert captured.out.strip() == "Provider check OK: faux/faux"


def test_config_check_json_outputs_provider_result(tmp_path, capsys) -> None:
    code = main(["config", "check", "--json", "--home", str(tmp_path)])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert code == 0
    assert payload["status"] == "ok"
    assert payload["ok"] is True
    assert payload["provider_id"] == "faux"
    assert payload["model_id"] == "faux"


def test_config_check_json_reports_provider_config_error(tmp_path, capsys) -> None:
    code = main(
        [
            "config",
            "check",
            "--json",
            "--home",
            str(tmp_path),
            "--provider",
            "openai-compatible",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert code == 2
    assert payload["status"] == "failed"
    assert payload["error_type"] == "ConfigError"
    assert "--base-url" in payload["error"]


def test_provider_commands_manage_config_and_runtime_default(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("OURO_TEST_KEY", "secret")

    set_code = main(
        [
            "provider",
            "set",
            "glm",
            "--home",
            str(tmp_path),
            "--kind",
            "openai-compatible",
            "--model",
            "glm-test",
            "--base-url",
            "https://example.test/v1",
            "--api-key-env",
            "OURO_TEST_KEY",
            "--timeout",
            "7",
            "--default",
            "--json",
        ]
    )
    set_payload = json.loads(capsys.readouterr().out)

    assert set_code == 0
    assert set_payload["provider"]["provider_id"] == "glm"
    assert set_payload["provider"]["default"] is True
    assert set_payload["provider"]["credential"] == "env:OURO_TEST_KEY"

    list_code = main(["provider", "list", "--home", str(tmp_path), "--json"])
    list_payload = json.loads(capsys.readouterr().out)

    assert list_code == 0
    assert list_payload["default"] == "glm"
    assert {provider["provider_id"] for provider in list_payload["providers"]} == {
        "faux",
        "glm",
    }

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


def test_provider_check_can_target_builtin_faux(tmp_path, capsys) -> None:
    code = main(["provider", "check", "faux", "--home", str(tmp_path), "--json"])

    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["status"] == "ok"
    assert payload["provider_id"] == "faux"
    assert payload["model_id"] == "faux"


def test_provider_remove_resets_default_to_faux(tmp_path, capsys) -> None:
    assert (
        main(
            [
                "provider",
                "set",
                "local",
                "--home",
                str(tmp_path),
                "--kind",
                "faux",
                "--default",
            ]
        )
        == 0
    )
    capsys.readouterr()

    remove_code = main(["provider", "remove", "local", "--home", str(tmp_path), "--json"])
    remove_payload = json.loads(capsys.readouterr().out)

    assert remove_code == 0
    assert remove_payload["default"] == "faux"

import json
from types import SimpleNamespace

import pytest

from ouro.application.plugin_commands import consume_plugin_command
from ouro.application.plugin_models import PluginCommandExecutionView, PluginCommandView
from ouro.application.runtime import RuntimeService
from ouro.cli.interactive import run_interactive
from ouro.cli.interactive_commands import handle_command
from ouro.cli.main import main
from ouro.cli.parser import build_parser
from ouro.cli.run import build_default_runtime
from ouro.core.adapter import RuntimeAdapter
from ouro.core.provider import ModelMetadata, ProviderStreamEvent
from ouro.core.session_controls import AttributedSessionInput, AttributedSessionInputBatch
from ouro.providers import FauxProvider
from ouro.sessions import LocalSessionStore
from ouro.tools import ToolRegistry


@pytest.mark.parametrize(
    ("cli_value", "expected"),
    (("follow-up", "follow_up"), ("steer", "steer")),
)
def test_run_parser_maps_canonical_queue_submission_modes(cli_value, expected) -> None:
    args = build_parser().parse_args(["run", "--queue-mode", cli_value, "redirect"])

    assert args.queue_mode == expected


@pytest.mark.parametrize(
    "args",
    (
        ["run", "--queue-mode", "follow_up", "redirect"],
        ["config", "queued-inputs", "--consumption-mode", "all_at_once"],
    ),
)
def test_cli_rejects_internal_underscore_enum_values(args) -> None:
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(args)

    assert exc_info.value.code == 2


def test_queued_input_config_command_persists_consumption_mode(tmp_path, capsys) -> None:
    code = main(
        [
            "config",
            "queued-inputs",
            "--home",
            str(tmp_path),
            "--consumption-mode",
            "all-at-once",
            "--json",
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["queued_inputs"]["consumption_mode"] == "all_at_once"
    assert build_default_runtime(tmp_path).queued_input_config.consumption_mode == "all_at_once"


@pytest.mark.asyncio
async def test_interactive_steer_prefix_selects_steering_mode(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(
                [
                    ProviderStreamEvent(type="text_delta", text="ok"),
                    ProviderStreamEvent(type="done"),
                ]
            ),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
        tools=ToolRegistry(),
    )
    queue_modes: list[str] = []
    original_run = runtime.run

    async def recording_run(prompt, **kwargs):
        queue_modes.append(kwargs.get("queue_mode", "follow_up"))
        return await original_run(prompt, **kwargs)

    runtime.run = recording_run  # type: ignore[method-assign]
    inputs = iter(["/steer redirect", "/quit"])

    code = await run_interactive(
        runtime=runtime,
        input_func=lambda _prompt: next(inputs),
        output_func=lambda _text: None,
    )

    assert code == 0
    assert queue_modes == ["steer"]


@pytest.mark.asyncio
async def test_cli_plugin_submission_preserves_atomic_batch(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(
                [
                    ProviderStreamEvent(type="text_delta", text="ok"),
                    ProviderStreamEvent(type="done"),
                ]
            ),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
        tools=ToolRegistry(),
    )
    session = runtime.create_session(title="CLI batch")
    common = {
        "plugin_id": "dev.example.goal",
        "contribution_id": "goal",
        "generation_id": "generation_1",
        "execution_contract_hash": "contract_1",
    }
    batch = AttributedSessionInputBatch(
        inputs=(
            AttributedSessionInput(
                prompt="create",
                role="user",
                semantic_tag="goal-create",
                **common,
            ),
            AttributedSessionInput(prompt="ship", role="user", **common),
        )
    )

    async def execute(command_id, argument, **kwargs):
        return PluginCommandExecutionView(command_id=command_id, session_input_batches=[batch])

    plugins = SimpleNamespace(
        list_commands=lambda: [
            PluginCommandView(
                command_id="dev.example.goal:goal", plugin_id="dev.example.goal", name="goal"
            )
        ],
        execute_command=execute,
    )
    command = await handle_command("/goal ship", runtime, session.session_id, plugins=plugins)
    assert command.plugin_command is not None
    results = [result async for result in consume_plugin_command(runtime, command.plugin_command)]
    result = results[-1]

    assert result.run.status == "completed"
    messages = [
        message
        for message in store.load_session(session.session_id).messages
        if message.role == "user"
    ]
    assert [message.text_content for message in messages] == ["create", "ship"]
    assert len({message.run_id for message in messages}) == 1
    await runtime.close()

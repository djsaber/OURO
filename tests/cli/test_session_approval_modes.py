from __future__ import annotations

import json

import pytest

from ouro.application.runtime import RuntimeService
from ouro.cli.interactive import run_interactive
from ouro.cli.main import main
from ouro.cli.parser import build_parser
from ouro.core.adapter import RuntimeAdapter
from ouro.core.provider import ModelMetadata, ProviderStreamEvent
from ouro.providers import FauxProvider
from ouro.sessions import LocalSessionStore
from ouro.tools import ToolRegistry, builtin_tools


def test_run_parser_accepts_session_approval_mode() -> None:
    args = build_parser().parse_args(["run", "--approval-mode", "deny", "hello"])

    assert args.approval_mode == "deny"


def test_run_parser_accepts_afk_preset() -> None:
    args = build_parser().parse_args(["run", "--afk", "hello"])

    assert args.afk is True
    assert args.approval_mode is None


def test_interactive_afk_flag_passes_policy_pair_to_host(monkeypatch, tmp_path) -> None:
    calls: list[tuple[str, str]] = []

    async def fake_interactive(
        *,
        home,
        system_prompt,
        approval_mode,
        interaction_mode,
        session_backend=None,
    ) -> int:
        del home, system_prompt, session_backend
        calls.append((approval_mode, interaction_mode))
        return 0

    monkeypatch.setattr("ouro.cli.main.run_interactive", fake_interactive)

    code = main(["--home", str(tmp_path), "--afk"])

    assert code == 0
    assert calls == [("auto", "unattended")]


def test_run_cli_persists_session_approval_mode(tmp_path, capsys) -> None:
    code = main(
        [
            "run",
            "--json",
            "--home",
            str(tmp_path),
            "--approval-mode",
            "auto",
            "hello",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["approval_mode"] == "auto"
    store = LocalSessionStore(tmp_path)
    session = store.load_session(payload["session_id"]).session
    assert session.approval_mode == "auto"


def test_run_cli_persists_afk_policy_pair(tmp_path, capsys) -> None:
    code = main(["run", "--json", "--home", str(tmp_path), "--afk", "hello"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["approval_mode"] == "auto"
    assert payload["interaction_mode"] == "unattended"
    store = LocalSessionStore(tmp_path)
    session = store.load_session_record(payload["session_id"])
    assert session.approval_mode == "auto"
    assert session.interaction_mode == "unattended"


def test_session_approval_mode_command_updates_existing_session(tmp_path, capsys) -> None:
    assert main(["run", "--json", "--home", str(tmp_path), "hello"]) == 0
    session_id = json.loads(capsys.readouterr().out)["session_id"]

    code = main(
        [
            "session",
            "approval-mode",
            session_id,
            "deny",
            "--home",
            str(tmp_path),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["session"]["approval_mode"] == "deny"


def test_session_afk_command_updates_existing_session_atomically(tmp_path, capsys) -> None:
    assert main(["run", "--json", "--home", str(tmp_path), "hello"]) == 0
    session_id = json.loads(capsys.readouterr().out)["session_id"]

    code = main(
        [
            "session",
            "afk",
            session_id,
            "on",
            "--home",
            str(tmp_path),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["session"]["approval_mode"] == "auto"
    assert payload["session"]["interaction_mode"] == "unattended"


@pytest.mark.asyncio
async def test_interactive_command_sets_mode_before_first_session(tmp_path) -> None:
    target = tmp_path / "interactive.txt"
    store = LocalSessionStore(tmp_path)
    store.initialize()
    registry = ToolRegistry()
    for tool in builtin_tools():
        registry.register(tool)
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(
                event_batches=[
                    [
                        ProviderStreamEvent.from_tool_call(
                            {
                                "id": "call_1",
                                "name": "Write",
                                "arguments": {"path": str(target), "content": "written"},
                            },
                        ),
                        ProviderStreamEvent(type="done"),
                    ],
                    [
                        ProviderStreamEvent(type="text_delta", text="Done."),
                        ProviderStreamEvent(type="done"),
                    ],
                ]
            ),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
        tools=registry,
        default_workspace=tmp_path,
    )
    inputs = iter(["/approval-mode auto", "write", "/exit"])
    outputs: list[str] = []

    code = await run_interactive(
        runtime=runtime,
        input_func=lambda _prompt: next(inputs),
        output_func=outputs.append,
    )

    assert code == 0
    assert target.read_text(encoding="utf-8") == "written"
    assert "Approval mode: auto" in outputs
    session = store.read_catalog().sessions[0]
    assert session.approval_mode == "auto"


@pytest.mark.asyncio
async def test_interactive_afk_command_sets_both_policies_before_first_session(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(
                event_batches=[
                    [
                        ProviderStreamEvent(type="text_delta", text="Done."),
                        ProviderStreamEvent(type="done"),
                    ]
                ]
            ),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
        tools=ToolRegistry(),
    )
    inputs = iter(["/afk on", "continue", "/exit"])
    outputs: list[str] = []

    code = await run_interactive(
        runtime=runtime,
        input_func=lambda _prompt: next(inputs),
        output_func=outputs.append,
    )

    assert code == 0
    assert "AFK mode: on" in outputs
    session = store.read_catalog().sessions[0]
    assert session.approval_mode == "auto"
    assert session.interaction_mode == "unattended"

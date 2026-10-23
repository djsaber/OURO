from __future__ import annotations

import asyncio
import json

from ouro.application.configuration.facade import ConfigurationFacade
from ouro.cli.main import main
from ouro.local_application import LocalApplication


def _json_command(args: list[str], capsys) -> tuple[int, dict[str, object]]:
    code = main(args)
    return code, json.loads(capsys.readouterr().out)


async def _host_tree(home: str, root_session_id: str) -> dict[str, object]:
    async with LocalApplication.build(home) as application:
        assert application.agents is not None
        return application.agents.agent_tree_snapshot(root_session_id).model_dump(mode="json")


def test_agent_definition_cli_default_model_requires_system_route(tmp_path, capsys) -> None:
    command = [
        "agent",
        "definition",
        "create",
        "reader",
        "--name",
        "Reader",
        "--description",
        "Read evidence.",
        "--system-prompt",
        "Read carefully.",
        "--model",
        "default",
        "--expected-revision",
        "0",
        "--home",
        str(tmp_path),
        "--json",
    ]
    code, failed = _json_command(command, capsys)
    assert code != 0
    assert "system default endpoint is not configured" in failed["error"]

    ConfigurationFacade(tmp_path).add_provider(
        display_name="Primary", provider_type="openai", make_default=True
    )
    code, created = _json_command(command, capsys)
    assert code == 0
    assert created["catalog"]["definitions"][1]["model"] == "default"


def test_agent_definition_cli_crud_and_tool_compatibility_without_provider(
    tmp_path,
    capsys,
) -> None:
    home = str(tmp_path)

    code, listed = _json_command(
        ["agent", "definition", "list", "--home", home, "--json"],
        capsys,
    )
    assert code == 0
    assert listed["status"] == "ok"
    assert listed["catalog"]["revision"] == 0  # type: ignore[index]
    assert listed["catalog"]["definitions"][0]["agent_id"] == "general"  # type: ignore[index]

    code, tools = _json_command(
        ["agent", "definition", "tools", "--home", home, "--json"],
        capsys,
    )
    assert code == 0
    assert "Read" in {item["name"] for item in tools["tools"]}  # type: ignore[union-attr]

    code, created = _json_command(
        [
            "agent",
            "definition",
            "create",
            "reader",
            "--name",
            "Reader",
            "--description",
            "Read bounded evidence.",
            "--system-prompt",
            "Read only.",
            "--tool",
            "Read",
            "--expected-revision",
            "0",
            "--home",
            home,
            "--json",
        ],
        capsys,
    )
    assert code == 0
    assert created["catalog"]["revision"] == 1  # type: ignore[index]
    assert created["catalog"]["effective_from"] == "next_decision"  # type: ignore[index]

    code, shown = _json_command(
        ["agent", "definition", "show", "reader", "--home", home, "--json"],
        capsys,
    )
    assert code == 0
    assert shown["definition"]["system_prompt"] == "Read only."  # type: ignore[index]
    assert shown["definition"]["editable"] is True  # type: ignore[index]

    code, updated = _json_command(
        [
            "agent",
            "definition",
            "update",
            "reader",
            "--name",
            "Researcher",
            "--description",
            "Research bounded evidence.",
            "--system-prompt",
            "Research only.",
            "--all-tools",
            "--expected-revision",
            "1",
            "--home",
            home,
            "--json",
        ],
        capsys,
    )
    assert code == 0
    assert updated["catalog"]["revision"] == 2  # type: ignore[index]

    code, copied = _json_command(
        [
            "agent",
            "definition",
            "copy",
            "general",
            "general_copy",
            "--name",
            "General Copy",
            "--expected-revision",
            "2",
            "--home",
            home,
            "--json",
        ],
        capsys,
    )
    assert code == 0
    assert copied["catalog"]["revision"] == 3  # type: ignore[index]

    code, deleted = _json_command(
        [
            "agent",
            "definition",
            "delete",
            "reader",
            "--expected-revision",
            "3",
            "--home",
            home,
            "--json",
        ],
        capsys,
    )
    assert code == 0
    assert deleted["catalog"]["revision"] == 4  # type: ignore[index]

    code, rejected = _json_command(
        [
            "agent",
            "definition",
            "delete",
            "general",
            "--expected-revision",
            "4",
            "--home",
            home,
            "--json",
        ],
        capsys,
    )
    assert code == 2
    assert rejected["status"] == "failed"
    assert rejected["error_type"] == "ConfigError"


def test_agent_runtime_cli_uses_application_services_and_stable_dtos(tmp_path, capsys) -> None:
    home = str(tmp_path)
    code, parent = _json_command(
        ["run", "--json", "--home", home, "Create a parent Run."],
        capsys,
    )
    assert code == 0
    assert parent["status"] == "completed"

    code, spawned = _json_command(
        [
            "agent",
            "spawn",
            str(parent["session_id"]),
            str(parent["run_id"]),
            "general",
            "Complete one unattended child task.",
            "--description",
            "CLI child",
            "--request-id",
            "cli/spawn-1",
            "--home",
            home,
            "--json",
        ],
        capsys,
    )
    assert code == 0
    child = spawned["agent"]
    assert child["status"] == "completed"  # type: ignore[index]
    assert child["revision"] >= 1  # type: ignore[index,operator]
    delegation_id = str(child["delegation_id"])  # type: ignore[index]

    code, listed = _json_command(
        ["agent", "list", str(parent["session_id"]), "--home", home, "--json"],
        capsys,
    )
    assert code == 0
    assert [item["delegation_id"] for item in listed["agents"]] == [delegation_id]  # type: ignore[union-attr]

    code, shown = _json_command(
        ["agent", "show", delegation_id, "--home", home, "--json"],
        capsys,
    )
    assert code == 0
    assert shown["agent"]["final_text"] == "Hello from OURO."  # type: ignore[index]

    code, followed = _json_command(
        [
            "agent",
            "send",
            delegation_id,
            "Continue once.",
            "--mode",
            "follow-up",
            "--request-id",
            "cli/follow-up-1",
            "--home",
            home,
            "--json",
        ],
        capsys,
    )
    assert code == 0
    assert followed["agent"]["status"] == "completed"  # type: ignore[index]
    assert followed["agent"]["revision"] > child["revision"]  # type: ignore[index,operator]

    code, waited = _json_command(
        ["agent", "wait", delegation_id, "--home", home, "--json"],
        capsys,
    )
    assert code == 0
    assert waited["wait"]["completed"] is True  # type: ignore[index]

    code, interactions = _json_command(
        [
            "agent",
            "interactions",
            str(parent["session_id"]),
            "--home",
            home,
            "--json",
        ],
        capsys,
    )
    assert code == 0
    assert interactions["interactions"] == []

    code, interrupted = _json_command(
        ["agent", "interrupt", delegation_id, "--home", home, "--json"],
        capsys,
    )
    assert code == 0
    assert interrupted["agent"]["status"] == "completed"  # type: ignore[index]

    code, closed = _json_command(
        ["agent", "close", delegation_id, "--home", home, "--json"],
        capsys,
    )
    assert code == 0
    assert closed["agent"]["control_state"] == "closed"  # type: ignore[index]

    code, tree = _json_command(
        ["agent", "tree", str(parent["session_id"]), "--home", home, "--json"],
        capsys,
    )
    assert code == 0
    assert tree["tree"]["nodes"][0]["control_state"] == "closed"  # type: ignore[index]
    assert tree["tree"] == asyncio.run(_host_tree(home, str(parent["session_id"])))


def test_agent_runtime_config_cli_is_revisioned_and_typed(tmp_path, capsys) -> None:
    home = str(tmp_path)
    code, initial = _json_command(
        ["config", "agents", "--home", home, "--json"],
        capsys,
    )
    assert code == 0
    assert initial["agents"]["revision"] == 0  # type: ignore[index]
    assert initial["agents"]["max_depth"] == 1  # type: ignore[index]

    code, missing_revision = _json_command(
        ["config", "agents", "--allow-agent", "general", "--home", home, "--json"],
        capsys,
    )
    assert code == 2
    assert missing_revision["status"] == "failed"
    assert missing_revision["error_type"] == "ConfigError"

    code, updated = _json_command(
        [
            "config",
            "agents",
            "--expected-revision",
            "0",
            "--allow-agent",
            "general",
            "--allow-agent",
            "review-*",
            "--allow-model",
            "openai/*",
            "--execution-mode",
            "sync",
            "--no-write",
            "--no-network",
            "--no-mcp",
            "--no-external-paths",
            "--home",
            home,
            "--json",
        ],
        capsys,
    )
    assert code == 0
    assert updated["agents"]["revision"] == 1  # type: ignore[index]
    assert updated["agents"]["spawn_policy"]["allowed_agent_id_patterns"] == [  # type: ignore[index]
        "general",
        "review-*",
    ]
    policy = updated["agents"]["spawn_policy"]  # type: ignore[index]
    assert policy["allowed_model_patterns"] == ["openai/*"]  # type: ignore[index]
    assert policy["allowed_execution_modes"] == ["sync"]  # type: ignore[index]
    assert policy["allow_write"] is False  # type: ignore[index]
    assert policy["allow_network"] is False  # type: ignore[index]
    assert policy["allow_mcp"] is False  # type: ignore[index]
    assert policy["allow_external_paths"] is False  # type: ignore[index]
    assert updated["change"]["effective_from"] == "next_runtime"  # type: ignore[index]

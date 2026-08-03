from __future__ import annotations

from ouro.tui.commands import matching_commands, resolve_command


def test_matching_commands_filters_prefixes_and_aliases() -> None:
    assert [command.name for command in matching_commands("/m")] == ["/model", "/mcp"]
    assert [command.name for command in matching_commands("/res")] == ["/sessions"]
    assert matching_commands("plain text") == ()
    assert matching_commands("/model selector") == ()


def test_resolve_command_returns_canonical_command_and_argument() -> None:
    command, argument = resolve_command("/model provider/endpoint")
    approval, approval_argument = resolve_command("/approval-mode deny")
    thinking, thinking_argument = resolve_command("/reasoning high")
    compact, compact_argument = resolve_command("/compaction")
    config, config_argument = resolve_command("/config compaction")
    fork, fork_argument = resolve_command("/branch msg_123")
    alias, alias_argument = resolve_command("/exit")
    typo_alias, _ = resolve_command("/quite")
    unknown, _ = resolve_command("/missing")

    assert command is not None and command.name == "/model"
    assert argument == "provider/endpoint"
    assert approval is not None and approval.name == "/approval"
    assert approval_argument == "deny"
    assert thinking is not None and thinking.name == "/thinking"
    assert thinking_argument == "high"
    assert compact is not None and compact.name == "/compact"
    assert compact_argument == ""
    assert config is not None and config.name == "/config"
    assert config_argument == "compaction"
    assert fork is not None and fork.name == "/fork"
    assert fork_argument == "msg_123"
    assert alias is not None and alias.name == "/quit"
    assert typo_alias is not None and typo_alias.name == "/quit"
    assert alias_argument == ""
    assert unknown is None

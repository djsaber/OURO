from __future__ import annotations

from ouro.tui.commands import matching_commands, resolve_command


def test_matching_commands_filters_prefixes_and_aliases() -> None:
    assert [command.name for command in matching_commands("/m")] == ["/model"]
    assert [command.name for command in matching_commands("/res")] == ["/sessions"]
    assert matching_commands("plain text") == ()
    assert matching_commands("/model selector") == ()


def test_resolve_command_returns_canonical_command_and_argument() -> None:
    command, argument = resolve_command("/model provider/endpoint")
    alias, alias_argument = resolve_command("/exit")
    unknown, _ = resolve_command("/missing")

    assert command is not None and command.name == "/model"
    assert argument == "provider/endpoint"
    assert alias is not None and alias.name == "/quit"
    assert alias_argument == ""
    assert unknown is None

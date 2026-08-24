from __future__ import annotations

from ouro.tui.commands import (
    TUI_COMMANDS,
    TuiCommand,
    TuiCommandArgument,
    argument_suggestion,
    matching_arguments,
    matching_commands,
    resolve_command,
)


def test_aliases_are_distinct_from_canonical_command_prefixes() -> None:
    for command in TUI_COMMANDS:
        for alias in command.aliases:
            canonical = command.name.casefold()
            normalized_alias = alias.casefold()
            assert not canonical.startswith(normalized_alias)
            assert not normalized_alias.startswith(canonical)


def test_matching_commands_filters_canonical_prefixes_and_distinct_aliases() -> None:
    assert [command.name for command in matching_commands("/m")] == ["/actions", "/model"]
    assert [command.name for command in matching_commands("/res")] == ["/sessions"]
    assert [command.name for command in matching_commands("/ski")] == ["/skill"]
    assert [command.name for command in matching_commands("/unq")] == ["/unqueue"]
    assert matching_commands("/config") == ()
    assert matching_commands("/tools") == ()
    assert matching_commands("/mcp") == ()
    assert matching_commands("/skills") == ()
    assert matching_commands("plain text") == ()
    assert matching_commands("/model selector") == ()


def test_resolve_command_returns_canonical_command_and_argument() -> None:
    command, argument = resolve_command("/model provider/endpoint")
    approval, approval_argument = resolve_command("/approval deny")
    thinking, thinking_argument = resolve_command("/reasoning high")
    compact, compact_argument = resolve_command("/compact")
    skill, skill_argument = resolve_command("/skill reviewer inspect app.py")
    fork, fork_argument = resolve_command("/branch msg_123")
    unqueue, unqueue_argument = resolve_command("/unqueue queued_123")
    quit_command, quit_argument = resolve_command("/exit")
    removed_aliases = tuple(
        resolve_command(value)[0]
        for value in (
            "/approval-mode",
            "/compaction",
            "/quite",
        )
    )
    removed_help_alias, _ = resolve_command("/?")
    unknown, _ = resolve_command("/missing")

    assert command is not None and command.name == "/model"
    assert argument == "provider/endpoint"
    assert approval is not None and approval.name == "/approval"
    assert approval_argument == "deny"
    assert thinking is not None and thinking.name == "/thinking"
    assert thinking_argument == "high"
    assert compact is not None and compact.name == "/compact"
    assert compact_argument == ""
    assert skill is not None and skill.name == "/skill"
    assert skill_argument == "reviewer inspect app.py"
    assert fork is not None and fork.name == "/fork"
    assert fork_argument == "msg_123"
    assert unqueue is not None and unqueue.name == "/unqueue"
    assert unqueue_argument == "queued_123"
    assert quit_command is not None and quit_command.name == "/quit"
    assert quit_argument == ""
    assert removed_aliases == (None,) * 3
    assert removed_help_alias is None
    assert unknown is None


def test_parameter_options_filter_and_drive_inline_suggestions() -> None:
    command = TuiCommand(
        "/goal",
        "Goal",
        "Control the current Goal",
        "[status|pause|replace] | <request>",
        argument_options=(
            TuiCommandArgument("status", "Show the current Goal"),
            TuiCommandArgument("pause", "Pause the active Goal"),
            TuiCommandArgument(
                "replace",
                "Replace the current Goal",
                "[--token-budget N] <request>",
            ),
        ),
    )

    assert [item.value for item in matching_arguments(command, "pau")] == ["pause"]
    assert [item.value for item in matching_arguments(command, "replace new goal")] == ["replace"]
    assert command.input_hint == "<request>"
    assert argument_suggestion(command, "", command.argument_options[0]) == "<request>"
    assert argument_suggestion(command, "pau", command.argument_options[1]) == "se"
    assert argument_suggestion(command, "replace ", command.argument_options[2]) == (
        "--token-budget N <request>"
    )


def test_input_hint_preserves_full_usage_but_omits_menu_owned_branches() -> None:
    full_hint = (
        "[--token-budget N] <objective> | status | pause | "
        "resume [--token-budget N] | cancel | "
        "replace [--token-budget N] <objective>"
    )
    command = TuiCommand(
        "/goal",
        "Goal",
        "Control the current Goal",
        full_hint,
        argument_options=(
            TuiCommandArgument("status", "Show the current Goal"),
            TuiCommandArgument("pause", "Pause the active Goal"),
            TuiCommandArgument("resume", "Resume a paused Goal", "[--token-budget N]"),
            TuiCommandArgument("cancel", "Cancel the current Goal"),
            TuiCommandArgument(
                "replace",
                "Replace the current Goal",
                "[--token-budget N] <objective>",
            ),
        ),
    )

    assert command.usage == f"/goal {full_hint}"
    assert command.input_hint == "--token-budget N <objective>"


def test_argument_display_metadata_filters_without_changing_completion_value() -> None:
    argument = TuiCommandArgument(
        value="supplier/model-endpoint",
        description="supplier",
        label="model-endpoint",
    )
    command = TuiCommand(
        "/model",
        "Select model",
        "Select a model",
        "<model>",
        argument_options=(argument,),
    )

    assert matching_arguments(command, "model") == (argument,)
    assert matching_arguments(command, "supplier") == (argument,)
    assert argument_suggestion(command, "model", argument) == ""
    assert argument_suggestion(command, "supplier/model", argument) == "-endpoint"

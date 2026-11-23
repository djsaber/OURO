from __future__ import annotations

import pytest

from ouro.cli.parser import build_parser


def _subcommand_help(args: list[str], capsys: pytest.CaptureFixture[str]) -> str:
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args([*args, "--help"])
    assert exc_info.value.code == 0
    return capsys.readouterr().out


def test_endpoint_add_help_lists_reasoning_configuration_options(capsys) -> None:
    help_text = _subcommand_help(["provider", "endpoint", "add"], capsys)

    assert "--reasoning-preset" in help_text
    assert "--reasoning-config" in help_text


def test_run_help_lists_supported_thinking_modes_and_efforts(capsys) -> None:
    help_text = _subcommand_help(["run"], capsys)

    assert "--thinking-mode {default,enabled,disabled}" in help_text
    assert "--thinking-effort {minimal,low,medium,high,xhigh,max}" in help_text


def test_public_identifier_options_use_consistent_resource_and_session_names() -> None:
    parser = build_parser()

    spawn = parser.parse_args(
        [
            "agent",
            "spawn",
            "ses_parent",
            "run_parent",
            "general",
            "Review this change",
            "--description",
            "Review",
            "--request-id",
            "cli/review-1",
            "--resource",
            "res_context",
        ]
    )
    plugin = parser.parse_args(
        ["plugin", "run-command", "dev.ouro.example:hello", "--session", "ses_target"]
    )

    assert spawn.resource_ids == ["res_context"]
    assert plugin.session_id == "ses_target"

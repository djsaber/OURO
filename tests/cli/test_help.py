from __future__ import annotations

import pytest

from ouro.cli.parser import build_parser


def _subcommand_help(args: list[str], capsys: pytest.CaptureFixture[str]) -> str:
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args([*args, "--help"])
    assert exc_info.value.code == 0
    return capsys.readouterr().out


def test_endpoint_add_help_documents_reasoning_contract_choices(capsys) -> None:
    help_text = _subcommand_help(["provider", "endpoint", "add"], capsys)

    assert "model route:" in help_text
    assert "endpoint capabilities:" in help_text
    assert "reasoning contract:" in help_text
    assert "always-on" in help_text
    assert "openai-responses-effort" in help_text
    assert "capability.supported_summaries" in help_text
    assert "anthropic-messages-adaptive-effort" in help_text
    assert '{"capability": {...}, "profile":' in help_text
    assert "Presets are explicit declarations, not capability detection" in help_text
    assert "rejected before configuration is written" in help_text


def test_run_help_exposes_canonical_reasoning_values_and_strict_behavior(capsys) -> None:
    help_text = _subcommand_help(["run"], capsys)

    assert "model route overrides:" in help_text
    assert "endpoint capability overrides:" in help_text
    assert "reasoning intent:" in help_text
    assert "--thinking-mode {default,enabled,disabled}" in help_text
    assert "--thinking-effort {minimal,low,medium,high,xhigh,max}" in help_text
    assert "unsupported requests fail before an HTTP request is sent" in help_text


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


@pytest.mark.parametrize(
    "args",
    (
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
            "--resource-id",
            "res_context",
        ],
        [
            "plugin",
            "run-command",
            "dev.ouro.example:hello",
            "--session-id",
            "ses_target",
        ],
    ),
)
def test_removed_identifier_option_spellings_are_rejected(args) -> None:
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(args)

    assert exc_info.value.code == 2

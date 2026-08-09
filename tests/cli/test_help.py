from __future__ import annotations

import pytest

from ouro.cli.parser import build_parser


def _subcommand_help(args: list[str], capsys: pytest.CaptureFixture[str]) -> str:
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args([*args, "--help"])
    assert exc_info.value.code == 0
    return capsys.readouterr().out


def test_root_help_explains_interactive_default_and_getting_started() -> None:
    help_text = build_parser().format_help()

    assert "Run without a command to start the interactive Host" in help_text
    assert "Getting started:" in help_text
    assert "ouro provider templates" in help_text
    assert "ouro COMMAND --help" in help_text


def test_provider_help_explains_integrated_and_compatible_workflows(capsys) -> None:
    help_text = _subcommand_help(["provider"], capsys)

    assert "Integrated types install reviewed Endpoint templates" in help_text
    assert "compatible types start empty" in help_text
    assert "Typical workflow:" in help_text
    assert "provider endpoint set" in help_text


def test_endpoint_set_help_documents_reasoning_contract_choices(capsys) -> None:
    help_text = _subcommand_help(["provider", "endpoint", "set"], capsys)

    assert "model route:" in help_text
    assert "endpoint capabilities:" in help_text
    assert "reasoning contract:" in help_text
    assert "reasoning-content-effort" in help_text
    assert "anthropic-adaptive-effort" in help_text
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


def test_compaction_help_distinguishes_route_reset_from_full_clear(capsys) -> None:
    help_text = _subcommand_help(["config", "compaction"], capsys)

    assert "Omitted options retain their current values" in help_text
    assert "session runtime route inherits each Session's model reasoning" in help_text
    assert "clears the dedicated route and its reasoning override" in help_text
    assert "--clear also removes temperature and token overrides" in help_text

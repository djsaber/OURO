from __future__ import annotations

import pytest

from ouro import __version__
from ouro.cli.main import main
from ouro.cli.parser import build_parser


def test_root_parser_accepts_tui_resume_session() -> None:
    args = build_parser().parse_args(["--session", "ses_saved"])

    assert args.command is None
    assert args.resume_session_id == "ses_saved"


def test_default_command_starts_tui_with_host_options(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    def fake_run_tui(**kwargs: object) -> int:
        captured.update(kwargs)
        return 7

    monkeypatch.setattr("ouro.tui.entry.run_tui", fake_run_tui)

    code = main(
        [
            "--home",
            str(tmp_path),
            "--afk",
            "--session",
            "ses_saved",
        ]
    )

    assert code == 7
    assert captured == {
        "home": str(tmp_path),
        "system_prompt": None,
        "session_backend": None,
        "approval_mode": "auto",
        "interaction_mode": "unattended",
        "resume_session_id": "ses_saved",
    }


def test_interactive_command_forwards_command_scoped_options(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    async def fake_run_interactive(**kwargs: object) -> int:
        captured.update(kwargs)
        return 3

    monkeypatch.setattr("ouro.cli.main.run_interactive", fake_run_interactive)

    code = main(
        [
            "interactive",
            "--home",
            str(tmp_path),
            "--system-prompt",
            "Custom",
            "--approval-mode",
            "approval",
            "--session-backend",
            "sqlite",
        ]
    )

    assert code == 3
    assert captured == {
        "home": str(tmp_path),
        "system_prompt": "Custom",
        "approval_mode": "approval",
        "interaction_mode": "interactive",
        "session_backend": "sqlite",
    }


def test_removed_tui_command_is_rejected() -> None:
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["tui"])

    assert exc_info.value.code == 2


def test_version_uses_canonical_short_option(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["-V"])

    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"ouro {__version__}"

    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["-v"])

    assert exc_info.value.code == 2


@pytest.mark.parametrize(
    "option",
    (["--session", "ses_saved"], ["--approval-mode", "auto"], ["--afk"]),
)
def test_tui_only_options_are_not_silently_applied_to_commands(option, capsys) -> None:
    code = main([*option, "interactive"])

    assert code == 2
    assert "before a command are TUI options" in capsys.readouterr().err

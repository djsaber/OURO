from __future__ import annotations

from ouro.cli.main import main
from ouro.cli.parser import build_parser


def test_tui_parser_accepts_resume_session() -> None:
    args = build_parser().parse_args(["tui", "--session", "ses_saved"])

    assert args.command == "tui"
    assert args.tui_session_id == "ses_saved"


def test_tui_command_forwards_host_options(monkeypatch, tmp_path) -> None:
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
            "tui",
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

from __future__ import annotations

import json

from ouro.cli.main import main


def test_registered_subscription_cli_setup_disclosure_and_missing_login(tmp_path, capsys):
    home = ["--home", str(tmp_path)]
    assert main(["provider", "templates", "openai-codex", *home]) == 0
    assert "local planning budget" in capsys.readouterr().out
    assert (
        main(
            [
                "provider",
                "add",
                "Personal Codex",
                "--type",
                "openai-codex",
                "--default",
                "--json",
                *home,
            ]
        )
        == 0
    )
    added_output = capsys.readouterr().out
    assert "Actual output may exceed" in added_output
    provider_id = json.loads(added_output)["provider"]["provider_id"]
    assert main(["provider", "show", provider_id, "--json", *home]) == 0
    provider = json.loads(capsys.readouterr().out)["provider"]
    assert provider["auth_driver"] == "openai-codex"
    assert provider["credential"].startswith("managed:")
    assert "local planning budget" in provider["notice"]
    assert main(["provider", "auth", "methods", provider_id, "--json", *home]) == 0
    assert [m["method_id"] for m in json.loads(capsys.readouterr().out)["methods"]] == ["browser"]
    assert main(["provider", "check", provider_id, "--json", *home]) == 3
    assert json.loads(capsys.readouterr().out)["error_code"] == "authentication_required"
    assert main(["run", "Reply OK.", "--afk", "--json", *home]) == 1
    failed = json.loads(capsys.readouterr().out)
    assert failed["interaction_mode"] == "unattended"
    assert any(
        event["payload"].get("error_code") == "authentication_required"
        for event in failed["events"]
    )
    assert main(["provider", "auth", "logout", provider_id, "--json", *home]) == 0
    assert json.loads(capsys.readouterr().out)["credential"]["state"] == "logged_out"

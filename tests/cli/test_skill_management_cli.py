from __future__ import annotations

import json
from pathlib import Path

from ouro.cli.main import main


def test_skill_management_cli_lifecycle(tmp_path, monkeypatch, capsys) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "source" / "writer"
    _write_skill(source, "writer", "Write text.", "Version one.")
    monkeypatch.chdir(workspace)

    assert (
        main(
            [
                "skills",
                "install",
                str(source),
                "--home",
                str(home),
                "--scope",
                "workspace",
                "--all",
                "--json",
            ]
        )
        == 0
    )
    installed = json.loads(capsys.readouterr().out)
    assert installed["action"] == "installed"
    assert installed["skills"][0]["scope"] == "workspace"

    assert main(["skills", "disable", "writer", "--home", str(home), "--json"]) == 0
    disabled = json.loads(capsys.readouterr().out)
    assert disabled["action"] == "disabled"

    assert main(["skills", "show", "writer", "--home", str(home), "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["skill"]["enabled"] is False

    assert main(["skills", "list", "--all", "--home", str(home), "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["skills"][0]["enabled"] is False
    assert listed["skills"][0]["managed"] is True

    assert main(["skills", "enable", "writer", "--home", str(home), "--json"]) == 0
    capsys.readouterr()

    _write_skill(source, "writer", "Write text.", "Version two.")
    assert main(["skills", "update", "writer", "--home", str(home), "--json"]) == 0
    updated = json.loads(capsys.readouterr().out)
    assert updated["action"] == "updated"
    assert "Version two." in (workspace / ".ouro" / "skills" / "writer" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    assert main(["skills", "validate", str(source), "--home", str(home), "--json"]) == 0
    validated = json.loads(capsys.readouterr().out)
    assert validated["valid"] is True

    assert main(["skills", "remove", "writer", "--home", str(home), "--json"]) == 0
    removed = json.loads(capsys.readouterr().out)
    assert removed["action"] == "removed"
    assert not (workspace / ".ouro" / "skills" / "writer").exists()


def test_install_cli_requires_skill_selection_for_collections(tmp_path) -> None:
    import pytest

    source = tmp_path / "collection"
    _write_skill(source / "one", "one", "First.")
    _write_skill(source / "two", "two", "Second.")

    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "skills",
                "install",
                str(source),
                "--home",
                str(tmp_path / "home"),
                "--json",
            ]
        )

    assert exc_info.value.code == 2


def _write_skill(root: Path, name: str, description: str, body: str = "Instructions.") -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text(
        f"""---
name: {name}
description: {description}
---
{body}
""",
        encoding="utf-8",
    )

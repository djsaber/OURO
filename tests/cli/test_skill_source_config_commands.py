from __future__ import annotations

import json

from ouro.cli.main import main
from ouro.skills import default_skill_roots


def test_skill_source_config_defaults_to_all_host_directories_enabled(tmp_path, capsys) -> None:
    code = main(["config", "skills", "--home", str(tmp_path), "--json"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["skills"]["enabled"] is True
    assert payload["skills"]["disabled_sources"] == []
    assert all(source["enabled"] for source in payload["skills"]["sources"])


def test_skill_source_config_updates_scoped_ouro_and_host_sources(tmp_path, capsys) -> None:
    code = main(
        [
            "config",
            "skills",
            "--home",
            str(tmp_path),
            "--disable-source",
            "ouro.user",
            "--disable-source",
            "codex.workspace",
            "--json",
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    states = {source["source_id"]: source["enabled"] for source in payload["skills"]["sources"]}
    assert states["ouro.user"] is False
    assert states["ouro.workspace"] is True
    assert states["codex.user"] is True
    assert states["codex.workspace"] is False

    roots = {root.source_id: root.enabled for root in default_skill_roots(home=tmp_path)}
    assert roots["ouro.user"] is False
    assert roots["codex.workspace"] is False


def test_skill_source_config_can_disable_all_host_directory_discovery(tmp_path, capsys) -> None:
    code = main(
        [
            "config",
            "skills",
            "--home",
            str(tmp_path),
            "--no-discovery-enabled",
            "--json",
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["skills"]["enabled"] is False
    assert all(not root.enabled for root in default_skill_roots(home=tmp_path))


def test_skill_source_config_rejects_conflicting_updates_without_writing(tmp_path, capsys) -> None:
    code = main(
        [
            "config",
            "skills",
            "--home",
            str(tmp_path),
            "--enable-source",
            "ouro.user",
            "--disable-source",
            "ouro.user",
            "--json",
        ]
    )

    assert code != 0
    payload = json.loads(capsys.readouterr().out)
    assert "cannot be enabled and disabled together" in payload["error"]
    assert not (tmp_path / "config" / "config.json").exists()

from __future__ import annotations

import json

from ouro.application.sessions import SessionService
from ouro.cli.main import main
from ouro.sessions import LocalSessionStore
from ouro.transfer.archive import read_archive


def test_resource_cli_round_trip_and_session_export_manifest(tmp_path, capsys) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionService(store).create(title="CLI resources").session
    source = tmp_path / "source.txt"
    source.write_text("resource body", encoding="utf-8")

    assert (
        main(
            [
                "resource",
                "import",
                session.session_id,
                str(source),
                "--home",
                str(tmp_path),
                "--json",
            ]
        )
        == 0
    )
    imported = json.loads(capsys.readouterr().out)
    resource_id = imported["resource"]["resource_id"]
    assert imported["resource"]["source"]["name"] == "source.txt"
    assert str(source.parent) not in json.dumps(imported)

    assert (
        main(
            [
                "resource",
                "list",
                session.session_id,
                "--home",
                str(tmp_path),
                "--json",
            ]
        )
        == 0
    )
    listed = json.loads(capsys.readouterr().out)
    assert [item["resource_id"] for item in listed["resources"]] == [resource_id]

    destination = tmp_path / "exported.txt"
    assert (
        main(
            [
                "resource",
                "export",
                session.session_id,
                resource_id,
                str(destination),
                "--home",
                str(tmp_path),
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert destination.read_text("utf-8") == "resource body"

    archive = tmp_path / "session.zip"
    assert (
        main(
            [
                "data",
                "export",
                "--kind",
                "sessions",
                "--id",
                session.session_id,
                "--path",
                str(archive),
                "--home",
                str(tmp_path),
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "ok"
    exported_session = read_archive(archive, "sessions").manifest.data
    assert exported_session["resources"][0]["resource_id"] == resource_id
    assert "path" not in exported_session["resources"][0]["source"]

    assert (
        main(
            [
                "resource",
                "remove",
                session.session_id,
                resource_id,
                "--home",
                str(tmp_path),
                "--force",
                "--json",
            ]
        )
        == 0
    )
    removed = json.loads(capsys.readouterr().out)
    assert removed["content_collected"] is True

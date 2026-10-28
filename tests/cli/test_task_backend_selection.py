from __future__ import annotations

import json

import pytest

from ouro.cli.main import main


@pytest.mark.parametrize("task_backend", ["files", "sqlite"])
def test_run_selects_task_backend_without_changing_session_backend(tmp_path, capsys, task_backend):
    assert (
        main(
            [
                "run",
                "hello",
                "--home",
                str(tmp_path),
                "--json",
                "--task-backend",
                task_backend,
                "--session-backend",
                "files",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert (tmp_path / "sessions" / result["session_id"] / "session-data.sqlite3").is_file()
    assert not (tmp_path / "sessions.sqlite3").exists()
    assert (tmp_path / "tasks.sqlite3").exists() == (task_backend == "sqlite")
    assert (tmp_path / "tasks" / "layout.json").exists() == (task_backend == "files")


def test_explicit_task_backend_overrides_environment(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OURO_TASK_BACKEND", "sqlite")
    assert main(["--task-backend", "files", "run", "hello", "--home", str(tmp_path), "--json"]) == 0
    capsys.readouterr()
    assert not (tmp_path / "tasks.sqlite3").exists()
    assert (tmp_path / "tasks" / "layout.json").exists()


def test_invalid_task_backend_environment_reports_configuration_error(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("OURO_TASK_BACKEND", "remote")
    assert main(["run", "hello", "--home", str(tmp_path), "--json"]) != 0
    result = json.loads(capsys.readouterr().out)
    assert result["error_type"] == "ConfigError"
    assert "OURO_TASK_BACKEND" in result["error"]


def test_tui_forwards_task_backend_to_application_composition(monkeypatch):
    import ouro.tui.entry

    captured = {}

    def run_tui(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(ouro.tui.entry, "run_tui", run_tui)
    assert main(["--task-backend", "sqlite", "--session-backend", "files"]) == 0
    assert captured["task_backend"] == "sqlite"
    assert captured["session_backend"] == "files"

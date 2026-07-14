import json

from ouro.cli.main import main
from ouro.config.tool_execution import ToolExecutionConfigManager


def test_config_tool_execution_persists_parallel_policy(tmp_path, capsys) -> None:
    code = main(
        [
            "config",
            "tool-execution",
            "--home",
            str(tmp_path),
            "--mode",
            "parallel",
            "--max-concurrency",
            "3",
            "--json",
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tool_execution"] == {
        "mode": "parallel",
        "max_concurrency": 3,
        "workspace_only": True,
        "max_output_chars": 100_000,
    }
    snapshot = ToolExecutionConfigManager(tmp_path).snapshot()
    assert snapshot.mode == "parallel"
    assert snapshot.workspace_only is True


def test_config_tool_execution_persists_workspace_and_output_policy(tmp_path, capsys) -> None:
    code = main(
        [
            "config",
            "tool-execution",
            "--home",
            str(tmp_path),
            "--no-workspace-only",
            "--max-output-chars",
            "4096",
            "--json",
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tool_execution"]["workspace_only"] is False
    assert payload["tool_execution"]["max_output_chars"] == 4096


def test_config_tool_execution_rejects_invalid_concurrency(tmp_path, capsys) -> None:
    code = main(
        [
            "config",
            "tool-execution",
            "--home",
            str(tmp_path),
            "--max-concurrency",
            "0",
            "--json",
        ]
    )

    assert code != 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "failed"

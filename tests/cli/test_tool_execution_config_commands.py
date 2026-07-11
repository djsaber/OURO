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
    assert payload["tool_execution"] == {"mode": "parallel", "max_concurrency": 3}
    assert ToolExecutionConfigManager(tmp_path).snapshot().mode == "parallel"


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

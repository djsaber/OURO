import json

from ouro.cli.main import main
from ouro.config.run_budget import RunBudgetConfigManager


def test_run_budget_defaults_to_unlimited(tmp_path, capsys) -> None:
    code = main(["config", "run-budget", "--home", str(tmp_path), "--json"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["run_budget"] == {}


def test_run_budget_command_persists_and_clears_limits(tmp_path, capsys) -> None:
    code = main(
        [
            "config",
            "run-budget",
            "--home",
            str(tmp_path),
            "--max-model-rounds",
            "12",
            "--max-input-tokens",
            "50000",
            "--max-elapsed-seconds",
            "90",
            "--json",
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["run_budget"] == {
        "max_model_rounds": 12,
        "max_input_tokens": 50000,
        "max_elapsed_seconds": 90.0,
    }
    assert RunBudgetConfigManager(tmp_path).snapshot().max_model_rounds == 12

    clear_code = main(["config", "run-budget", "--home", str(tmp_path), "--clear", "--json"])
    assert clear_code == 0
    assert json.loads(capsys.readouterr().out)["run_budget"] == {}

import json

import pytest

from ouro.cli.main import main
from ouro.cli.parser import build_parser
from ouro.cli.run import build_default_runtime


@pytest.mark.parametrize(
    ("cli_value", "expected"),
    (("follow-up", "follow_up"), ("steer", "steer")),
)
def test_run_parser_maps_canonical_queue_submission_modes(cli_value, expected) -> None:
    args = build_parser().parse_args(["run", "--queue-mode", cli_value, "redirect"])

    assert args.queue_mode == expected


@pytest.mark.parametrize(
    "args",
    (
        ["run", "--queue-mode", "follow_up", "redirect"],
        ["config", "queued-inputs", "--consumption-mode", "all_at_once"],
    ),
)
def test_cli_rejects_internal_underscore_enum_values(args) -> None:
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(args)

    assert exc_info.value.code == 2


def test_queued_input_config_command_persists_consumption_mode(tmp_path, capsys) -> None:
    code = main(
        [
            "config",
            "queued-inputs",
            "--home",
            str(tmp_path),
            "--consumption-mode",
            "all-at-once",
            "--json",
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["queued_inputs"]["consumption_mode"] == "all_at_once"
    assert build_default_runtime(tmp_path).queued_input_config.consumption_mode == "all_at_once"

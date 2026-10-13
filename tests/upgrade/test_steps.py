from __future__ import annotations

import copy

import pytest

from ouro.core.state import RunRecord
from ouro_upgrade import records
from ouro_upgrade.errors import UpgradeError
from ouro_upgrade.records import convert, loads
from ouro_upgrade.steps import UpgradeStep, migrate
from tests.upgrade.conftest import FIXTURES


def test_future_run_version_retains_the_entire_historical_upgrade_path(monkeypatch) -> None:
    class FutureRun(RunRecord):
        schema_version: int = 6
        future_checkpoint: str | None

    def from_v5(value: dict) -> None:
        assert value["schema_version"] == 5
        assert value["driver"]["driver_id"] == "ouro.default"
        value["future_checkpoint"] = None

    monkeypatch.setitem(records.MODELS, "run", FutureRun)
    monkeypatch.setitem(records.VERSION_STEPS["run"], 5, UpgradeStep(6, from_v5))
    raw = loads((FIXTURES / "files/sessions/ses_fixture/runs/run_fixture.json").read_text("utf-8"))
    before = copy.deepcopy(raw)
    result = convert("run", raw)
    assert result["schema_version"] == 6
    assert result["future_checkpoint"] is None
    assert result["active_tool_batch"]["producer"]["kind"] == "model"
    assert result["run_id"] == raw["run_id"]
    assert raw == before
    assert convert("run", result) == result


@pytest.mark.parametrize("target", [1, 2, 7, True])
def test_invalid_steps_fail_before_executing_a_transform(target: int) -> None:
    def unexpected(value: dict) -> None:
        pytest.fail("invalid transition must not execute")

    with pytest.raises(UpgradeError, match="invalid fixture upgrade step"):
        migrate(
            "fixture", {"schema_version": 2}, target=6, steps={2: UpgradeStep(target, unexpected)}
        )


@pytest.mark.parametrize("version", [None, True, "2", 2.0, 3, 7])
def test_only_registered_source_versions_are_accepted(version: object) -> None:
    def unexpected(value: dict) -> None:
        pytest.fail("unknown source must not execute")

    with pytest.raises(UpgradeError, match="unsupported fixture version"):
        migrate(
            "fixture", {"schema_version": version}, target=6, steps={2: UpgradeStep(6, unexpected)}
        )


def test_missing_transition_does_not_mutate_input() -> None:
    raw = {"schema_version": 2, "metadata": {"schema_version": 999}}
    before = copy.deepcopy(raw)

    def from_v2(value: dict) -> None:
        value["metadata"]["changed"] = True

    with pytest.raises(UpgradeError, match="missing fixture upgrade step from version 5"):
        migrate("fixture", raw, target=6, steps={2: UpgradeStep(5, from_v2)})
    assert raw == before


def test_current_envelope_still_converts_independently_versioned_nested_records() -> None:
    raw = loads(
        (FIXTURES / "files/sessions/ses_fixture/operations/op_fixture.json").read_text("utf-8")
    )
    raw["messages"][0] = convert("message", raw["messages"][0])
    result = convert("commit", raw)
    assert result["schema_version"] == raw["schema_version"] == 1
    assert result["messages"][0] == raw["messages"][0]
    assert all(message["schema_version"] == 4 for message in result["messages"])
    assert result["request_digest"] == raw["request_digest"]


@pytest.mark.parametrize("damage", ["extra", "identity", "facts", "version"])
def test_envelope_validation_rejects_invalid_nested_records_without_mutating_input(damage):
    raw = loads(
        (FIXTURES / "files/sessions/ses_fixture/operations/op_fixture.json").read_text("utf-8")
    )
    message = raw["messages"][-1]
    if damage == "extra":
        message["unknown_native_field"] = "private-nested-value"
    elif damage == "identity":
        del message["message_id"]
    elif damage == "facts":
        del message["sequence"]
    else:
        message["schema_version"] = 999
    before = copy.deepcopy(raw)
    with pytest.raises(UpgradeError) as error:
        convert("commit", raw)
    assert "private-nested-value" not in str(error.value)
    assert raw == before

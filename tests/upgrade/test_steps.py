from __future__ import annotations

import copy

import pytest

from ouro.core.events import EventRecord
from ouro.core.state import RunRecord
from ouro_upgrade import records
from ouro_upgrade.errors import UpgradeError
from ouro_upgrade.records import convert, loads
from ouro_upgrade.steps import UpgradeStep, migrate
from tests.upgrade.conftest import FIXTURES


def test_future_run_version_retains_the_entire_historical_upgrade_path(monkeypatch) -> None:
    class FutureRun(RunRecord):
        schema_version: int = 11
        future_checkpoint: str | None

    def from_v5(value: dict) -> None:
        assert value["schema_version"] == 10
        assert value["driver"]["driver_id"] == "ouro.default"
        value["future_checkpoint"] = None

    monkeypatch.setitem(records.MODELS, "run", FutureRun)
    monkeypatch.setitem(records.VERSION_STEPS["run"], 10, UpgradeStep(11, from_v5))
    raw = loads((FIXTURES / "files/sessions/ses_fixture/runs/run_fixture.json").read_text("utf-8"))
    raw["active_tool_batch"] = None
    before = copy.deepcopy(raw)
    result = convert("run", raw)
    assert result["schema_version"] == 11
    assert result["future_checkpoint"] is None
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


def test_current_records_are_preserved_without_auditing_their_payloads() -> None:
    raw = {"schema_version": EventRecord.model_fields["schema_version"].default, "payload": {}}
    assert convert("event", raw) is raw


def test_new_event_step_is_used_even_when_the_kind_previously_needed_no_upgrade(monkeypatch):
    class FutureEvent(EventRecord):
        schema_version: int = EventRecord.model_fields["schema_version"].default + 1
        category: str

    def categorize(value):
        value["category"] = "historical"

    raw = EventRecord(session_id="ses_fixture", event_type="fixture", sequence=1).model_dump(
        mode="json"
    )
    monkeypatch.setitem(records.MODELS, "event", FutureEvent)
    monkeypatch.setitem(
        records.VERSION_STEPS,
        "event",
        {
            raw["schema_version"]: UpgradeStep(
                FutureEvent.model_fields["schema_version"].default, categorize
            )
        },
    )
    result = convert("event", raw)
    assert result["schema_version"] == FutureEvent.model_fields["schema_version"].default
    assert result["category"] == "historical"
    assert convert("event", result) is result

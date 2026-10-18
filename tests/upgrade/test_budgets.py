from __future__ import annotations

import copy
import json
import sqlite3
from contextlib import closing

import pytest

from ouro.application.runtime_activity import queued_runtime_activity, reduce_runtime_activity
from ouro.core.context import ContextMetrics
from ouro.core.events import EventRecord
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro_upgrade.engine import upgrade
from ouro_upgrade.errors import UpgradeError
from ouro_upgrade.records import convert, loads
from tests.upgrade.conftest import FIXTURES
from tests.upgrade.test_upgrade import hashes


@pytest.fixture
def budget_records() -> dict:
    return loads((FIXTURES / "records/output-budget.json").read_text("utf-8"))


@pytest.mark.parametrize(
    "kind,field,version", [("context", "metrics", 5), ("compaction", "context_metrics_after", 2)]
)
def test_budget_records_preserve_measurements_and_request_capacity(
    budget_records, kind, field, version
):
    source = budget_records[kind]
    before = copy.deepcopy(source)
    result = convert(kind, source)
    assert result["schema_version"] == version
    metrics = ContextMetrics.model_validate(result[field])
    assert metrics.budget.model_dump() == {"context_window": 1000, "max_output_tokens": 200}
    for key, value in before[field].items():
        assert getattr(metrics, key) == value
    for key in before.keys() - {"schema_version", field}:
        assert result[key] == before[key]
    assert source == before
    assert convert(kind, result) is result


def test_event_and_commit_metrics_remain_replayable_without_touching_opaque_payloads(
    budget_records,
):
    commit = budget_records["commit"]
    before = copy.deepcopy(commit)
    result = convert("commit", commit)
    activity = queued_runtime_activity("ses_fixture")
    for source, event in zip(before["events"], result["events"], strict=True):
        assert event == convert("event", source)
        assert event["metadata"] == source["metadata"]
        activity = reduce_runtime_activity(activity, EventRecord.model_validate(event))
        assert activity.effective_context_metrics is not None
        assert activity.effective_context_metrics.input_budget_tokens == 800
    assert activity.context_status is not None
    assert result["output"] == before["output"]
    assert result["request_digest"] == before["request_digest"]
    assert commit == before
    assert convert("commit", result) is result
    unrelated = copy.deepcopy(before["events"][0])
    unrelated["event_type"] = "plugin.custom"
    assert convert("event", unrelated) is unrelated


@pytest.mark.parametrize("damage", ["missing_window", "mixed", "reserve", "derived", "unknown"])
def test_budget_upgrade_rejects_incomplete_or_conflicting_facts(budget_records, damage):
    source = budget_records["context"]
    metrics = source["metrics"]
    if damage == "missing_window":
        del metrics["context_window_tokens"]
    elif damage == "mixed":
        metrics["budget"] = {"context_window": 1000, "max_output_tokens": 200}
    elif damage == "reserve":
        metrics["reserved_output_tokens"] = True
    elif damage == "derived":
        metrics["estimated_remaining_tokens"] = 999
    else:
        metrics["unknown"] = "private-value-must-not-be-logged"
    before = copy.deepcopy(source)
    with pytest.raises(UpgradeError) as error:
        convert("context", source)
    assert "private-value" not in str(error.value)
    assert source == before


def test_zero_reserve_is_unlimited_and_absent_compaction_metrics_stay_absent(budget_records):
    source = budget_records["context"]
    source["metrics"].update(
        reserved_output_tokens=0,
        input_budget_tokens=1000,
        estimated_remaining_tokens=880,
        estimated_utilization_ratio=0.12,
    )
    assert convert("context", source)["metrics"]["budget"] == {
        "context_window": 1000,
        "max_output_tokens": None,
    }
    compaction = budget_records["compaction"]
    compaction["context_metrics_after"] = None
    assert convert("compaction", compaction)["context_metrics_after"] is None


@pytest.mark.parametrize("historical_home", ["files", "sqlite"], indirect=True)
def test_budget_upgrade_reaches_both_stores_and_preserves_restore(historical_home, budget_records):
    home = historical_home
    backend = "sqlite" if (home / "sessions.sqlite3").exists() else "files"
    options = {"session_backend": backend, "task_backend": backend}
    context = json.dumps(budget_records["context"])
    compaction = budget_records["compaction"]
    events = budget_records["events"]
    commit = budget_records["commit"]
    if backend == "files":
        directory = home / "sessions/ses_fixture"
        (directory / "context.json").write_text(context, encoding="utf-8")
        (directory / "compactions.jsonl").write_text(
            json.dumps(compaction) + "\n", encoding="utf-8"
        )
        (directory / "events.jsonl").write_text(
            "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
        )
        (directory / "operations/op_budget.json").write_text(json.dumps(commit), encoding="utf-8")
        offsets = directory / "event-index/sequences.bin"
        offsets.parent.mkdir(parents=True, exist_ok=True)
        offsets.write_bytes(b"old offsets")
        store = LocalSessionStore(home)
    else:
        with closing(sqlite3.connect(home / "sessions.sqlite3")) as connection:
            connection.execute(
                "UPDATE latest_contexts SET context_id=?,payload=?",
                (budget_records["context"]["context_id"], context),
            )
            connection.execute(
                "INSERT INTO compactions (compaction_id,session_id,payload) VALUES (?,?,?)",
                (compaction["compaction_id"], "ses_fixture", json.dumps(compaction)),
            )
            for event in events:
                connection.execute(
                    "INSERT INTO events (event_id,session_id,run_id,sequence,payload) "
                    "VALUES (?,?,?,?,?)",
                    (
                        event["event_id"],
                        "ses_fixture",
                        "run_fixture",
                        event["sequence"],
                        json.dumps(event),
                    ),
                )
            connection.execute(
                "INSERT INTO session_commits (operation_id,session_id,payload) VALUES (?,?,?)",
                ("op_budget", "ses_fixture", json.dumps(commit)),
            )
            connection.commit()
        store = SQLiteSessionStore(home / "sessions.sqlite3")
    before = hashes(home)
    assert upgrade(home, check=True, **options)["status"] == "upgrade_required"
    assert hashes(home) == before

    def interrupt_publication(progress):
        if progress.phase == "publish" and progress.completed == 1:
            raise OSError("simulated publication failure")

    with pytest.raises(UpgradeError, match="I/O"):
        upgrade(home, progress=interrupt_publication, **options)
    assert upgrade(home, restore=True, **options)["status"] == "restored"
    assert hashes(home) == before
    assert upgrade(home, **options)["status"] == "upgraded"
    snapshot = store.load_session("ses_fixture")
    assert snapshot.compactions[0].context_metrics_after.input_budget_tokens == 800
    assert store.load_latest_context("ses_fixture").metrics.input_budget_tokens == 800
    assert (
        store.load_commit("ses_fixture", "op_budget").events[0].payload["context_metrics"]["budget"]
    )
    if backend == "files":
        assert not offsets.exists()
    upgraded = hashes(home)
    assert upgrade(home, **options)["status"] == "current"
    assert hashes(home) == upgraded

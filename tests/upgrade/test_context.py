from __future__ import annotations

import copy
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from ouro.core.context import ContextMetrics
from ouro.core.context_snapshots import ContextSnapshotRecord
from ouro.core.errors import PersistenceError
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro_upgrade import records
from ouro_upgrade.engine import upgrade
from ouro_upgrade.errors import UpgradeError
from ouro_upgrade.records import convert, loads
from ouro_upgrade.steps import UpgradeStep
from tests.upgrade.conftest import FIXTURES
from tests.upgrade.test_upgrade import hashes


@pytest.fixture
def context_v3() -> dict:
    return loads((FIXTURES / "records/context-v3.json").read_text("utf-8"))


@pytest.mark.parametrize("supports_vision", [False, True])
def test_context_upgrade_preserves_snapshot_and_converts_nested_messages(
    context_v3: dict, supports_vision: bool
) -> None:
    context_v3["supports_vision"] = supports_vision
    before = copy.deepcopy(context_v3)
    result = convert("context", context_v3)
    assert result["schema_version"] == 11
    assert "supports_vision" not in result
    assert result["supports_image_input"] is supports_vision
    assert result["supports_audio_input"] is False
    assert result["supports_video_input"] is False
    metrics = ContextMetrics.model_validate(result["metrics"])
    for key, value in before["metrics"].items():
        assert getattr(metrics, key) == value
    projected = result["provider_context"]["messages"]
    sources = before["injected_messages"] + before["source_messages"]
    assert [item["content"] for item in projected] == [item["content"] for item in sources]
    assert projected[-1]["tool_result"]["producer"]["kind"] == "model"
    assert result["context_metadata"] == before["context_metadata"]
    assert context_v3 == before
    assert convert("context", result) == result


def test_context_v4_keeps_audio_and_video_capabilities() -> None:
    raw = loads((FIXTURES / "files/sessions/ses_fixture/context.json").read_text("utf-8"))
    raw.update(supports_audio_input=True, supports_video_input=True)
    result = convert("context", raw)
    assert result["supports_image_input"] is False
    assert result["supports_audio_input"] is True
    assert result["supports_video_input"] is True
    assert convert("context", result) == result


@pytest.mark.parametrize(
    "damage",
    [
        "missing_vision",
        "invalid_vision",
        "invalid_vision_string",
        "supports_image_input",
        "supports_audio_input",
        "supports_video_input",
        "unknown_field",
        "past_version",
        "future_version",
    ],
)
def test_context_upgrade_rejects_incomplete_or_ambiguous_records(context_v3: dict, damage: str):
    if damage == "missing_vision":
        del context_v3["supports_vision"]
    elif damage == "invalid_vision":
        context_v3["supports_vision"] = None
    elif damage == "invalid_vision_string":
        context_v3["supports_vision"] = "false"
    elif damage in {"past_version", "future_version"}:
        context_v3["schema_version"] = 2 if damage == "past_version" else 6
    else:
        context_v3[damage] = "private-value-must-not-be-logged"
    before = copy.deepcopy(context_v3)
    with pytest.raises(UpgradeError) as error:
        convert("context", context_v3)
    assert "private-value" not in str(error.value)
    assert context_v3 == before


def test_context_upgrade_runs_each_step_to_the_model_version(context_v3: dict, monkeypatch):
    class FutureContext(ContextSnapshotRecord):
        schema_version: int = 12
        future_capability: bool

    def from_v5(value: dict) -> None:
        assert value["schema_version"] == 11
        assert value["supports_image_input"] is True
        assert "budget" in value["metrics"]
        value["future_capability"] = False

    monkeypatch.setitem(records.MODELS, "context", FutureContext)
    monkeypatch.setitem(records.VERSION_STEPS["context"], 11, UpgradeStep(12, from_v5))
    result = convert("context", context_v3)
    assert result["schema_version"] == 12
    assert result["future_capability"] is False
    assert convert("context", result) == result


def test_context_upgrade_rejects_a_missing_step(context_v3: dict, monkeypatch):
    class FutureContext(ContextSnapshotRecord):
        schema_version: int = 12

    monkeypatch.setitem(records.MODELS, "context", FutureContext)
    before = copy.deepcopy(context_v3)
    with pytest.raises(UpgradeError, match="missing context upgrade step from version 11"):
        convert("context", context_v3)
    assert context_v3 == before


@pytest.mark.parametrize("historical_home", ["files", "sqlite"], indirect=True)
def test_context_v3_upgrades_in_place_for_both_backends(historical_home: Path, context_v3: dict):
    home = historical_home
    backend = "sqlite" if (home / "sessions.sqlite3").exists() else "files"
    options = {"session_backend": backend, "task_backend": backend}
    payload = json.dumps(context_v3)
    if backend == "files":
        (home / "sessions/ses_fixture/context.json").write_text(payload, encoding="utf-8")
        store = LocalSessionStore(home)
    else:
        with closing(sqlite3.connect(home / "sessions.sqlite3")) as connection:
            connection.execute(
                "UPDATE latest_contexts SET payload=? WHERE session_id='ses_fixture'", (payload,)
            )
            connection.commit()
        store = SQLiteSessionStore(home / "sessions.sqlite3")
    with pytest.raises(PersistenceError):
        store.load_latest_context("ses_fixture")
    before = hashes(home)
    assert upgrade(home, check=True, **options)["status"] == "upgrade_required"
    assert hashes(home) == before
    report = upgrade(home, **options)
    assert report["status"] == "upgraded"
    context = store.load_latest_context("ses_fixture")
    assert context is not None
    assert (
        context.provider_context.model_dump(mode="json")
        == convert("context", context_v3)["provider_context"]
    )
    upgraded = hashes(home)
    assert upgrade(home, **options)["status"] == "current"
    assert hashes(home) == upgraded

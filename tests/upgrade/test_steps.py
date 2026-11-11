from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from contextlib import closing

import pytest

from ouro.core.agents import (
    AgentCatalogEntry,
    AgentCatalogSnapshot,
    AgentDefinition,
    DelegatedConstraints,
    DelegationRecord,
    EffectiveToolRef,
)
from ouro.core.harness import HarnessVersion
from ouro.core.state import RunRecord, SessionRecord
from ouro_upgrade import records, session_records
from ouro_upgrade.errors import UpgradeError
from ouro_upgrade.json_codec import loads
from ouro_upgrade.records import convert
from ouro_upgrade.session_storage import record_predicate
from ouro_upgrade.steps import UpgradeStep, advance
from tests.upgrade.conftest import FIXTURES


def test_future_run_version_retains_the_entire_historical_upgrade_path(monkeypatch) -> None:
    class FutureRun(RunRecord):
        schema_version: int = 12
        future_checkpoint: str | None

    def from_v11(value: dict) -> None:
        assert value["schema_version"] == 11
        assert value["driver"]["driver_id"] == "ouro.default"
        value["future_checkpoint"] = None

    monkeypatch.setitem(records.MODELS, "run", FutureRun)
    monkeypatch.setitem(records.VERSION_STEPS["run"], 11, UpgradeStep(12, from_v11))
    raw = loads((FIXTURES / "files/sessions/ses_fixture/runs/run_fixture.json").read_text("utf-8"))
    raw["active_tool_batch"] = None
    before = copy.deepcopy(raw)
    result = convert("run", raw)
    assert result["schema_version"] == 12
    assert result["future_checkpoint"] is None
    assert result["run_id"] == raw["run_id"]
    assert raw == before
    assert convert("run", result) == result


@pytest.mark.parametrize("target", [1, 2, 7, True])
def test_invalid_steps_fail_before_executing_a_transform(target: int) -> None:
    def unexpected(value: dict) -> None:
        pytest.fail("invalid transition must not execute")

    with pytest.raises(UpgradeError, match="invalid fixture upgrade step"):
        advance(
            "fixture", {"schema_version": 2}, target=6, steps={2: UpgradeStep(target, unexpected)}
        )


@pytest.mark.parametrize("version", [None, True, "2", 2.0, 3, 7])
def test_only_registered_source_versions_are_accepted(version: object) -> None:
    def unexpected(value: dict) -> None:
        pytest.fail("unknown source must not execute")

    with pytest.raises(UpgradeError, match="unsupported fixture version"):
        advance(
            "fixture", {"schema_version": version}, target=6, steps={2: UpgradeStep(6, unexpected)}
        )


def test_missing_transition_does_not_mutate_input() -> None:
    raw = {"schema_version": 2, "metadata": {"schema_version": 999}}
    before = copy.deepcopy(raw)

    def from_v2(value: dict) -> None:
        value["metadata"]["changed"] = True

    with pytest.raises(UpgradeError, match="missing fixture upgrade step from version 5"):
        advance("fixture", raw, target=6, steps={2: UpgradeStep(5, from_v2)})
    assert raw == before


def test_current_records_are_preserved_without_auditing_their_payloads() -> None:
    raw = {"schema_version": HarnessVersion.model_fields["schema_version"].default, "state": {}}
    assert convert("harness", raw) is raw


def test_new_harness_step_is_used_when_the_kind_previously_needed_no_upgrade(monkeypatch):
    class FutureHarness(HarnessVersion):
        schema_version: int = HarnessVersion.model_fields["schema_version"].default + 1
        category: str

    def categorize(value):
        value["category"] = "historical"

    raw = HarnessVersion(session_id="ses_fixture").model_dump(mode="json")
    monkeypatch.setitem(records.MODELS, "harness", FutureHarness)
    monkeypatch.setitem(
        records.VERSION_STEPS,
        "harness",
        {
            raw["schema_version"]: UpgradeStep(
                FutureHarness.model_fields["schema_version"].default, categorize
            )
        },
    )
    result = convert("harness", raw)
    assert result["schema_version"] == FutureHarness.model_fields["schema_version"].default
    assert result["category"] == "historical"
    assert convert("harness", result) is result


def historical_agent_catalog():
    definition = AgentDefinition(agent_id="reader", name="Reader", description="Inspect")
    reference = EffectiveToolRef(
        public_name="Read",
        canonical_tool_id="ouro.host/tools/Read",
        execution_contract_hash="read/1",
        effect="read_only",
        minimum_permission_mode="approval",
    )
    raw = AgentCatalogSnapshot(
        entries=(AgentCatalogEntry(definition=definition, effective_tools=(reference,)),)
    ).model_dump(mode="json")
    raw["schema_version"] = 1
    raw["spawn_policy"].update(allow_network=False, allow_external_paths=False)
    raw["entries"][0]["effective_tools"][0].update(
        requires_network=False,
        requires_mcp=False,
        requires_external_paths=False,
    )
    payload = {key: raw[key] for key in ("schema_version", "revision", "entries", "spawn_policy")}
    raw["contract_hash"] = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    return raw


def test_run_upgrade_converts_nested_tool_authority_without_changing_frozen_tools():
    raw = RunRecord(session_id="session").model_dump(mode="json")
    raw["schema_version"] = 10
    raw["agent_catalog"] = historical_agent_catalog()
    before = copy.deepcopy(raw)
    upgraded = convert("run", raw)
    record = records.validate(RunRecord, upgraded)
    catalog = record.agent_catalog
    assert catalog.schema_version == 2
    assert catalog.contract_hash != before["agent_catalog"]["contract_hash"]
    assert (
        catalog.created_at.isoformat().replace("+00:00", "Z")
        == before["agent_catalog"]["created_at"]
    )
    tools = catalog.resolve("reader").effective_tools
    assert [(tool.public_name, tool.minimum_permission_mode) for tool in tools] == [
        ("Read", "approval")
    ]
    assert raw == before
    assert convert("run", upgraded) is upgraded


def test_catalog_upgrade_rejects_changed_frozen_content():
    raw = historical_agent_catalog()
    raw["entries"][0]["effective_tools"][0]["minimum_permission_mode"] = "auto"
    with pytest.raises(UpgradeError, match="contract hash"):
        convert("agent_catalog", raw)


def test_delegation_upgrade_retains_workspace_tool_grants_and_identity():
    catalog = historical_agent_catalog()
    definition = AgentDefinition.model_validate(catalog["entries"][0]["definition"])
    raw = DelegationRecord(
        idempotency_key="request",
        root_session_id="root",
        parent_session_id="root",
        parent_run_id="run",
        task_description="Inspect",
        prompt_digest="sha256:prompt",
        definition=definition,
        constraints=DelegatedConstraints(workspace="workspace"),
    ).model_dump(mode="json")
    raw["schema_version"] = 2
    raw["effective_tools"] = catalog["entries"][0]["effective_tools"]
    raw["constraints"]["allow_external_paths"] = False
    before = copy.deepcopy(raw)
    upgraded = convert("delegation", raw)
    record = records.validate(DelegationRecord, upgraded)
    assert record.schema_version == 3
    assert record.constraints.workspace == "workspace"
    assert record.delegation_id == before["delegation_id"]
    assert record.effective_tools[0].minimum_permission_mode == "approval"
    assert raw == before
    assert convert("delegation", upgraded) is upgraded


def test_session_upgrade_preserves_workspace_and_call_permissions():
    raw = SessionRecord(workspace="workspace", approval_mode="deny").model_dump(mode="json")
    raw.update(schema_version=5, workspace_trusted=True)
    before = copy.deepcopy(raw)
    upgraded = convert("session", raw)
    session = records.validate(SessionRecord, upgraded)
    assert session.schema_version == 6
    assert session.workspace == "workspace"
    assert session.approval_mode == "deny"
    assert session.session_id == before["session_id"]
    assert "workspace_trusted" not in upgraded
    assert raw == before


def test_historical_session_headers_validate_once_after_assembly(monkeypatch):
    run = RunRecord(session_id="session").model_dump(mode="json")
    catalog = loads((FIXTURES / "records/frozen-agent-catalog.json").read_text("utf-8"))
    run.update(schema_version=5, agent_catalog=catalog)
    session = SessionRecord(session_id="session").model_dump(mode="json")
    session.update(schema_version=2, active_compaction_id=None)
    session.pop("context_selection")
    before = copy.deepcopy((run, session))
    validated = []
    original = records.validate

    def track(model, value):
        validated.append(model)
        return original(model, value)

    monkeypatch.setattr(records, "validate", track)
    monkeypatch.setattr(session_records, "validate", track)
    messages, runs, _, _ = session_records.messages_and_runs([], [run], 0)
    result = session_records.session(session, runs, messages, [])
    assert result.run_count == 1
    assert runs[0].agent_catalog.resolve("reader").definition.agent_id == "reader"
    assert validated.count(RunRecord) == validated.count(SessionRecord) == 1
    assert (run, session) == before


def test_native_selector_uses_registered_owned_paths(monkeypatch):
    from ouro_upgrade import retired_fields

    raw = RunRecord(session_id="session").model_dump(mode="json")
    raw["agent_catalog"] = AgentCatalogSnapshot().model_dump(mode="json")
    raw["agent_catalog"]["future_header"] = HarnessVersion(session_id="session").model_dump(
        mode="json"
    )
    raw["agent_catalog"]["future_header"]["retired_marker"] = None
    monkeypatch.setitem(
        records.NESTED_RECORDS,
        "agent_catalog",
        (
            *records.NESTED_RECORDS["agent_catalog"],
            ("future_header", "harness"),
        ),
    )
    monkeypatch.setitem(
        retired_fields.PATHS,
        "harness",
        (
            *retired_fields.PATHS["harness"],
            "retired_marker",
        ),
    )
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.execute("CREATE TABLE records(payload TEXT)")
        for expected in (True, False):
            assert records._needs_upgrade("run", raw) is expected
            connection.execute("DELETE FROM records")
            connection.execute("INSERT INTO records VALUES (?)", (json.dumps(raw),))
            selected = connection.execute(
                f"SELECT 1 FROM records WHERE {record_predicate('run')}"
            ).fetchone()
            assert (selected is not None) is expected
            raw["agent_catalog"]["future_header"].pop("retired_marker", None)


@pytest.mark.parametrize("native_home", ["sqlite"], indirect=True)
def test_native_schema_extension_requires_a_complete_path(native_home, monkeypatch):
    from ouro_upgrade import session_storage, sessions
    from ouro_upgrade.engine import upgrade
    from tests.upgrade.test_native_sessions import database
    from tests.upgrade.test_upgrade import hashes

    home, backend = native_home
    target = sessions.SQLITE_SCHEMA_VERSION
    monkeypatch.setattr(sessions, "SQLITE_SCHEMA_VERSION", target + 1)
    before = hashes(home)
    with pytest.raises(UpgradeError, match="missing Session SQLite upgrade step"):
        upgrade(home, session_backend=backend)
    assert hashes(home) == before

    def add_projection(connection):
        connection.execute("CREATE TABLE future_projection(value TEXT)")

    monkeypatch.setitem(
        session_storage.SQLITE_STEPS, target, UpgradeStep(target + 1, add_projection)
    )
    assert upgrade(home, session_backend=backend)["status"] == "upgraded"
    with closing(sqlite3.connect(database(home, backend))) as connection:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='sqlite_schema_version'"
        ).fetchone()[0] == str(target + 1)
        assert connection.execute("SELECT * FROM future_projection").fetchall() == []
    assert upgrade(home, session_backend=backend)["status"] == "current"


def test_task_container_target_matches_the_storage_writer(tmp_path):
    from ouro.automation.sqlite import SQLiteTaskRepository
    from ouro_upgrade.databases import TASK_SCHEMA_VERSION

    SQLiteTaskRepository(tmp_path).initialize()
    with closing(sqlite3.connect(tmp_path / "tasks.sqlite3")) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == TASK_SCHEMA_VERSION

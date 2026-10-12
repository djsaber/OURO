from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from ouro.automation.local_files import LocalTaskRepository
from ouro.automation.sqlite import SQLiteTaskRepository
from ouro.billing.local import LocalUsageLedger
from ouro.core.billing import UsageFilter
from ouro.core.errors import PersistenceError
from ouro.deployment import ensure_home_available
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro_upgrade import workspace
from ouro_upgrade.engine import upgrade
from ouro_upgrade.errors import UpgradeError
from ouro_upgrade.records import convert, loads
from ouro_upgrade.workspace import digest
from tests.upgrade.conftest import FIXTURES


def hashes(home: Path) -> dict[str, str | None]:
    return {
        p.relative_to(home).as_posix(): digest(p)
        for p in home.rglob("*")
        if p.is_file() and ".upgrade" not in p.relative_to(home).parts
    }


@pytest.mark.parametrize("historical_home", ["files", "sqlite"], indirect=True)
def test_historical_home_upgrades_for_current_readers(historical_home: Path) -> None:
    home = historical_home
    backend = "sqlite" if (home / "sessions.sqlite3").exists() else "files"
    options = {"session_backend": backend, "task_backend": backend}
    before = hashes(home)
    assert upgrade(home, check=True, **options)["status"] == "upgrade_required"
    assert hashes(home) == before
    report = upgrade(home, **options)
    assert report["status"] == "upgraded"
    upgraded = hashes(home)
    assert upgrade(home, **options)["status"] == "current"
    assert hashes(home) == upgraded
    assert len(list((home / ".upgrade/receipts").glob("*.json"))) == 1
    manifest = json.loads((home / str(report["manifest"])).read_text("utf-8"))
    for index, change in enumerate(manifest["changes"]):
        assert change["before"] == before.get(change["path"])
        if change["before"] is not None:
            assert digest(home / str(report["backup"]) / str(index)) == change["before"]
    store = (
        LocalSessionStore(home)
        if backend == "files"
        else SQLiteSessionStore(home / "sessions.sqlite3")
    )
    store.initialize()
    snapshot = store.load_session("ses_fixture")
    assert snapshot.session.message_count == 3
    assert snapshot.session.metadata["schema_version"] == 999
    assert [message.message_id for message in snapshot.messages] == [
        "msg_user",
        "msg_assistant",
        "msg_result",
    ]
    assert snapshot.messages[-1].tool_result.producer.kind == "model"
    run = store.load_run("ses_fixture", "run_fixture")
    assert run.driver.driver_id == "ouro.default"
    assert run.driver_checkpoint is None
    assert run.status == "completed"
    assert run.active_tool_batch.producer.kind == "model"
    assert run.active_tool_batch.calls[0].invocation.producer.kind == "model"
    assert run.active_tool_batch.calls[0].request.arguments["schema_version"] == 999
    context = store.load_latest_context("ses_fixture")
    assert context.context_digest == "sha256:" + "a" * 64
    assert context.source_messages[-1].tool_result.producer.kind == "model"
    assert context.injected_messages[-1].tool_result.producer.kind == "model"
    repo = LocalTaskRepository(home) if backend == "files" else SQLiteTaskRepository(home)
    repo.initialize()
    task = repo.get("task_fixture")
    assert task.configuration.target.prompt == "fixture scheduled prompt"
    assert not task.enabled and task.revision == 1 and task.owner_id is None
    (execution,) = repo.executions("task_fixture")
    assert execution.target.run_id == "run_fixture"
    assert execution.result is None and not execution.cancel_requested
    ledger = LocalUsageLedger(home)
    entry = ledger.get("usage_kept")
    assert entry.revision == 2 and entry.input_tokens == 123 and entry.plugin_id is None
    assert ledger.summarize(UsageFilter()).totals.input_tokens == 123
    assert ledger.summarize(UsageFilter()).totals.estimated_cost == 0.125
    assert len(ledger.audit().events) == 4


def test_current_readers_reject_fixture_before_upgrade(historical_home: Path) -> None:
    with pytest.raises(PersistenceError):
        LocalTaskRepository(historical_home).initialize()
    with pytest.raises(PersistenceError):
        LocalUsageLedger(historical_home).initialize()
    with pytest.raises(PersistenceError):
        LocalSessionStore(historical_home).load_session("ses_fixture")


def test_failure_blocks_entry_points_and_restores_all_bytes(
    historical_home: Path, monkeypatch
) -> None:
    home = historical_home
    before = hashes(home)
    original = workspace.install
    calls = 0

    def interrupt(source, target, mode):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("simulated write failure")
        original(source, target, mode)

    monkeypatch.setattr(workspace, "install", interrupt)
    with pytest.raises(UpgradeError, match="I/O"):
        upgrade(home)
    assert hashes(home) != before
    with pytest.raises(PersistenceError, match="--restore"):
        ensure_home_available(home)
    from ouro.cli.main import main
    from ouro.local_application import resolve_local_home

    assert main(["usage", "list", "--home", str(home)]) != 0
    with pytest.raises(PersistenceError, match="--restore"):
        resolve_local_home(home)
    with pytest.raises(UpgradeError, match="unfinished"):
        upgrade(home)
    # Restoring is repeatable after another interruption and never consumes backups.
    calls = 0
    with pytest.raises(UpgradeError):
        upgrade(home, restore=True)
    monkeypatch.setattr(workspace, "install", original)
    assert upgrade(home, restore=True)["status"] == "restored"
    assert hashes(home) == before
    assert ensure_home_available(home) == home
    assert upgrade(home)["status"] == "upgraded"


@pytest.mark.parametrize("damage", ["version", "identity", "extra", "commit", "occurrence"])
def test_unsupported_data_is_rejected_before_business_writes(historical_home: Path, damage) -> None:
    home = historical_home
    path = home / "sessions/ses_fixture/runs/run_fixture.json"
    value = loads(path.read_text("utf-8"))
    if damage == "version":
        value["schema_version"] = 99
    elif damage == "identity":
        del value["run_id"]
    elif damage == "extra":
        value["unknown_native_field"] = "secret-input-must-not-be-logged"
    elif damage == "commit":
        (home / "sessions/ses_fixture/commit.json").write_text("{}", encoding="utf-8")
    else:
        next((home / "tasks/records/task_fixture/occurrences").iterdir()).unlink()
    path.write_text(json.dumps(value), encoding="utf-8")
    before = hashes(home)
    with pytest.raises(UpgradeError) as error:
        upgrade(home)
    assert "secret-input" not in str(error.value)
    assert hashes(home) == before
    assert not (home / ".upgrade/in-progress.json").exists()


def test_record_conversion_preserves_opaque_values_and_digest() -> None:
    raw = loads(
        (FIXTURES / "files/sessions/ses_fixture/operations/op_fixture.json").read_text("utf-8")
    )
    result = convert("commit", raw)
    assert result["output"] == raw["output"]
    assert result["request_digest"] == raw["request_digest"]
    assert result["messages"][-1]["metadata"] == raw["messages"][-1]["metadata"]
    assert raw["messages"][-1]["schema_version"] == 3
    assert convert("commit", result) == result


def test_mixed_journal_keeps_current_prefix_bytes(historical_home: Path) -> None:
    path = historical_home / "sessions/ses_fixture/messages.jsonl"
    rows = path.read_text("utf-8").splitlines()
    prefix = json.dumps(convert("message", loads(rows[0])), ensure_ascii=False) + "\r\n"
    path.write_bytes(prefix.encode("utf-8") + ("\n".join(rows[1:]) + "\n").encode("utf-8"))
    upgrade(historical_home)
    assert path.read_bytes().startswith(prefix.encode("utf-8"))


@pytest.mark.parametrize("text", ['{"a":1,"a":2}', '{"a":NaN}', "[]"])
def test_ambiguous_json_is_rejected(text: str) -> None:
    with pytest.raises(UpgradeError):
        loads(text)


def test_unselected_backend_and_private_files_are_preserved(historical_home: Path) -> None:
    home = historical_home
    (home / "sessions.sqlite3").write_bytes(b"unsupported inactive database")
    (home / "trash").mkdir()
    (home / "trash/old.json").write_text('{"schema_version":0}', encoding="utf-8")
    (home / "private.bin").write_bytes(b"opaque\x00content")
    before = hashes(home)
    report = upgrade(home)
    after = hashes(home)
    for path in before.keys() - set(report["files"]):
        assert after[path] == before[path]
    assert report["preserved_outside_scope"]


def test_upgrade_imports_no_runtime_or_plugins() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import ouro_upgrade.engine; "
                "assert not any(k.startswith(('ouro.application.', 'ouro.plugins.', "
                "'ouro.builtin_plugins.', 'ouro.tui.')) for k in sys.modules)"
            ),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_committed_wal_is_included_and_source_group_restorable(
    historical_home: Path, monkeypatch
) -> None:
    home = historical_home
    path = home / "billing/usage.sqlite3"
    script = """
import json,os,sqlite3,sys
c=sqlite3.connect(sys.argv[1])
c.execute('PRAGMA journal_mode=WAL')
c.execute('PRAGMA wal_autocheckpoint=0')
row=c.execute("SELECT payload FROM entry_records WHERE entry_id='usage_kept'").fetchone()
payload=json.loads(row[0])
payload['input_tokens']=345
c.execute("UPDATE entry_records SET payload=? WHERE entry_id='usage_kept'",(json.dumps(payload),))
c.execute("UPDATE entries SET input_tokens=345 WHERE entry_id='usage_kept'")
c.commit()
os._exit(0)
"""
    subprocess.run([sys.executable, "-c", script, str(path)], check=True)
    assert path.with_name(path.name + "-wal").stat().st_size > 0
    before = hashes(home)
    original = workspace.install

    def interrupt(source, target, mode):
        original(source, target, mode)
        if target == path:
            raise OSError("interrupted after main database replacement")

    monkeypatch.setattr(workspace, "install", interrupt)
    with pytest.raises(UpgradeError):
        upgrade(home)
    monkeypatch.setattr(workspace, "install", original)
    upgrade(home, restore=True)
    assert hashes(home) == before
    upgrade(home)
    assert not path.with_name(path.name + "-wal").exists()
    assert LocalUsageLedger(home).get("usage_kept").input_tokens == 345


def test_damaged_backup_does_not_allow_partial_restore(historical_home: Path, monkeypatch) -> None:
    home = historical_home
    original = workspace.install
    monkeypatch.setattr(workspace, "install", lambda *_: (_ for _ in ()).throw(OSError()))
    with pytest.raises(UpgradeError):
        upgrade(home)
    marker = loads((home / ".upgrade/in-progress.json").read_text("utf-8"))
    backup = home / ".upgrade" / marker["upgrade_id"] / "before/0"
    backup.write_bytes(b"damaged")
    before = hashes(home)
    monkeypatch.setattr(workspace, "install", original)
    with pytest.raises(UpgradeError, match="damaged backup"):
        upgrade(home, restore=True)
    assert hashes(home) == before
    assert (home / ".upgrade/in-progress.json").exists()


def test_cli_checks_home_without_selecting_destination(historical_home: Path, capsys) -> None:
    from ouro_upgrade.cli import main

    assert main(["--home", str(historical_home), "--check"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "upgrade_required"
    with pytest.raises(SystemExit):
        main(["--home", str(historical_home), "--check", "--restore"])

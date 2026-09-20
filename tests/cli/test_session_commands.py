from __future__ import annotations

import json
from pathlib import Path

import pytest

from ouro.application.runtime import RuntimeService
from ouro.cli.main import main
from ouro.cli.parser import build_parser
from ouro.cli.session_commands import _print_compaction_result
from ouro.config.compaction import CompactionConfig
from ouro.core.adapter import RuntimeAdapter
from ouro.core.agents import AgentDefinition, DelegationRecord
from ouro.core.compaction import (
    CompactionArtifact,
    CompactionCommandResult,
    CompactionPolicy,
    CompactionRequest,
    CompactionStrategyRegistry,
)
from ouro.core.messages import MessageRecord
from ouro.core.provider import ModelMetadata, ProviderCheckResult, ProviderStreamEvent
from ouro.core.session_commits import SessionCommit, SessionCommitStep
from ouro.core.state import RunRecord, SessionRecord
from ouro.local_application import build_local_session_services
from ouro.sessions import LocalSessionStore, SessionStore
from ouro.tools import ToolRegistry
from ouro.transfer.archive import read_archive


@pytest.mark.parametrize("backend", ["files", "sqlite"])
def test_export_contains_canonical_harness_checkpoints_and_receipts(tmp_path, capsys, backend):
    services = build_local_session_services(tmp_path, session_backend=backend)
    store = services._store
    session = SessionRecord()
    store.create_session(session)
    message = MessageRecord.text(session_id=session.session_id, role="user", text="input")
    commit = store.commit_session(
        SessionCommit(before=session, steps=(SessionCommitStep(message=message),))
    )
    assert (
        main(
            [
                "data",
                "export",
                "--kind",
                "sessions",
                "--id",
                session.session_id,
                "--path",
                str(tmp_path / "session.zip"),
                "--home",
                str(tmp_path),
                "--session-backend",
                backend,
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "ok"
    payload = read_archive(tmp_path / "session.zip", "sessions").manifest.data["bundle"][
        "sessions"
    ][0]["snapshot"]
    assert payload["session"]["history_epoch"] == 0
    assert payload["session"]["harness_version"] == session.harness_version
    assert payload["harness_versions"][0]["version_id"] == session.harness_version
    assert payload["commits"][0]["operation_id"] == commit.operation_id


def test_session_list_shows_new_session(tmp_path, capsys) -> None:
    assert main(["run", "--print", "--home", str(tmp_path), "hello"]) == 0

    code = main(["session", "list", "--home", str(tmp_path)])
    captured = capsys.readouterr()

    assert code == 0
    assert "Sessions:" in captured.out


def test_session_list_json(tmp_path, capsys) -> None:
    run_code = main(["run", "--json", "--home", str(tmp_path), "hello"])
    run_payload = json.loads(capsys.readouterr().out)
    assert run_code == 0

    code = main(["session", "list", "--home", str(tmp_path), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["status"] == "ok"
    session_ids = [session["session_id"] for session in payload["sessions"]]
    assert run_payload["session_id"] in session_ids


def test_session_show(tmp_path, capsys) -> None:
    run_code = main(["run", "--json", "--home", str(tmp_path), "hello"])
    run_payload = json.loads(capsys.readouterr().out)
    assert run_code == 0

    code = main(["session", "show", run_payload["session_id"], "--home", str(tmp_path)])
    captured = capsys.readouterr()

    assert code == 0
    assert run_payload["session_id"] in captured.out


def test_session_show_missing(tmp_path, capsys) -> None:
    code = main(["session", "show", "ses_missing", "--home", str(tmp_path)])
    captured = capsys.readouterr()

    assert code == 1
    assert "session not found" in captured.err


def test_session_context_reports_latest_request_status_and_provider_content(
    tmp_path,
    capsys,
) -> None:
    assert main(["run", "--json", "--home", str(tmp_path), "hello context"]) == 0
    run_payload = json.loads(capsys.readouterr().out)
    session_id = run_payload["session_id"]

    assert main(["session", "context", session_id, "--home", str(tmp_path), "--json"]) == 0
    status_payload = json.loads(capsys.readouterr().out)
    assert status_payload["context_status"]["metrics"]["estimated_input_tokens"] > 0
    assert status_payload["context_status"]["context_id"].startswith("ctx_")
    assert status_payload["context_status"]["prepared_at"]
    assert status_payload["context_status"]["provider_reported_input_tokens"] is not None
    assert "provider_context" not in status_payload

    assert (
        main(
            [
                "session",
                "context",
                session_id,
                "--home",
                str(tmp_path),
                "--content",
                "--json",
            ]
        )
        == 0
    )
    content_payload = json.loads(capsys.readouterr().out)
    assert "<ouro-system-information>" in content_payload["provider_context"]["system_prompt"]
    assert content_payload["provider_context"]["messages"][0]["role"] == "user"
    assert content_payload["provider_context"]["messages"][0]["content"][0]["text"] == (
        "hello context"
    )
    assert len(content_payload["provider_context"]["messages"]) == 1
    assert "message_id" not in content_payload["provider_context"]["messages"][0]
    assert content_payload["context_digest"].startswith("sha256:")


def test_session_remove_with_force(tmp_path, capsys) -> None:
    run_code = main(["run", "--json", "--home", str(tmp_path), "hello"])
    run_payload = json.loads(capsys.readouterr().out)
    assert run_code == 0

    code = main(
        [
            "session",
            "remove",
            run_payload["session_id"],
            "--home",
            str(tmp_path),
            "--force",
        ]
    )
    captured = capsys.readouterr()

    assert code == 0
    assert "removed" in captured.out
    store = LocalSessionStore(tmp_path)
    assert not (store.sessions_dir / run_payload["session_id"]).exists()
    assert any(
        (path / "sessions" / run_payload["session_id"] / "session.json").exists()
        for path in store.trash_sessions_dir.iterdir()
    )


def test_session_remove_child_cascades_old_incompatible_agent_tree(tmp_path, capsys) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    root = SessionRecord(session_id="ses_old_root")
    child = SessionRecord(
        session_id="ses_old_child",
        session_kind="subagent",
        root_session_id=root.session_id,
        parent_session_id=root.session_id,
        delegation_id="dlg_old",
    )
    store.create_session(root)
    store.create_session(child)
    relation = store.create_delegation(
        DelegationRecord(
            delegation_id="dlg_old",
            idempotency_key="old/remove",
            root_session_id=root.session_id,
            parent_session_id=root.session_id,
            parent_run_id="run_old",
            agent_id="general",
            task_description="Old child",
            prompt_digest="sha256:old",
            definition=AgentDefinition(
                agent_id="general",
                name="General",
                description="Old child",
            ),
        )
    )
    payload = relation.model_dump(mode="json")
    payload["execution_mode"] = "foreground"
    relation_path = (
        store.sessions_dir / root.session_id / "delegations" / f"{relation.delegation_id}.json"
    )
    relation_path.write_text(json.dumps(payload), encoding="utf-8")

    code = main(
        [
            "session",
            "remove",
            child.session_id,
            "--home",
            str(tmp_path),
            "--force",
            "--json",
        ]
    )
    result = json.loads(capsys.readouterr().out)

    assert code == 0
    assert set(result["removed"]) == {root.session_id, child.session_id}
    assert store.list_sessions() == []
    trash_root = store.trash_sessions_dir / result["trash_id"] / "sessions"
    assert (trash_root / root.session_id / "session.json").exists()
    assert (trash_root / child.session_id / "session.json").exists()


def test_session_rename_archive_pin_and_filtered_list(tmp_path, capsys) -> None:
    run_code = main(["run", "--json", "--home", str(tmp_path), "hello"])
    run_payload = json.loads(capsys.readouterr().out)
    assert run_code == 0
    session_id = run_payload["session_id"]

    assert main(["session", "rename", session_id, "Focused work", "--home", str(tmp_path)]) == 0
    capsys.readouterr()
    assert main(["session", "tags", session_id, "review", "urgent", "--home", str(tmp_path)]) == 0
    capsys.readouterr()
    assert main(["session", "pin", session_id, "--home", str(tmp_path)]) == 0
    capsys.readouterr()
    assert main(["session", "archive", session_id, "--home", str(tmp_path)]) == 0
    capsys.readouterr()

    code = main(
        [
            "session",
            "list",
            "--home",
            str(tmp_path),
            "--json",
            "--search",
            "focused",
            "--archived",
            "--pinned",
            "--tag",
            "review",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert [entry["session_id"] for entry in payload["sessions"]] == [session_id]
    assert payload["sessions"][0]["title_source"] == "user"
    assert payload["sessions"][0]["title_locked"] is True
    assert payload["sessions"][0]["tags"] == ["review", "urgent"]


def test_session_list_maps_shell_friendly_status_and_sort_values() -> None:
    args = build_parser().parse_args(
        [
            "session",
            "list",
            "--status",
            "waiting-approval",
            "--sort",
            "created-desc",
        ]
    )

    assert args.statuses == ["waiting_approval"]
    assert args.sort == "created_desc"


def test_session_branch_and_replay_commands_use_application_facade(tmp_path, capsys) -> None:
    run_code = main(["run", "--json", "--home", str(tmp_path), "branch me"])
    run_payload = json.loads(capsys.readouterr().out)
    assert run_code == 0
    session_id = run_payload["session_id"]

    store = LocalSessionStore(tmp_path)
    store.initialize()
    source = store.load_session(session_id)
    branch_point = source.messages[-1].message_id

    branch_code = main(
        [
            "session",
            "branch",
            session_id,
            branch_point,
            "--home",
            str(tmp_path),
            "--json",
        ]
    )
    branch_payload = json.loads(capsys.readouterr().out)
    assert branch_code == 0
    assert branch_payload["session"]["parent_session_id"] == session_id

    replay_code = main(["session", "replay", session_id, "--home", str(tmp_path), "--json"])
    replay_payload = json.loads(capsys.readouterr().out)
    assert replay_code == 0
    assert replay_payload["frames"]
    assert replay_payload["frames"][0]["event"]["event_type"] == "run_started"


@pytest.mark.parametrize("json_mode", [False, True])
def test_session_data_export_to_archive(tmp_path, capsys, json_mode) -> None:
    assert main(["run", "--json", "--home", str(tmp_path), "hello"]) == 0
    run_payload = json.loads(capsys.readouterr().out)
    output = tmp_path / "export.zip"
    args = [
        "data",
        "export",
        "--kind",
        "sessions",
        "--id",
        run_payload["session_id"],
        "--home",
        str(tmp_path),
        "--path",
        str(output),
    ]
    if json_mode:
        args.append("--json")
    assert main(args) == 0
    captured = capsys.readouterr()
    if json_mode:
        assert json.loads(captured.out)["status"] == "ok"
    else:
        assert "Exported sessions" in captured.out
    payload = read_archive(output, "sessions").manifest.data["bundle"]["sessions"][0]["snapshot"]
    assert payload["session"]["session_id"] == run_payload["session_id"]
    assert any(event["event_type"] == "run_completed" for event in payload["events"])


def test_session_messages_and_events(tmp_path, capsys) -> None:
    run_code = main(["run", "--json", "--home", str(tmp_path), "hello"])
    run_payload = json.loads(capsys.readouterr().out)
    assert run_code == 0

    messages_code = main(
        ["session", "messages", run_payload["session_id"], "--home", str(tmp_path), "--json"]
    )
    messages_payload = json.loads(capsys.readouterr().out)
    assert messages_code == 0
    assert any(message["role"] == "user" for message in messages_payload["messages"])

    events_code = main(
        ["session", "events", run_payload["session_id"], "--home", str(tmp_path), "--json"]
    )
    events_payload = json.loads(capsys.readouterr().out)
    assert events_code == 0
    assert any(event["event_type"] == "run_completed" for event in events_payload["events"])


def test_session_message_command_exposes_page_cursor(tmp_path, capsys) -> None:
    run_code = main(["run", "--json", "--home", str(tmp_path), "hello"])
    run_payload = json.loads(capsys.readouterr().out)
    assert run_code == 0

    first_code = main(
        [
            "session",
            "messages",
            run_payload["session_id"],
            "--home",
            str(tmp_path),
            "--limit",
            "1",
            "--json",
        ]
    )
    first = json.loads(capsys.readouterr().out)
    assert first_code == 0
    assert len(first["messages"]) == 1
    assert first["next_cursor"] is not None

    second_code = main(
        [
            "session",
            "messages",
            run_payload["session_id"],
            "--home",
            str(tmp_path),
            "--cursor",
            str(first["next_cursor"]),
            "--limit",
            "1",
            "--json",
        ]
    )
    second = json.loads(capsys.readouterr().out)
    assert second_code == 0
    assert len(second["messages"]) == 1
    assert second["next_cursor"] is None


class _DummyProvider:
    provider_id = "dummy"

    async def stream(self, *, model, system_prompt="", messages, tools=None, request_context=None):
        del model, system_prompt, messages, tools
        yield ProviderStreamEvent(type="text_delta", text="ok")
        yield ProviderStreamEvent(type="done")

    async def check_connection(
        self, model: ModelMetadata, *, request_context=None
    ) -> ProviderCheckResult:
        return ProviderCheckResult(provider_id=self.provider_id, ok=True, model_id=model.model_id)


class _RecordingStrategy:
    strategy_id = "test.summary"
    version = "1"

    def __init__(self) -> None:
        self.requests: list[CompactionRequest] = []

    async def compact(self, request: CompactionRequest, *, signal=None):
        del signal
        self.requests.append(request)
        return CompactionArtifact(
            summary=f"summary-{len(self.requests)}",
            usage={"input_tokens": 10, "output_tokens": 2},
            metadata={},
        )


def _compaction_runtime(tmp_path: Path) -> RuntimeService:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    registry = CompactionStrategyRegistry()
    registry.register(_RecordingStrategy())
    return RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            _DummyProvider(),
            ModelMetadata(
                model_id="dummy",
                provider_id="dummy",
                context_window=3_000,
                max_output_tokens=500,
            ),
        ),
        tools=ToolRegistry(),
        system_prompt="",
        compaction_strategies=registry,
        compaction_config=CompactionConfig(
            strategy_id="test.summary",
            policy=CompactionPolicy(
                trigger_ratio=0.3,
                target_ratio=0.2,
            ),
        ),
    )


def _create_session_with_turns(store: SessionStore, count: int) -> str:
    session = SessionRecord(title="compact test")
    store.create_session(session)
    for index in range(count):
        store.append_message(
            MessageRecord.text(
                session_id=session.session_id,
                role="user",
                text=f"user {index}",
                sequence=2 * index + 1,
            )
        )
        store.append_message(
            MessageRecord.text(
                session_id=session.session_id,
                role="assistant",
                text=f"assistant {index}",
                sequence=2 * index + 2,
            )
        )
    return session.session_id


def test_session_compact_success(monkeypatch, tmp_path, capsys) -> None:
    runtime = _compaction_runtime(tmp_path)
    monkeypatch.setattr(
        "ouro.cli.session_commands.build_default_runtime",
        lambda home, **kwargs: runtime,
    )
    session_id = _create_session_with_turns(runtime.store, count=3)

    code = main(["session", "compact", session_id, "--home", str(tmp_path)])
    captured = capsys.readouterr()

    assert code == 0
    assert "Compaction completed" in captured.out
    assert "messages covered" in captured.out


def test_session_compact_skipped(monkeypatch, tmp_path, capsys) -> None:
    runtime = _compaction_runtime(tmp_path)
    monkeypatch.setattr(
        "ouro.cli.session_commands.build_default_runtime",
        lambda home, **kwargs: runtime,
    )
    session_id = _create_session_with_turns(runtime.store, count=1)

    code = main(["session", "compact", session_id, "--home", str(tmp_path)])
    captured = capsys.readouterr()

    assert code == 0
    assert "skipped" in captured.out.lower()


def test_session_compact_missing(monkeypatch, tmp_path, capsys) -> None:
    runtime = _compaction_runtime(tmp_path)
    monkeypatch.setattr(
        "ouro.cli.session_commands.build_default_runtime",
        lambda home, **kwargs: runtime,
    )

    code = main(["session", "compact", "ses_missing", "--home", str(tmp_path)])
    captured = capsys.readouterr()

    assert code == 1
    assert "session not found" in captured.err


def test_session_compact_json(monkeypatch, tmp_path, capsys) -> None:
    runtime = _compaction_runtime(tmp_path)
    monkeypatch.setattr(
        "ouro.cli.session_commands.build_default_runtime",
        lambda home, **kwargs: runtime,
    )
    session_id = _create_session_with_turns(runtime.store, count=3)

    code = main(["session", "compact", session_id, "--home", str(tmp_path), "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert code == 0
    assert payload["status"] == "ok"
    assert payload["session_id"] == session_id
    assert payload["skipped"] is False
    assert payload["compaction_id"] is not None
    assert payload["covered_message_count"] > 0


def test_session_compact_aborted_is_not_reported_as_skipped(capsys) -> None:
    session = SessionRecord(session_id="ses_compaction_aborted")
    result = CompactionCommandResult(
        session=session,
        run=RunRecord(
            run_id="run_compaction_aborted",
            session_id=session.session_id,
            kind="compaction",
            status="aborted",
        ),
    )

    assert _print_compaction_result(result, json_mode=False) == 0
    assert capsys.readouterr().out == "Compaction aborted: run_compaction_aborted\n"

    assert _print_compaction_result(result, json_mode=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "aborted"
    assert payload["skipped"] is False


# Keep pytest from treating helper classes as test classes.
_DummyProvider.__test__ = False  # type: ignore[attr-defined]
_RecordingStrategy.__test__ = False  # type: ignore[attr-defined]


def test_session_parser_does_not_expose_history_migration():
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(["session", "migrate-harness", "ses_test"])
    assert error.value.code == 2

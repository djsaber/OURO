from __future__ import annotations

import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from ouro.application.data_transfer import DataTransferRequest
from ouro.config.agents import AgentDefinitionsConfigManager
from ouro.config.file import JsonConfigFile
from ouro.config.prompts import PromptConfigManager, PromptDraft
from ouro.config.provider_template_store import ProviderTemplateStore
from ouro.config.providers import ProviderConfigManager
from ouro.config.skills import ManagedSkillRecord, SkillConfigManager
from ouro.core.agents import AgentDefinition, DelegationRecord
from ouro.core.errors import ConfigError, PersistenceError, SessionBusyError
from ouro.core.harness import HarnessState
from ouro.core.messages import MessageRecord
from ouro.core.session_commits import SessionCommit, SessionCommitStep
from ouro.core.session_extensions import SessionExtensionStateRecord
from ouro.core.state import RunRecord, SessionRecord
from ouro.core.tools import ToolCallRequest
from ouro.local_application import build_local_data_transfer_service, build_local_session_services
from ouro.mcp.configuration import MCPConfigStore, MCPServerConfigUpdate
from ouro.resources.local import LocalResourceStore
from ouro.sessions.sqlite import SQLiteSessionStore
from ouro.transfer.archive import (
    ArchiveManifest,
    DataArchive,
    DataTransferError,
    read_archive,
    write_archive,
)


def transfer(home, action, kind, path, *, ids=(), replace=False, backend="files"):
    return build_local_data_transfer_service(home, session_backend=backend).transfer(
        DataTransferRequest(action=action, kind=kind, path=path, identities=ids, replace=replace)
    )


def _configure(home: Path) -> None:
    ProviderConfigManager(home).create_provider(
        display_name="Example",
        provider_type="custom",
        protocol="openai-chat-completions",
        base_url="https://example.test/v1",
        api_key_env="EXAMPLE_KEY",
    )
    MCPConfigStore(home).set_by_name("Example", MCPServerConfigUpdate(command="example-server"))
    AgentDefinitionsConfigManager(home).create(
        AgentDefinition(
            agent_id="review",
            name="Review",
            description="Review changes",
            system_prompt="Review carefully",
        ),
        expected_revision=0,
    )
    ProviderTemplateStore(home).catalog()


@pytest.mark.parametrize("kind", ["providers", "provider-templates", "mcp", "agents"])
def test_configuration_round_trip_replacement_and_domain_isolation(tmp_path, kind):
    source, target = tmp_path / "source", tmp_path / "target"
    _configure(source)
    PromptConfigManager(target).save_entry(
        "replacement",
        PromptDraft(text="Keep target settings", enabled=True),
        expected_revision=0,
    )
    archive = tmp_path / "config.zip"
    transfer(source, "export", kind, archive)
    transfer(target, "import", kind, archive)
    source_raw = JsonConfigFile(source, name=f"{kind}.json").read()
    target_raw = JsonConfigFile(target, name=f"{kind}.json").read()
    assert target_raw == source_raw
    assert PromptConfigManager(target).snapshot().config.system_prompt == "Keep target settings"
    with pytest.raises(DataTransferError, match="exists"):
        transfer(target, "import", kind, archive)
    transfer(target, "import", kind, archive, replace=True)
    if kind == "agents":
        assert AgentDefinitionsConfigManager(target).snapshot().revision == 2
    assert not (source / "config" / "config.json").exists()


def test_prompt_configuration_round_trip_rebases_revision_and_applies_without_restart(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    source_prompts = PromptConfigManager(source)
    source_prompts.save_entry(
        "replacement",
        PromptDraft(description="Primary", text="Use the source prompt", enabled=True),
        expected_revision=0,
    )
    source_prompts.save_entry(
        "addition",
        PromptDraft(description="Appendix", text="Preserve this addition", enabled=True),
        expected_revision=1,
    )
    target_prompts = PromptConfigManager(target)
    for revision in range(3):
        target_prompts.save_entry(
            "addition",
            PromptDraft(text=f"Target prompt {revision}"),
            expected_revision=revision,
        )

    archive = tmp_path / "prompts.zip"
    transfer(source, "export", "prompts", archive)
    with pytest.raises(DataTransferError, match="exists"):
        transfer(target, "import", "prompts", archive)

    result = transfer(target, "import", "prompts", archive, replace=True)

    expected = source_prompts.snapshot().config.model_copy(update={"revision": 4})
    assert target_prompts.snapshot().config == expected
    assert result.count == 1
    assert not result.restart_required


def test_invalid_prompt_archive_cannot_change_target(tmp_path):
    prompts = PromptConfigManager(tmp_path)
    prompts.save_entry(
        "replacement", PromptDraft(text="Keep this prompt", enabled=True), expected_revision=0
    )
    before = prompts.config_path.read_bytes()
    archive = tmp_path / "invalid-prompts.zip"
    write_archive(
        archive,
        DataArchive(
            ArchiveManifest(
                kind="prompts",
                data={
                    "prompts": {
                        "revision": 1,
                        "replacements": [
                            {"id": "first", "text": "First", "enabled": True},
                            {"id": "second", "text": "Second", "enabled": True},
                        ],
                        "additions": [],
                    }
                },
            )
        ),
        replace=False,
    )

    with pytest.raises(DataTransferError, match="invalid prompts"):
        transfer(tmp_path, "import", "prompts", archive, replace=True)
    assert prompts.config_path.read_bytes() == before


def test_invalid_configuration_archive_cannot_change_target(tmp_path):
    _configure(tmp_path)
    target = tmp_path / "config" / "mcp.json"
    before = target.read_bytes()
    archive = tmp_path / "invalid.zip"
    write_archive(
        archive,
        DataArchive(
            ArchiveManifest(
                kind="mcp",
                data={
                    "mcp": {"servers": {"bad": {"transport": "invalid", "secret": "sensitive"}}},
                },
            )
        ),
        replace=False,
    )
    with pytest.raises(DataTransferError, match="invalid mcp") as error:
        transfer(tmp_path, "import", "mcp", archive, replace=True)
    assert "sensitive" not in str(error.value)
    assert target.read_bytes() == before


def test_runtime_rejects_obsolete_shared_config_without_blocking_provider_inspection(tmp_path):
    from ouro.application.runtime_configuration import RuntimeConfigurationResolver

    JsonConfigFile(tmp_path).write({"providers": {"default": "faux"}})
    assert ProviderConfigManager(tmp_path).snapshot().default_provider_id == "faux"
    with pytest.raises(ConfigError, match="unsupported shared config sections"):
        RuntimeConfigurationResolver(tmp_path).snapshot()


def _create_session(store, identity="ses_test", **kwargs):
    session = SessionRecord(session_id=identity, **kwargs)
    store.create_session(session)
    run = RunRecord(session_id=identity, status="completed")
    store.save_run(run)
    message = MessageRecord.text(
        session_id=identity, run_id=run.run_id, role="user", text="History"
    )
    result = store.commit_session(
        SessionCommit(
            before=session,
            steps=(SessionCommitStep(message=message),),
        )
    )
    return result.session, run


@pytest.mark.parametrize("source_backend", ["files", "sqlite"])
@pytest.mark.parametrize("target_backend", ["files", "sqlite"])
def test_session_round_trip_across_backends_with_attachments(
    tmp_path, source_backend, target_backend
):
    source, target = tmp_path / "source", tmp_path / "target"
    services = build_local_session_services(source, session_backend=source_backend)
    session, _run = _create_session(services._store)
    attachment = tmp_path / "attachment.txt"
    attachment.write_bytes(b"portable attachment")
    services.resources.import_resource(session.session_id, attachment)
    _create_session(services._store, "ses_other")
    archive = tmp_path / "sessions.zip"
    assert transfer(source, "export", "sessions", archive, backend=source_backend).count == 2
    assert transfer(target, "import", "sessions", archive, backend=target_backend).count == 2
    imported = build_local_session_services(target, session_backend=target_backend)
    assert imported._store.load_session(session.session_id) == services._store.load_session(
        session.session_id
    )
    assert imported._store.load_resource_references(
        session.session_id
    ) == services._store.load_resource_references(session.session_id)
    assert (
        LocalResourceStore(target).load_content(
            session.session_id, imported._store.load_resource_references(session.session_id)[0]
        )
        == b"portable attachment"
    )
    repeated = transfer(target, "import", "sessions", archive, backend=target_backend)
    assert repeated.count == 0 and repeated.skipped == 2 and repeated.replaced == 0
    assert len(imported.sessions.catalog().entries) == 2


def test_session_attachment_import_keeps_complete_named_files(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    services = build_local_session_services(source)
    session, _ = _create_session(services._store)
    expected = []
    for index in range(8):
        path = tmp_path / f"attachment-{index}.txt"
        content = f"attachment {index}".encode()
        path.write_bytes(content)
        services.resources.import_resource(session.session_id, path)
        expected.append(content)
    archive = tmp_path / "sessions.zip"
    transfer(source, "export", "sessions", archive)
    transfer(target, "import", "sessions", archive)
    imported = build_local_session_services(target)
    records = imported._store.load_resource_references(session.session_id)
    assert len(records) == len(expected)
    store = LocalResourceStore(target)
    for record in records:
        assert store.load_content(session.session_id, record) in expected
        assert store.path_for(session.session_id, record).name.startswith("attachment-")
    assert not (target / "resources").exists()


@pytest.mark.parametrize("backend", ["files", "sqlite"])
def test_selected_session_import_from_full_archive_and_missing_id(tmp_path, backend):
    source = tmp_path / "source"
    services = build_local_session_services(source, session_backend=backend)
    _create_session(services._store, "ses_one")
    _create_session(services._store, "ses_two")
    archive = tmp_path / "all.zip"
    transfer(source, "export", "sessions", archive, backend=backend)
    target = tmp_path / "target"
    transfer(target, "import", "sessions", archive, ids=("ses_two",), backend=backend)
    actual = build_local_session_services(target, session_backend=backend)
    assert [record.session_id for record in actual._store.list_sessions()] == ["ses_two"]
    with pytest.raises(DataTransferError, match="not found in archive"):
        transfer(target, "import", "sessions", archive, ids=("ses_missing",), backend=backend)
    partial = tmp_path / "partial.zip"
    transfer(source, "export", "sessions", partial, ids=("ses_one",), backend=backend)
    payload = read_archive(partial, "sessions").manifest.data
    assert len(payload["bundle"]["sessions"]) == 1
    transfer(target, "import", "sessions", archive, ids=("ses_one",), backend=backend)
    assert len(actual._store.list_sessions()) == 2


def test_selected_child_transfers_complete_agent_tree(tmp_path):
    services = build_local_session_services(tmp_path / "source")
    root, run = _create_session(services._store, "ses_root")
    definition = AgentDefinition(
        agent_id="review", name="Review", description="Review", system_prompt="Review"
    )
    relation = DelegationRecord(
        root_session_id=root.session_id,
        parent_session_id=root.session_id,
        parent_run_id=run.run_id,
        child_session_id="ses_child",
        agent_id=definition.agent_id,
        definition=definition,
        idempotency_key="example",
        task_description="Review",
        prompt_digest="digest",
    )
    _create_session(
        services._store,
        "ses_child",
        session_kind="subagent",
        root_session_id=root.session_id,
        parent_session_id=root.session_id,
        delegation_id=relation.delegation_id,
    )
    services._store.create_delegation(relation)
    archive = tmp_path / "tree.zip"
    assert (
        transfer(tmp_path / "source", "export", "sessions", archive, ids=("ses_child",)).count == 2
    )
    transfer(tmp_path / "target", "import", "sessions", archive, ids=("ses_child",))
    target = build_local_session_services(tmp_path / "target")
    assert target._store.list_delegations("ses_root") == [relation]


def test_busy_session_export_releases_acquired_locks(tmp_path):
    services = build_local_session_services(tmp_path)
    session, _run = _create_session(services._store)
    lock = services._store.acquire_session_lock(session.session_id, "running")
    assert lock is not None
    try:
        with pytest.raises(SessionBusyError):
            transfer(tmp_path, "export", "sessions", tmp_path / "busy.zip")
    finally:
        lock.release()
    transfer(tmp_path, "export", "sessions", tmp_path / "idle.zip")


def test_corrupt_resource_rejected_before_session_publication(tmp_path):
    source = tmp_path / "source"
    services = build_local_session_services(source)
    session, _run = _create_session(services._store)
    attachment = tmp_path / "input.txt"
    attachment.write_bytes(b"data")
    services.resources.import_resource(session.session_id, attachment)
    path = tmp_path / "archive.zip"
    transfer(source, "export", "sessions", path)
    archive = read_archive(path, "sessions")
    archive.files[next(iter(archive.files))] = b"corrupt"
    write_archive(path, archive, replace=True)
    with pytest.raises(DataTransferError, match="integrity"):
        transfer(tmp_path / "target", "import", "sessions", path)
    assert not build_local_session_services(tmp_path / "target")._store.list_sessions()


def test_skills_transfer_actual_files_without_discovery_configuration(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    skill = source / "skills" / "review"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: review\ndescription: Review changes\n---\nReview.", encoding="utf-8"
    )
    (skill / "scripts" / "check.py").write_bytes(b"print('review')\n")
    JsonConfigFile(source).write({"skills": {"enabled": False}})
    archive = tmp_path / "skills.zip"
    transfer(source, "export", "skills", archive, ids=("review",))
    transfer(target, "import", "skills", archive)
    assert (
        target / "skills" / "review" / "scripts" / "check.py"
    ).read_bytes() == b"print('review')\n"
    assert JsonConfigFile(target).read() == {}
    with pytest.raises(DataTransferError, match="skill exists"):
        transfer(target, "import", "skills", archive)
    transfer(target, "import", "skills", archive, replace=True)


@pytest.mark.parametrize(
    "name", ["../escape", "C:/escape", "skills\\escape", "skills/con", "/absolute"]
)
def test_unsafe_archive_paths_are_rejected(tmp_path, name):
    archive = tmp_path / "bad.zip"
    with ZipFile(archive, "w") as stream:
        stream.writestr(
            "manifest.json",
            json.dumps(
                {"format": "ouro-data", "version": 1, "kind": "skills", "data": {"names": []}}
            ),
        )
        stream.writestr(name, "unsafe")
    with pytest.raises(DataTransferError, match="unsafe path|undeclared files"):
        transfer(tmp_path / "target", "import", "skills", archive)


def test_archive_kind_version_and_destination_conflicts(tmp_path):
    archive = tmp_path / "data.zip"
    transfer(tmp_path / "source", "export", "providers", archive)
    original = archive.read_bytes()
    with pytest.raises(DataTransferError, match="destination exists"):
        transfer(tmp_path / "source", "export", "providers", archive)
    assert archive.read_bytes() == original
    with pytest.raises(DataTransferError, match="archive kind"):
        transfer(tmp_path / "target", "import", "mcp", archive)
    with ZipFile(archive, "w") as stream:
        stream.writestr(
            "manifest.json",
            json.dumps({"format": "ouro-data", "version": 900, "kind": "providers", "data": {}}),
        )
    with pytest.raises(DataTransferError, match="valid OURO"):
        transfer(tmp_path / "target", "import", "providers", archive)


@pytest.mark.parametrize("backend", ["files", "sqlite"])
def test_unknown_extension_tool_and_model_are_preserved_without_loading_them(tmp_path, backend):
    source = tmp_path / "source"
    services = build_local_session_services(source, session_backend=backend)
    session, run = _create_session(services._store, current_model="unknown-provider/model")
    state = SessionExtensionStateRecord(
        session_id=session.session_id,
        owner_id="dev.missing.plugin",
        state_schema_version=947,
        payload={"unknown": ["opaque", {"version": 100}]},
    )
    message = MessageRecord(
        session_id=session.session_id,
        run_id=run.run_id,
        role="assistant",
        tool_calls=[ToolCallRequest(name="missing__tool", arguments={"value": "retained"})],
    )
    services._store.commit_session(
        SessionCommit(
            before=session,
            steps=(SessionCommitStep(message=message, harness=HarnessState(states=(state,))),),
        )
    )
    archive = tmp_path / "external.zip"
    transfer(source, "export", "sessions", archive, backend=backend)
    result = transfer(tmp_path / "target", "import", "sessions", archive, backend=backend)
    target = build_local_session_services(tmp_path / "target", session_backend=backend)
    assert target.sessions.open(session.session_id).session.session_id == session.session_id
    assert target._store.load_session(session.session_id) == services._store.load_session(
        session.session_id
    )
    assert result.diagnostics[0].metadata == {
        "plugins": ["dev.missing.plugin"],
        "tools": ["missing__tool"],
        "model": "unknown-provider/model",
    }


@pytest.mark.parametrize("backend", ["files", "sqlite"])
def test_invalid_harness_checkpoint_rolls_back_entire_import(tmp_path, backend):
    source = tmp_path / "source"
    services = build_local_session_services(source)
    _create_session(services._store, "ses_one")
    _create_session(services._store, "ses_two")
    archive_path = tmp_path / "invalid.zip"
    transfer(source, "export", "sessions", archive_path)
    archive = read_archive(archive_path, "sessions")
    archive.manifest.data["bundle"]["sessions"][1]["snapshot"]["harness_versions"] = []
    write_archive(archive_path, archive, replace=True)
    with pytest.raises(PersistenceError, match="Harness checkpoint"):
        transfer(tmp_path / "target", "import", "sessions", archive_path, backend=backend)
    target = build_local_session_services(tmp_path / "target", session_backend=backend)
    assert target._store.list_sessions() == []


def test_unknown_selected_export_id_and_empty_archives(tmp_path):
    for kind in ("sessions", "skills"):
        archive = tmp_path / f"{kind}.zip"
        assert transfer(tmp_path / "source", "export", kind, archive).count == 0
        assert transfer(tmp_path / "target", "import", kind, archive).count == 0
        with pytest.raises(DataTransferError, match="not found"):
            transfer(tmp_path / "source", "export", kind, archive, ids=("missing",), replace=True)


def test_template_archive_preserves_custom_entry_without_optional_overlay_metadata(tmp_path):
    source = tmp_path / "source"
    catalog = ProviderTemplateStore(source).catalog()
    file = JsonConfigFile(source, name="provider-templates.json")
    raw = file.read()
    raw["templates"]["personal"] = {
        **catalog.entries[0].template.model_dump(mode="json"),
        "template_id": "personal",
    }
    file.write(raw)
    archive = tmp_path / "templates.zip"
    transfer(source, "export", "provider-templates", archive)
    transfer(tmp_path / "target", "import", "provider-templates", archive)
    assert ProviderTemplateStore(tmp_path / "target").get("personal").template_id == "personal"


def test_failed_dispatch_without_materialized_child_is_preserved(tmp_path):
    services = build_local_session_services(tmp_path / "source")
    root, run = _create_session(services._store)
    definition = AgentDefinition(
        agent_id="review", name="Review", description="Review", system_prompt="Review"
    )
    relation = DelegationRecord(
        root_session_id=root.session_id,
        parent_session_id=root.session_id,
        parent_run_id=run.run_id,
        child_session_id="ses_reserved",
        agent_id=definition.agent_id,
        definition=definition,
        idempotency_key="failed",
        task_description="Review",
        prompt_digest="digest",
        spawn_state="failed",
    )
    services._store.create_delegation(relation)
    archive = tmp_path / "failed.zip"
    transfer(tmp_path / "source", "export", "sessions", archive)
    transfer(tmp_path / "target", "import", "sessions", archive)
    imported = build_local_session_services(tmp_path / "target")
    assert imported._store.list_delegations(root.session_id) == [relation]


def test_file_session_publication_failure_restores_target(tmp_path, monkeypatch):
    source, target = tmp_path / "source", tmp_path / "target"
    services = build_local_session_services(source)
    for identity in ("ses_one", "ses_two"):
        _create_session(services._store, identity)
    existing = build_local_session_services(target)
    _create_session(existing._store, "ses_existing")
    before = existing._store.load_session("ses_existing")
    archive = tmp_path / "archive.zip"
    transfer(source, "export", "sessions", archive)
    original = Path.rename

    def fail_second_publication(path, destination):
        if Path(destination) == target / "sessions" / "ses_two":
            raise OSError("injected publication failure")
        return original(path, destination)

    monkeypatch.setattr(Path, "rename", fail_second_publication)
    with pytest.raises(DataTransferError, match="could not import"):
        transfer(target, "import", "sessions", archive)
    assert existing._store.load_session("ses_existing") == before
    assert [session.session_id for session in existing._store.list_sessions()] == ["ses_existing"]


def test_export_cannot_overwrite_managed_config_even_with_replace(tmp_path):
    _configure(tmp_path)
    path = tmp_path / "config" / "providers.json"
    before = path.read_bytes()
    with pytest.raises(DataTransferError, match="managed data paths"):
        transfer(tmp_path, "export", "providers", path, replace=True)
    assert path.read_bytes() == before


def test_sqlite_import_failure_restores_existing_data(tmp_path, monkeypatch):
    source, target = tmp_path / "source", tmp_path / "target"
    services = build_local_session_services(source)
    for identity in ("ses_one", "ses_two"):
        _create_session(services._store, identity)
    existing = build_local_session_services(target, session_backend="sqlite")
    _create_session(existing._store, "ses_existing")
    before = existing._store.load_session("ses_existing")
    archive = tmp_path / "archive.zip"
    transfer(source, "export", "sessions", archive)
    original = SQLiteSessionStore.save_run

    def fail_second_session(store, run):
        if run.session_id == "ses_two":
            raise PersistenceError("injected write failure")
        original(store, run)

    monkeypatch.setattr(SQLiteSessionStore, "save_run", fail_second_session)
    with pytest.raises(PersistenceError, match="injected"):
        transfer(target, "import", "sessions", archive, backend="sqlite")
    assert existing._store.load_session("ses_existing") == before
    assert [session.session_id for session in existing._store.list_sessions()] == ["ses_existing"]


def test_skill_replacement_rolls_back_files_and_preserves_policy_on_failure(tmp_path, monkeypatch):
    source, target = tmp_path / "source", tmp_path / "target"
    for home, body in ((source, "New skill"), (target, "Original skill")):
        for name in ("first", "second"):
            directory = home / "skills" / name
            directory.mkdir(parents=True)
            (directory / "SKILL.md").write_text(
                f"---\nname: {name}\ndescription: Test skill\n---\n{body}", encoding="utf-8"
            )
    policy = SkillConfigManager(target)
    policy.record_install(
        "first",
        ManagedSkillRecord(
            source="original", scope="user", target=str(target / "skills" / "first")
        ),
    )
    policy.set_skill_enabled("first", enabled=False)
    before = policy.load_raw()
    original_files = {path: path.read_bytes() for path in (target / "skills").rglob("SKILL.md")}
    archive = tmp_path / "skills.zip"
    transfer(source, "export", "skills", archive)
    rename = Path.rename

    def fail_second_publication(path, destination):
        if Path(destination) == target / "skills" / "second" and "backups" not in path.parts:
            raise OSError("injected skill publication failure")
        return rename(path, destination)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "rename", fail_second_publication)
        with pytest.raises(DataTransferError, match="could not import"):
            transfer(target, "import", "skills", archive, replace=True)
    assert policy.load_raw() == before
    assert all(path.read_bytes() == content for path, content in original_files.items())
    transfer(target, "import", "skills", archive, replace=True)
    assert policy.snapshot().config.installed == {}
    assert policy.snapshot().config.disabled == {"first"}
    assert all("New skill" in path.read_text(encoding="utf-8") for path in original_files)


@pytest.mark.parametrize("backend", ["files", "sqlite"])
def test_missing_attachment_item_resource_is_rejected_before_import(tmp_path, backend):
    services = build_local_session_services(tmp_path / "source")
    _create_session(services._store)
    archive_path = tmp_path / "invalid.zip"
    transfer(tmp_path / "source", "export", "sessions", archive_path)
    archive = read_archive(archive_path, "sessions")
    message = archive.manifest.data["bundle"]["sessions"][0]["snapshot"]["messages"][0]
    message["input_attachments"] = [
        {"kind": "upload", "resource_id": "res_" + "a" * 32, "name": "lost.txt", "text_offset": 0}
    ]
    write_archive(archive_path, archive, replace=True)
    with pytest.raises(DataTransferError, match="resource references are incomplete"):
        transfer(tmp_path / "target", "import", "sessions", archive_path, backend=backend)
    assert not build_local_session_services(
        tmp_path / "target", session_backend=backend
    )._store.list_sessions()


@pytest.mark.parametrize("backend", ["files", "sqlite"])
def test_foreign_event_run_reference_rejected_before_import(tmp_path, backend):
    services = build_local_session_services(tmp_path / "source")
    _create_session(services._store)
    archive_path = tmp_path / "invalid.zip"
    transfer(tmp_path / "source", "export", "sessions", archive_path)
    archive = read_archive(archive_path, "sessions")
    archive.manifest.data["bundle"]["sessions"][0]["snapshot"]["events"] = [
        {"session_id": "ses_test", "run_id": "../../foreign", "event_type": "run_completed"}
    ]
    write_archive(archive_path, archive, replace=True)
    with pytest.raises(DataTransferError, match="invalid or unsupported Session"):
        transfer(tmp_path / "target", "import", "sessions", archive_path, backend=backend)
    assert not build_local_session_services(
        tmp_path / "target", session_backend=backend
    )._store.list_sessions()


def test_skill_rollback_failure_keeps_original_backup(tmp_path, monkeypatch):
    source, target = tmp_path / "source", tmp_path / "target"
    for home, body in ((source, "New"), (target, "Original")):
        directory = home / "skills" / "review"
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: review\ndescription: Review\n---\n{body}", encoding="utf-8"
        )
    before = (target / "skills" / "review" / "SKILL.md").read_bytes()
    archive = tmp_path / "skills.zip"
    transfer(source, "export", "skills", archive)
    rename = Path.rename

    def fail_restore(path, destination):
        if path.parent.name == "backups":
            raise OSError("injected restore failure")
        return rename(path, destination)

    def fail_policy_write(*_args):
        raise OSError("injected policy write failure")

    monkeypatch.setattr(Path, "rename", fail_restore)
    monkeypatch.setattr(SkillConfigManager, "forget_installs", fail_policy_write)
    with pytest.raises(DataTransferError, match="original backups retained") as error:
        transfer(target, "import", "skills", archive, replace=True)
    backups = list(target.glob(".skill-import-*/backups/review/SKILL.md"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == before
    assert str(backups[0].parents[2]) in str(error.value)


@pytest.mark.parametrize("version", [None, True, "1"])
def test_archive_requires_explicit_integer_version(tmp_path, version):
    path = tmp_path / "invalid.zip"
    manifest = {"format": "ouro-data", "kind": "sessions", "data": {}}
    if version is not None:
        manifest["version"] = version
    with ZipFile(path, "w") as stream:
        stream.writestr("manifest.json", json.dumps(manifest))
    with pytest.raises(DataTransferError):
        transfer(tmp_path / "target", "import", "sessions", path)


def test_skill_export_rejects_shadowed_native_names(tmp_path):
    for folder in ("first", "second"):
        path = tmp_path / "skills" / folder
        path.mkdir(parents=True)
        (path / "SKILL.md").write_text(
            "---\nname: review\ndescription: Review\n---\nReview", encoding="utf-8"
        )
    archive = tmp_path / "skills.zip"
    with pytest.raises(DataTransferError, match="incomplete native skill discovery"):
        transfer(tmp_path, "export", "skills", archive)
    assert not archive.exists()


@pytest.mark.parametrize("backend", ["files", "sqlite"])
def test_transfer_selection_lists_sources_without_importing(tmp_path, backend):
    source = tmp_path / "source"
    store = build_local_session_services(source, session_backend=backend)._store
    _create_session(store, "ses_one", title="First conversation")
    _create_session(store, "ses_two", title="Second conversation", archived=True)
    store.refresh_catalog("ses_one")
    store.refresh_catalog("ses_two")
    archive = tmp_path / "sessions.zip"
    request = DataTransferRequest(action="export", kind="sessions", path=archive)
    service = build_local_data_transfer_service(source, session_backend=backend)
    expected = {("ses_one", "First conversation"), ("ses_two", "Second conversation")}
    assert {(item.identity, item.label) for item in service.selection_items(request)} == expected
    service.transfer(request)
    target = tmp_path / "target"
    imported_request = request.model_copy(update={"action": "import"})
    preview = build_local_data_transfer_service(target, session_backend=backend)
    assert {
        (item.identity, item.label) for item in preview.selection_items(imported_request)
    } == expected
    assert not target.exists()


@pytest.mark.parametrize(
    "kind",
    ["providers", "provider-templates", "mcp", "agents", "prompts", "sessions", "skills"],
)
def test_archive_detection_uses_manifest_without_creating_target(tmp_path, kind):
    archive = tmp_path / "unrelated-name.zip"
    write_archive(archive, DataArchive(ArchiveManifest(kind=kind, data={})), replace=False)
    target = tmp_path / "target"
    service = build_local_data_transfer_service(target)
    assert service.archive_kind(archive) == kind
    assert not target.exists()


@pytest.mark.parametrize(
    "manifest",
    [
        {"format": "ouro-data", "version": 1, "kind": "unknown", "data": {}},
        {"format": "ouro-data", "version": 900, "kind": "sessions", "data": {}},
        {"kind": "providers", "data": {}},
    ],
)
def test_archive_detection_rejects_unsupported_manifest(tmp_path, manifest):
    archive = tmp_path / "invalid.zip"
    with ZipFile(archive, "w") as stream:
        stream.writestr("manifest.json", json.dumps(manifest))
    target = tmp_path / "target"
    with pytest.raises(DataTransferError):
        build_local_data_transfer_service(target).archive_kind(archive)
    assert not target.exists()

from __future__ import annotations

import json
from zipfile import ZipFile

import pytest

from ouro.application.data_transfer import DataTransferRequest
from ouro.core.messages import MessageRecord
from ouro.local_application import build_local_data_transfer_service, build_local_session_services
from ouro.transfer.archive import DataTransferError


@pytest.mark.parametrize("backend", ["files", "sqlite"])
def test_export_selection_reads_catalog_without_opening_sessions(tmp_path, backend, monkeypatch):
    services = build_local_session_services(tmp_path, session_backend=backend)
    services.sessions.create(session_id="ses_one", title="One")
    service = build_local_data_transfer_service(tmp_path, session_store=services._store)

    def forbidden(*args, **kwargs):
        pytest.fail("selection must not read canonical Session state")

    for name in ("list_sessions", "load_session_record", "load_session", "load_harness"):
        monkeypatch.setattr(services._store, name, forbidden)
    items = service.selection_items(
        DataTransferRequest(action="export", kind="sessions", path=tmp_path / "sessions.zip")
    )
    assert [(item.identity, item.label) for item in items] == [("ses_one", "One")]


@pytest.mark.parametrize("indexed", [True, False])
def test_import_selection_reads_only_selection_member(tmp_path, monkeypatch, indexed):
    source = tmp_path / "source"
    sessions = build_local_session_services(source)
    sessions.sessions.create(session_id="ses_one", title="One")
    sessions._store.append_message(
        MessageRecord.text(session_id="ses_one", role="user", text="large history " * 10000)
    )
    attachment = tmp_path / "attachment.txt"
    attachment.write_bytes(b"attachment " * 10000)
    sessions.resources.import_resource("ses_one", attachment)
    archive = tmp_path / "sessions.zip"
    build_local_data_transfer_service(source).transfer(
        DataTransferRequest(action="export", kind="sessions", path=archive)
    )
    if not indexed:
        with ZipFile(archive) as reader:
            members = {
                entry.filename: reader.read(entry)
                for entry in reader.infolist()
                if entry.filename != "index.json"
            }
        with ZipFile(archive, "w") as writer:
            for name, content in members.items():
                writer.writestr(name, content)
    read = ZipFile.read
    reads = []

    def checked_read(reader, name, *args, **kwargs):
        assert name == ("index.json" if indexed else "manifest.json")
        reads.append(name)
        return read(reader, name, *args, **kwargs)

    monkeypatch.setattr(ZipFile, "read", checked_read)
    target = tmp_path / "target"
    service = build_local_data_transfer_service(target)
    assert service.archive_kind(archive) == "sessions"
    items = service.selection_items(
        DataTransferRequest(action="import", kind="sessions", path=archive)
    )
    assert [(item.identity, item.label) for item in items] == [("ses_one", "One")]
    assert len(reads) == 2
    assert not target.exists()


def test_import_rejects_mismatched_selection_index_before_writes(tmp_path):
    services = build_local_session_services(tmp_path / "source")
    services.sessions.create(session_id="ses_one", title="One")
    archive = tmp_path / "sessions.zip"
    build_local_data_transfer_service(tmp_path / "source").transfer(
        DataTransferRequest(action="export", kind="sessions", path=archive)
    )
    with ZipFile(archive) as reader:
        members = {entry.filename: reader.read(entry) for entry in reader.infolist()}
    index = json.loads(members["index.json"])
    index["items"][0]["identity"] = "ses_wrong"
    members["index.json"] = json.dumps(index).encode()
    with ZipFile(archive, "w") as writer:
        for name, content in members.items():
            writer.writestr(name, content)
    with pytest.raises(DataTransferError, match="index does not match"):
        build_local_data_transfer_service(tmp_path / "target").transfer(
            DataTransferRequest(action="import", kind="sessions", path=archive)
        )
    assert build_local_session_services(tmp_path / "target")._store.list_sessions() == []

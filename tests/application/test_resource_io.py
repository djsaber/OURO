from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ouro.core.errors import ResourceIntegrityError
from ouro.core.resources import ResourceRecord, ResourceSource
from ouro.resources import LocalResourceStore


def _store(tmp_path, *contents):
    store = LocalResourceStore(tmp_path / "home")
    store.initialize()
    records = [
        ResourceRecord(
            resource_id=f"res_{hashlib.sha256(content).hexdigest()}",
            digest=f"sha256:{hashlib.sha256(content).hexdigest()}",
            size=len(content),
            media_type="text/plain",
            source=ResourceSource(name="fixture.txt"),
        )
        for content in contents
    ]
    store.import_contents(zip(records, contents, strict=True))
    return store, records


def test_export_reads_blob_once_and_publishes_verified_content(tmp_path, monkeypatch):
    content = b"verified content\n" * 100_000
    store, (record,) = _store(tmp_path, content)
    blob = store._blob_path(record)
    original_open = Path.open
    reads = []

    def tracked_open(path, *args, **kwargs):
        if path == blob:
            reads.append(path)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", tracked_open)
    destination = tmp_path / "export" / "copy.txt"
    assert store.export_file(record.resource_id, destination, overwrite=False) == destination
    assert destination.read_bytes() == content
    assert len(reads) == 1
    assert list(destination.parent.iterdir()) == [destination]


@pytest.mark.parametrize("damage", [b"bad", b"same size", None])
def test_export_failure_preserves_destination_and_cleans_temp(tmp_path, damage):
    store, (record,) = _store(tmp_path, b"canonical")
    blob = store._blob_path(record)
    if damage is None:
        blob.unlink()
    else:
        blob.write_bytes(damage)
    destination = tmp_path / "export" / "copy.txt"
    destination.parent.mkdir()
    destination.write_bytes(b"original")

    with pytest.raises(ResourceIntegrityError):
        store.export_file(record.resource_id, destination, overwrite=True)
    assert destination.read_bytes() == b"original"
    assert list(destination.parent.iterdir()) == [destination]


def test_delete_batch_scans_catalog_once_and_preserves_other_blobs(tmp_path, monkeypatch):
    store, records = _store(tmp_path, b"first", b"second", b"retained")
    original_scan = store._scan_records
    scans = []

    def scan():
        scans.append(True)
        return original_scan()

    monkeypatch.setattr(store, "_scan_records", scan)
    selected = [records[0].resource_id, records[1].resource_id, records[0].resource_id]
    assert store.delete_resources(selected) == selected[:2]
    assert store.load_content(records[2].resource_id) == (records[2], b"retained")
    assert list(store.blobs_dir.iterdir()) == [store._blob_path(records[2])]
    catalog = json.loads(store.index_path.read_text("utf-8"))
    assert [entry["resource_id"] for entry in catalog["resources"]] == [records[2].resource_id]
    assert len(scans) == 1


def test_resource_record_identity_must_match_requested_file(tmp_path):
    store, records = _store(tmp_path, b"first", b"second")
    first_path = store.records_dir / f"{records[0].resource_id}.json"
    first_path.write_text(records[1].model_dump_json(), encoding="utf-8")

    with pytest.raises(ResourceIntegrityError):
        store.delete_resources([records[0].resource_id])
    assert first_path.exists()
    assert store.load_content(records[1].resource_id) == (records[1], b"second")


def test_partial_delete_failure_refreshes_catalog(tmp_path, monkeypatch):
    store, records = _store(tmp_path, b"first", b"second")
    original_unlink = Path.unlink
    blocked = store.records_dir / f"{records[1].resource_id}.json"

    def unlink(path, *args, **kwargs):
        if path == blocked:
            raise OSError("injected deletion failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    with pytest.raises(OSError, match="injected deletion failure"):
        store.delete_resources(record.resource_id for record in records)
    catalog = json.loads(store.index_path.read_text("utf-8"))
    assert [entry["resource_id"] for entry in catalog["resources"]] == [records[1].resource_id]

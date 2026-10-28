from __future__ import annotations

import hashlib
from contextlib import contextmanager
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
            filename=f"fixture-{index}.txt",
            digest=f"sha256:{hashlib.sha256(content).hexdigest()}",
            size=len(content),
            media_type="text/plain",
            source=ResourceSource(name="fixture.txt"),
        )
        for index, content in enumerate(contents)
    ]
    store.import_contents("ses_test", zip(records, contents, strict=True))
    return store, records


def test_export_reads_blob_once_and_publishes_verified_content(tmp_path, monkeypatch):
    content = b"verified content\n" * 100_000
    store, (record,) = _store(tmp_path, content)
    blob = store.path_for("ses_test", record)
    original_open = Path.open
    reads = []

    def tracked_open(path, *args, **kwargs):
        if path == blob:
            reads.append(path)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", tracked_open)
    destination = tmp_path / "export" / "copy.txt"
    assert store.export_file("ses_test", record, destination, overwrite=False) == destination
    assert destination.read_bytes() == content
    assert len(reads) == 1
    assert list(destination.parent.iterdir()) == [destination]


@pytest.mark.parametrize("damage", [b"bad", b"same size", None])
def test_export_failure_preserves_destination_and_cleans_temp(tmp_path, damage):
    store, (record,) = _store(tmp_path, b"canonical")
    blob = store.path_for("ses_test", record)
    if damage is None:
        blob.unlink()
    else:
        blob.write_bytes(damage)
    destination = tmp_path / "export" / "copy.txt"
    destination.parent.mkdir()
    destination.write_bytes(b"original")

    with pytest.raises(ResourceIntegrityError):
        store.export_file("ses_test", record, destination, overwrite=True)
    assert destination.read_bytes() == b"original"
    assert list(destination.parent.iterdir()) == [destination]


def test_delete_batch_preserves_unselected_files_and_other_sessions(tmp_path):
    store, records = _store(tmp_path, b"first", b"second", b"retained")
    store.import_contents("ses_other", ((records[0], b"first"),))
    assert store.delete_resources("ses_test", (records[0], records[1], records[0])) == [
        r.resource_id for r in records[:2]
    ]
    assert store.load_content("ses_test", records[2]) == b"retained"
    assert store.load_content("ses_other", records[0]) == b"first"


def test_resource_filename_cannot_escape_session_directory(tmp_path):
    store, (record,) = _store(tmp_path, b"first")
    for name in ("../escape.txt", "C:/escape.txt", "CON", "evil."):
        invalid = record.model_copy(update={"filename": name})
        with pytest.raises(ValueError):
            store.delete_resources("ses_test", (invalid,))
    assert store.load_content("ses_test", record) == b"first"


def test_partial_delete_failure_preserves_unprocessed_files(tmp_path, monkeypatch):
    store, records = _store(tmp_path, b"first", b"second")
    original_unlink = Path.unlink
    blocked = store.path_for("ses_test", records[1])

    def unlink(path, *args, **kwargs):
        if path == blocked:
            raise OSError("injected deletion failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    with pytest.raises(OSError, match="injected deletion failure"):
        store.delete_resources("ses_test", records)
    assert store.load_content("ses_test", records[1]) == b"second"


@pytest.mark.parametrize("size", [4096, 4 * 1024 * 1024])
def test_small_resource_windows_read_only_selected_bytes(tmp_path, monkeypatch, size):
    content = b"x" * size
    store, (record,) = _store(tmp_path, content)
    blob = store.path_for("ses_test", record)
    original_open = Path.open
    reads = []

    class TrackedReader:
        def __init__(self, stream):
            self.stream = stream

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def read(self, length=-1):
            data = self.stream.read(length)
            reads.append(len(data))
            return data

    @contextmanager
    def tracked_open(path, *args, **kwargs):
        with original_open(path, *args, **kwargs) as stream:
            yield TrackedReader(stream) if path == blob else stream

    monkeypatch.setattr(Path, "open", tracked_open)
    assert store.read_ranges("ses_test", record, ((0, 16), (size - 32, 32))) == (
        b"x" * 16,
        b"x" * 32,
    )
    assert sum(reads) == 48


def test_window_read_and_explicit_integrity_verification_have_distinct_boundaries(tmp_path):
    store, (record,) = _store(tmp_path, b"prefix-original")
    store.path_for("ses_test", record).write_bytes(b"prefix-modified")
    assert store.read_ranges("ses_test", record, ((0, 6),)) == (b"prefix",)
    with pytest.raises(ResourceIntegrityError):
        store.verify("ses_test", record)


@pytest.mark.parametrize("ranges", [((-1, 1),), ((0, -1),), ((8, 1),), ((0, 9),)])
def test_resource_ranges_reject_invalid_boundaries(tmp_path, ranges):
    store, (record,) = _store(tmp_path, b"12345678")
    with pytest.raises(ValueError, match="range"):
        store.read_ranges("ses_test", record, ranges)


@pytest.mark.parametrize("damage", [b"short", None])
def test_window_read_rejects_missing_or_resized_resource(tmp_path, damage):
    store, (record,) = _store(tmp_path, b"12345678")
    blob = store.path_for("ses_test", record)
    if damage is None:
        blob.unlink()
    else:
        blob.write_bytes(damage)
    with pytest.raises(ResourceIntegrityError):
        store.read_ranges("ses_test", record, ((0, 1),))

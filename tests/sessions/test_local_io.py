from pathlib import Path

import pytest

import ouro.local_io as local_io
from ouro.core.errors import PersistenceError


def _windows_replace_error(winerror: int) -> OSError:
    error = PermissionError("injected Windows replace conflict")
    error.winerror = winerror
    return error


def test_atomic_write_retries_transient_windows_replace_conflicts(
    monkeypatch,
    tmp_path,
) -> None:
    target = tmp_path / "session.json"
    target.write_text("before\n", encoding="utf-8")
    original_replace = Path.replace
    attempts = 0
    delays: list[float] = []

    def flaky_replace(source: Path, destination: Path):
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise _windows_replace_error(5)
        return original_replace(source, destination)

    monkeypatch.setattr(Path, "replace", flaky_replace)
    monkeypatch.setattr(local_io, "sleep", delays.append)

    local_io.write_text_atomic(target, "after")

    assert target.read_text("utf-8") == "after"
    assert attempts == 3
    assert delays == [0.01, 0.02]


def test_atomic_write_does_not_retry_non_transient_replace_errors(
    monkeypatch,
    tmp_path,
) -> None:
    target = tmp_path / "session.json"
    delays: list[float] = []

    def denied_replace(_source: Path, _destination: Path):
        raise _windows_replace_error(3)

    monkeypatch.setattr(Path, "replace", denied_replace)
    monkeypatch.setattr(local_io, "sleep", delays.append)

    with pytest.raises(PersistenceError, match="could not write"):
        local_io.write_text_atomic(target, "after")

    assert delays == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("payload", ["", '{"message": "你好"}\n', "first\nsecond\n"])
def test_atomic_text_write_preserves_exact_utf8_bytes(tmp_path, payload):
    target = tmp_path / "messages.jsonl"
    local_io.write_text_atomic(target, payload)
    assert target.read_bytes() == payload.encode("utf-8")


def test_atomic_write_cleans_temporary_file_when_sync_fails(tmp_path, monkeypatch):
    target = tmp_path / "session.json"
    target.write_text("original", encoding="utf-8")

    def fail_sync(_fd):
        raise OSError("injected sync failure")

    monkeypatch.setattr(local_io.os, "fsync", fail_sync)
    with pytest.raises(PersistenceError, match="injected sync failure"):
        local_io.write_text_atomic(target, "replacement")

    assert target.read_text("utf-8") == "original"
    assert list(tmp_path.iterdir()) == [target]

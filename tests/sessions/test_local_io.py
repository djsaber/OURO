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

    assert target.read_text("utf-8") == "after\n"
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

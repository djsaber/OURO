from __future__ import annotations

import asyncio
import csv
import ctypes
import json
import os
import re
import stat
import subprocess
from ctypes import wintypes
from pathlib import Path

import pytest

import ouro.auth.store as store_module
from ouro.auth.contracts import AuthError, CredentialRecord, CredentialSummary
from ouro.auth.store import FileCredentialStore
from ouro.core.base import new_id


def record() -> CredentialRecord:
    return CredentialRecord(
        credential_id=new_id("cred"),
        driver_id="fixture",
        revision=1,
        state="active",
        secret_payload={"token": "fixture-secret-not-a-real-token"},
    )


def test_atomic_round_trip_and_safe_projection(tmp_path: Path) -> None:
    store = FileCredentialStore(tmp_path)
    original = record()
    assert store.read(original.credential_id) is None
    store.write(original)
    assert store.read(original.credential_id) == original
    assert store.list_ids() == (original.credential_id,)
    assert "fixture-secret" not in repr(original)
    summary = CredentialSummary.from_record(original)
    assert "secret" not in summary.model_dump_json()
    assert summary.revision == 1


@pytest.mark.parametrize("credential_id", ["../outside", "cred_../token", "cred_X", "", "c:/token"])
def test_credential_id_is_not_a_path(tmp_path: Path, credential_id: str) -> None:
    store = FileCredentialStore(tmp_path)
    with pytest.raises(AuthError) as failure:
        store.read(credential_id)
    assert failure.value.code == "credential_store_error"
    assert not store.directory.exists()


@pytest.mark.parametrize(
    "update",
    [
        {"format_version": 2},
        {"format_version": True},
        {"revision": 0},
        {"revision": "1"},
        {"unexpected": "fixture-secret"},
        {"state": "ready"},
        {"state": "logged_out"},
        {"secret_payload": {}},
        {"expires_at": "2026-09-16T00:00:00"},
        {"credential_id": "cred_" + "f" * 32},
    ],
)
def test_invalid_records_fail_without_exposing_or_rewriting_json(tmp_path: Path, update) -> None:
    store = FileCredentialStore(tmp_path)
    original = record()
    store.write(original)
    path = store.directory / f"{original.credential_id}.json"
    encoded = json.dumps({**original.model_dump(), **update})
    path.write_text(encoded, encoding="utf-8")
    with pytest.raises(AuthError) as failure:
        store.read(original.credential_id)
    assert "fixture-secret" not in str(failure.value)
    assert path.read_text("utf-8") == encoded


def test_corrupt_json_does_not_leak_partial_secret(tmp_path: Path) -> None:
    store = FileCredentialStore(tmp_path)
    original = record()
    store.write(original)
    path = store.directory / f"{original.credential_id}.json"
    path.write_text('{"secret_payload": "fixture-secret', encoding="utf-8")
    with pytest.raises(AuthError) as failure:
        store.read(original.credential_id)
    assert "fixture-secret" not in str(failure.value)


def test_missing_format_is_not_inferred_and_queries_do_not_create_storage(tmp_path: Path) -> None:
    store = FileCredentialStore(tmp_path)
    original = record()
    assert store.list_ids() == ()
    assert store.read(original.credential_id) is None
    assert not store.directory.exists()
    store.write(original)
    path = store.directory / f"{original.credential_id}.json"
    raw = original.model_dump()
    raw.pop("format_version")
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(AuthError):
        store.read(original.credential_id)


def test_unreadable_directory_is_not_reported_as_an_empty_credential_list(
    tmp_path: Path, monkeypatch
) -> None:
    store = FileCredentialStore(tmp_path)
    store.write(record())
    scandir = os.scandir

    def inaccessible(path):
        if Path(path) == store.directory:
            raise PermissionError("fixture-secret")
        return scandir(path)

    monkeypatch.setattr(os, "scandir", inaccessible)
    with pytest.raises(AuthError) as failure:
        store.list_ids()
    assert failure.value.code == "credential_store_error"
    assert "fixture-secret" not in str(failure.value)


def test_permissions_are_applied_before_writing_secret(tmp_path: Path, monkeypatch) -> None:
    store = FileCredentialStore(tmp_path)
    original = record()
    store.write(original)
    protect = store_module.restrict_to_user
    observed_empty_file = False

    def reject_temporary(path: Path, *, directory: bool = False) -> None:
        nonlocal observed_empty_file
        if not directory:
            assert path.read_bytes() == b""
            observed_empty_file = True
            raise PermissionError("fixture-secret")
        protect(path, directory=True)

    monkeypatch.setattr(store_module, "restrict_to_user", reject_temporary)
    with pytest.raises(AuthError) as failure:
        store.write(original.model_copy(update={"revision": 2}))
    assert observed_empty_file
    assert "fixture-secret" not in str(failure.value)
    assert store.read(original.credential_id) == original
    assert {path.name for path in store.directory.iterdir()} == {f"{original.credential_id}.json"}


def test_failed_replace_preserves_previous_record_and_cleans_temporary(
    tmp_path, monkeypatch
) -> None:
    store = FileCredentialStore(tmp_path)
    original = record()
    store.write(original)

    def fail_replace(source, target):
        assert source.read_bytes()
        assert target.read_bytes()
        raise OSError("fixture-secret")

    monkeypatch.setattr(store_module, "replace_path_with_retry", fail_replace)
    with pytest.raises(AuthError):
        store.write(original.model_copy(update={"revision": 2}))
    assert store.read(original.credential_id) == original
    assert len(list(store.directory.iterdir())) == 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_posix_modes_apply_to_directory_temporary_and_replaced_file(tmp_path, monkeypatch) -> None:
    store = FileCredentialStore(tmp_path)
    original = record()
    replace = store_module.replace_path_with_retry

    def checked_replace(source: Path, target: Path) -> None:
        assert stat.S_IMODE(source.stat().st_mode) == 0o600
        replace(source, target)

    monkeypatch.setattr(store_module, "replace_path_with_retry", checked_replace)
    store.write(original)
    assert stat.S_IMODE(store.directory.stat().st_mode) == 0o700
    assert (
        stat.S_IMODE((store.directory / f"{original.credential_id}.json").stat().st_mode) == 0o600
    )


def _windows_acl(path: Path) -> str:
    # Inspect the actual DACL without starting PowerShell for every file. These
    # read APIs are independent of the production permission-writing helper.
    api = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    api.GetFileSecurityW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    api.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel.LocalFree.restype = wintypes.HLOCAL
    size = wintypes.DWORD()
    api.GetFileSecurityW(str(path), 4, None, 0, ctypes.byref(size))
    if ctypes.get_last_error() != 122:  # ERROR_INSUFFICIENT_BUFFER
        raise ctypes.WinError(ctypes.get_last_error())
    descriptor = ctypes.create_string_buffer(size.value)
    if not api.GetFileSecurityW(str(path), 4, descriptor, size, ctypes.byref(size)):
        raise ctypes.WinError(ctypes.get_last_error())
    sddl = wintypes.LPWSTR()
    if not api.ConvertSecurityDescriptorToStringSecurityDescriptorW(
        descriptor, 1, 4, ctypes.byref(sddl), None
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return sddl.value
    finally:
        kernel.LocalFree(ctypes.cast(sddl, wintypes.HLOCAL))


def _windows_sid(trustee: str) -> str:
    # SDDL may abbreviate a SID (for example, the local administrator as LA).
    # Resolve identity through Windows instead of depending on its text spelling.
    api = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    api.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.LPVOID)]
    api.ConvertSidToStringSidW.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel.LocalFree.restype = wintypes.HLOCAL
    sid = wintypes.LPVOID()
    text = wintypes.LPWSTR()
    try:
        if not api.ConvertStringSidToSidW(trustee, ctypes.byref(sid)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not api.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise ctypes.WinError(ctypes.get_last_error())
        return text.value
    finally:
        if text:
            kernel.LocalFree(ctypes.cast(text, wintypes.HLOCAL))
        if sid:
            kernel.LocalFree(sid)


@pytest.mark.skipif(os.name != "nt", reason="Windows SIDs")
@pytest.mark.parametrize(
    ("trustee", "sid"),
    [("BA", "S-1-5-32-544"), ("SY", "S-1-5-18"), ("WD", "S-1-1-0"), ("S-1-5-18", "S-1-5-18")],
)
def test_windows_sid_resolves_identity_independently_of_sddl_spelling(trustee, sid) -> None:
    assert _windows_sid(trustee) == sid


@pytest.mark.skipif(os.name != "nt", reason="Windows ACLs")
def test_windows_acl_on_directory_temporary_and_replaced_file(tmp_path, monkeypatch) -> None:
    store = FileCredentialStore(tmp_path)
    original = record()
    replace = store_module.replace_path_with_retry
    identity = subprocess.run(
        ["whoami.exe", "/user", "/fo", "csv", "/nh"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",  # Only the ASCII SID is used; account names may use an OEM code page.
        check=True,
        timeout=60,
    )
    sid = next(csv.reader(identity.stdout.splitlines()))[1]
    assert sid.startswith("S-1-")
    paths: list[str] = []

    def checked_replace(source: Path, target: Path) -> None:
        paths.append(_windows_acl(source))
        replace(source, target)

    monkeypatch.setattr(store_module, "replace_path_with_retry", checked_replace)
    store.write(original)
    store.write(original.model_copy(update={"revision": 2}))
    paths.extend(
        [
            _windows_acl(store.directory),
            _windows_acl(store.directory / f"{original.credential_id}.json"),
        ]
    )
    for acl in paths:
        assert acl.startswith("D:") and "P" in acl.split("(", 1)[0]
        rules = re.findall(r"\(([^()]*)\)", acl)
        assert len(rules) == 1
        kind, flags, rights, object_type, inherited_type, trustee = rules[0].split(";")
        assert _windows_sid(trustee) == sid
        assert "ID" not in flags  # Not inherited.
        assert kind == "A"  # Allow.
        assert rights in {"FA", "0x1f01ff"}  # FullControl.
        assert object_type == inherited_type == ""


def test_linked_credential_file_is_rejected(tmp_path: Path) -> None:
    store = FileCredentialStore(tmp_path)
    original = record()
    store.write(original)
    path = store.directory / f"{original.credential_id}.json"
    alias = tmp_path / "alias.json"
    os.link(path, alias)
    with pytest.raises(AuthError):
        store.read(original.credential_id)
    with pytest.raises(AuthError):
        store.write(original)


async def test_lock_timeout_cancel_and_release(tmp_path: Path) -> None:
    store = FileCredentialStore(tmp_path)
    credential_id = new_id("cred")
    entered = asyncio.Event()

    async def waiter() -> None:
        entered.set()
        async with store.lock(credential_id, timeout=1):
            pytest.fail("cancelled waiter acquired a lock")

    async with store.lock(credential_id, timeout=1):
        with pytest.raises(AuthError) as failure:
            async with store.lock(credential_id, timeout=0.03):
                pytest.fail("second lock must be excluded")
        assert failure.value.code == "credential_lock_timeout"
        task = asyncio.create_task(waiter())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    async with store.lock(credential_id, timeout=1):
        pass


async def test_lock_does_not_reclassify_consumer_exceptions(tmp_path: Path) -> None:
    store = FileCredentialStore(tmp_path)
    with pytest.raises(OSError, match="consumer failure"):
        async with store.lock(new_id("cred"), timeout=1):
            raise OSError("consumer failure")

from __future__ import annotations

import ctypes
import struct
import sys

import pyperclip
import pytest

from ouro.tui.app import OuroTuiApp
from ouro.tui.clipboard import ClipboardReadError, read_system_clipboard
from ouro.tui.composer.input import Composer
from tests.tui.helpers import run_ready_app
from tests.tui.test_history_commands import application_at

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Native Windows file clipboard")


@pytest.fixture
def file_clipboard(tmp_path, monkeypatch):
    """Use real CF_HDROP memory and Shell32 decoding without changing the user's clipboard."""
    paths = (tmp_path / "中文 文件.txt", tmp_path / "second.txt")
    for path in paths:
        path.write_text(path.name, encoding="utf-8")
    payload = struct.pack("<IiiII", 20, 0, 0, 0, 1) + ("\0".join(map(str, paths)) + "\0\0").encode(
        "utf-16-le"
    )
    load_library = ctypes.WinDLL
    kernel32 = load_library("kernel32", use_last_error=True)
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    handle = kernel32.GlobalAlloc(2, len(payload))
    assert handle
    pointer = kernel32.GlobalLock(handle)
    assert pointer
    ctypes.memmove(pointer, payload, len(payload))
    kernel32.GlobalUnlock(handle)
    closed = []

    def close():
        closed.append(True)
        return 1

    user32 = load_library("user32", use_last_error=True)
    user32.IsClipboardFormatAvailable = lambda format: int(format == 15)
    user32.OpenClipboard = lambda _window: 1
    user32.GetClipboardData = lambda format: handle if format == 15 else None
    user32.CloseClipboard = close
    monkeypatch.setattr(
        ctypes,
        "WinDLL",
        lambda name, **kwargs: user32 if name == "user32" else load_library(name, **kwargs),
    )
    monkeypatch.setattr(pyperclip, "paste", lambda: "")
    try:
        yield paths, user32, closed
    finally:
        kernel32.GlobalFree(handle)


def test_native_files_become_a_complete_quoted_path_paste(file_clipboard):
    paths, _api, closed = file_clipboard
    assert read_system_clipboard() == "\n".join(f'"{path}"' for path in paths)
    assert closed == [True]


def test_clipboard_text_takes_precedence_over_native_files(file_clipboard, monkeypatch):
    _paths, _api, closed = file_clipboard
    monkeypatch.setattr(pyperclip, "paste", lambda: "clipboard text")
    assert read_system_clipboard() == "clipboard text"
    assert closed == []


def test_unreadable_native_file_data_closes_clipboard_and_reports_failure(file_clipboard):
    _paths, api, closed = file_clipboard
    api.GetClipboardData = lambda _format: None
    with pytest.raises(ClipboardReadError, match="System clipboard is unavailable"):
        read_system_clipboard()
    assert closed == [True]


@pytest.mark.parametrize("existing", (False, True))
async def test_right_click_files_use_normal_upload_on_both_pages(
    tmp_path, file_clipboard, existing
):
    paths, _api, _closed = file_clipboard
    application = application_at(tmp_path / "home")
    origin = application.runtime.create_session().session_id if existing else None
    app = OuroTuiApp(application=application, resume_session_id=origin)
    async with run_ready_app(app) as pilot:
        composer = app.query_one(Composer)
        composer.load_text("draft")
        await pilot.click(composer, offset=(2, 0), button=3)
        await app.workers.wait_for_complete(
            [w for w in app.workers if w.group in {"composer-clipboard", "attachment-import"}]
        )
        await pilot.pause()
        if workers := [w for w in app.workers if w.group == "attachment-import"]:
            await app.workers.wait_for_complete(workers)
        content = composer.input_content
        assert content.text == "draft"
        assert [item.name for item in content.attachments] == [path.name for path in paths]
        assert all(item.kind == "upload" for item in content.attachments)
        assert app._controller.session_id == origin
        assert len(application.runtime.store.list_sessions()) == int(existing)
        identity = origin or app._input_drafts.welcome_resource_id
        for item in content.attachments:
            saved = application.runtime.resources.directory_for(identity) / item.name
            assert saved.read_text(encoding="utf-8") == item.name

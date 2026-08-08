from __future__ import annotations

from textual.events import Key

from ouro.tui.keyboard import normalize_windows_modified_enter


def test_windows_shift_enter_is_preserved_when_terminal_reports_plain_enter() -> None:
    event = normalize_windows_modified_enter(Key("enter", "\r"), shift_pressed=True)

    assert event.key == "shift+enter"
    assert event.character is None


def test_windows_plain_and_already_modified_enter_are_unchanged() -> None:
    plain = Key("enter", "\r")
    modified = Key("shift+enter", None)

    assert normalize_windows_modified_enter(plain, shift_pressed=False) is plain
    assert normalize_windows_modified_enter(modified, shift_pressed=True) is modified

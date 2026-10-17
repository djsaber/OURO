from __future__ import annotations

from textual.events import Key

from ouro.tui.keyboard import normalize_windows_alt_key


def test_windows_alt_is_restored_when_console_reports_plain_characters() -> None:
    character = normalize_windows_alt_key(Key("h", "h"), alt_pressed=True)
    parser_artifact = normalize_windows_alt_key(
        Key("circumflex_accent", "^"),
        alt_pressed=True,
    )

    assert character.key == "alt+h"
    assert character.character == "h"
    assert parser_artifact.key == "alt+circumflex_accent"
    assert parser_artifact.character == "^"


def test_windows_plain_and_already_modified_keys_preserve_their_identity() -> None:
    plain = Key("h", "h")
    modified = Key("alt+h", "h")

    assert normalize_windows_alt_key(plain, alt_pressed=False) is plain
    assert normalize_windows_alt_key(modified, alt_pressed=True) is modified

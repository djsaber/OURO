from __future__ import annotations

from io import StringIO

import pytest

from ouro.tui import entry
from ouro.tui.terminal import set_terminal_title


class TerminalStream(StringIO):
    def __init__(self, *, tty: bool = True, fail: bool = False) -> None:
        super().__init__()
        self._tty = tty
        self._fail = fail

    def isatty(self) -> bool:
        return self._tty

    def write(self, value: str) -> int:
        if self._fail:
            raise OSError("terminal unavailable")
        return super().write(value)


def test_terminal_title_uses_standard_osc_sequence_for_tty() -> None:
    stream = TerminalStream()

    assert set_terminal_title("OURO", stream=stream) is True

    assert stream.getvalue() == "\x1b]0;OURO\x07"


def test_terminal_title_skips_non_interactive_output() -> None:
    stream = TerminalStream(tty=False)

    assert set_terminal_title("OURO", stream=stream) is False
    assert stream.getvalue() == ""


def test_terminal_title_failure_does_not_block_tui_startup() -> None:
    assert set_terminal_title("OURO", stream=TerminalStream(fail=True)) is False

    closed_stream = TerminalStream()
    closed_stream.close()
    assert set_terminal_title("OURO", stream=closed_stream) is False


def test_terminal_title_strips_control_characters() -> None:
    stream = TerminalStream()

    set_terminal_title("OURO\x1b]2;unsafe\x07", stream=stream)

    assert stream.getvalue() == "\x1b]0;OURO]2;unsafe\x07"


def test_run_tui_sets_title_before_starting_textual(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class FakeApp:
        def __init__(self, **_kwargs: object) -> None:
            calls.append("app")

        def run(self) -> int:
            calls.append("run")
            return 0

    monkeypatch.setattr(entry, "set_terminal_title", lambda title: calls.append(f"title:{title}"))
    monkeypatch.setattr(entry, "OuroTuiApp", FakeApp)

    assert entry.run_tui() == 0
    assert calls == ["title:OURO", "app", "run"]

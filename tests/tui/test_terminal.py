from __future__ import annotations

from contextlib import contextmanager
from io import StringIO

import pytest

from ouro.tui import entry, terminal
from ouro.tui.terminal import set_terminal_title, tui_terminal_guard


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


def test_tui_terminal_guard_resets_protocols_after_failure() -> None:
    output = TerminalStream()

    with pytest.raises(RuntimeError, match="startup failed"):
        with tui_terminal_guard(input_stream=TerminalStream(), output_stream=output):
            raise RuntimeError("startup failed")

    reset = output.getvalue()
    assert "\x1b[?1003l" in reset
    assert "\x1b[?1006l" in reset
    assert "\x1b[?2004l" in reset
    assert "\x1b[?1049l" in reset
    assert reset.endswith("\x1b[?25h")


def test_tui_terminal_guard_skips_reset_for_redirected_output() -> None:
    output = TerminalStream(tty=False)

    with tui_terminal_guard(input_stream=TerminalStream(tty=False), output_stream=output):
        pass

    assert output.getvalue() == ""


def test_tui_terminal_guard_restores_console_state_after_reset_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = object()
    calls: list[object] = []
    monkeypatch.setattr(
        terminal,
        "_capture_windows_console_state",
        lambda _input, _output: state,
    )
    monkeypatch.setattr(
        terminal,
        "_prepare_windows_console_for_reset",
        lambda captured: calls.append(("prepare", captured)),
    )

    def fail_reset(_stream: object) -> bool:
        calls.append("reset")
        raise RuntimeError("reset failed")

    monkeypatch.setattr(terminal, "_write_tui_reset", fail_reset)
    monkeypatch.setattr(
        terminal,
        "_restore_windows_console_state",
        lambda captured: calls.append(("restore", captured)),
    )

    with pytest.raises(RuntimeError, match="reset failed"):
        with tui_terminal_guard(input_stream=TerminalStream(), output_stream=TerminalStream()):
            calls.append("body")

    assert calls == ["body", ("prepare", state), "reset", ("restore", state)]


def test_windows_console_reset_restores_captured_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, int, int | None]] = []
    state = terminal._WindowsConsoleState(
        input_handle=10,
        input_mode=0x1F,
        output_handle=20,
        output_mode=0x03,
    )
    monkeypatch.setattr(
        terminal,
        "_set_windows_console_mode",
        lambda handle, mode: calls.append(("set", handle, mode)) or True,
    )
    monkeypatch.setattr(
        terminal,
        "_flush_windows_console_input",
        lambda handle: calls.append(("flush", handle, None)) or True,
    )

    terminal._prepare_windows_console_for_reset(state)
    terminal._restore_windows_console_state(state)

    assert calls == [
        ("set", 20, 0x07),
        ("flush", 10, None),
        ("set", 10, 0x1F),
        ("set", 20, 0x03),
    ]


def test_run_tui_sets_title_before_starting_textual(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    @contextmanager
    def fake_terminal_guard():
        calls.append("guard:enter")
        try:
            yield
        finally:
            calls.append("guard:exit")

    class FakeApp:
        def __init__(self, **_kwargs: object) -> None:
            calls.append("app")

        def run(self) -> int:
            calls.append("run")
            return 0

    monkeypatch.setattr(entry, "set_terminal_title", lambda title: calls.append(f"title:{title}"))
    monkeypatch.setattr(entry, "tui_terminal_guard", fake_terminal_guard)
    monkeypatch.setattr(entry, "OuroTuiApp", FakeApp)

    assert entry.run_tui() == 0
    assert calls == ["guard:enter", "title:OURO", "app", "run", "guard:exit"]

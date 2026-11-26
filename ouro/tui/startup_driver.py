"""Transfer the existing startup terminal surface to Textual without clearing it."""

from textual.driver import Driver

from ouro.tui.startup import TerminalStartup


def startup_driver(driver_class: type[Driver], startup: TerminalStartup) -> type[Driver]:
    class StartupDriver(driver_class):  # type: ignore[valid-type, misc]
        _preserve_startup = False

        def start_application_mode(self) -> None:
            self._preserve_startup = startup.active
            startup.stop()
            try:
                super().start_application_mode()
            finally:
                self._preserve_startup = False

        def write(self, data: str) -> None:
            # Re-entering the Windows Terminal alternate screen clears the startup frame.
            if self._preserve_startup and data == "\x1b[?1049h":
                return
            super().write(data)

    return StartupDriver

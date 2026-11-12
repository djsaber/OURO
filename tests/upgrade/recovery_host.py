"""A minimal Host for the offline recovery presentation contract."""

from textual.app import App

from ouro.tui.menus.styles import MENU_CSS
from ouro.tui.recovery import DataRecoveryScreen
from ouro.tui.theme import DEFAULT_TUI_THEME


class RecoveryHost(App[str]):
    CSS = MENU_CSS

    def __init__(self, *, error, auto_upgrade, theme=DEFAULT_TUI_THEME, **kwargs):
        self.tui_theme = theme
        super().__init__()
        self.recovery = DataRecoveryScreen(**kwargs)
        self.error = error
        self.auto_upgrade = auto_upgrade

    def get_css_variables(self):
        return {**super().get_css_variables(), **self.tui_theme.css_variables()}

    async def on_mount(self):
        await self.push_screen(self.recovery)
        self.run_worker(self.recover())

    async def recover(self):
        result = await self.recovery.recover(self.error, auto_upgrade=self.auto_upgrade)
        self.exit("upgraded" if result else None)

    async def action_quit(self):
        await self.recovery.action_cancel()

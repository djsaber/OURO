from __future__ import annotations

import asyncio
import threading
from functools import partial
from pathlib import Path

import pytest
from rich.segment import Segment
from rich.text import Text
from textual.app import App
from textual.color import Color
from textual.geometry import Region
from textual.strip import Strip
from textual.widgets import Input, OptionList, Static

from ouro.core.errors import ConfigError, PersistenceError
from ouro.tui.app import OuroTuiApp
from ouro.tui.controller import TuiSystemStatus
from ouro.tui.preferences import TuiPreferences, TuiPreferencesStore
from ouro.tui.screens import ChoiceScreen
from ouro.tui.theme import TUI_THEMES
from ouro.tui.welcome import WELCOME_STYLES, WelcomePage, WelcomeStyle, welcome_catalog
from ouro.tui.welcome.components import WelcomeUsage
from ouro.tui.welcome.dot_matrix.widget import DotWelcomePage
from ouro.tui.welcome.host import WelcomeHost
from ouro.tui.widgets import CommandPopup, Composer, WelcomePanel
from tests.tui.helpers import run_ready_app, two_route_application
from tests.tui.test_app import _wait_until


class CustomPage(WelcomePage):
    """A non-dot custom package proves the host has no graphics-specific contract."""

    DEFAULT_CSS = "CustomPage { content-align: center middle; }"

    def __init__(self):
        super().__init__()
        self.playback = (False, True)

    def render(self):
        return Text("CUSTOM WELCOME")

    def place_input(self, viewport, measure):
        width = max(1, viewport.width - 4)
        height = measure(width)
        return Region((viewport.width - width) // 2, viewport.height - height, width, height)

    def set_playback(self, *, active: bool, motion: bool) -> None:
        self.playback = active, motion


CUSTOM = WelcomeStyle("custom-scene", "Custom scene", "A custom component.", CustomPage)


class BytePage(WelcomePage):
    """Exercise full-viewport rendering, composition, scoped CSS and alternate layout."""

    SCOPED_CSS = False
    DEFAULT_CSS = """
    BytePage .byte-label { width: 20; height: 1; offset: 2 6; }
    BytePage WelcomeUsage { width: 28; height: 4; offset: 2 8; }
    #input-region.welcome-style-byte-page #interaction-surface {
        background: #123456; padding: 0 1;
    }
    """

    def __init__(self, *, top=False):
        super().__init__()
        self.top = top
        self.frames = 0
        self.layouts = 0
        self.timer = None
        self.playback = (False, True)
        self.usage = WelcomeUsage()
        self.theme_updates = 0

    def compose(self):
        yield Static("OURO / BYTE FIELD", classes="byte-label")
        yield self.usage

    def place_input(self, viewport, measure):
        self.layouts += 1
        width = viewport.width
        height = measure(width)
        return Region(0, 0 if self.top else viewport.height - height, width, height)

    def set_system_status(self, status):
        self.usage.set_system_status(status)

    def on_mount(self):
        self.timer = self.set_interval(1 / 30, self.advance, pause=True)
        self.set_playback(active=self.playback[0], motion=self.playback[1])

    def advance(self):
        self.frames += 1
        self.refresh()

    def set_playback(self, *, active, motion):
        self.playback = active, motion
        if self.timer is not None:
            if active and motion:
                self.timer.resume()
            else:
                self.timer.pause()

    def on_unmount(self):
        if self.timer is not None:
            self.timer.stop()
            self.timer = None

    def refresh_theme(self):
        self.theme_updates += 1
        self.refresh()

    def render_line(self, y):
        text = "".join(f"{(x * 7 + y + self.frames) % 16:X}" for x in range(self.size.width))
        return Strip([Segment(text, self.rich_style)])


@pytest.mark.parametrize("top", [False, True])
async def test_full_page_controls_viewport_input_layout_composition_and_style_scope(tmp_path, top):
    package = WelcomeStyle(
        "byte-page", "Byte page", "Whole-page character field", partial(BytePage, top=top)
    )
    app = OuroTuiApp(application=two_route_application(tmp_path), welcome_styles=(package,))
    async with run_ready_app(app, size=(160, 48)) as pilot:
        host = app.query_one(WelcomePanel)
        composer = app.query_one(Composer)
        composer.load_text("a draft\nwith two lines")
        selection = composer.selection
        surface = host.input_surface
        await host.set_welcome_style(package, motion=True)
        await pilot.pause(0.15)
        page = host.page
        assert isinstance(page, BytePage)
        assert page.frames > 0
        assert page.query_one(".byte-label", Static).render().plain == "OURO / BYTE FIELD"
        assert surface.styles.background == Color.parse("#123456")
        assert composer.selection == selection and app.focused is composer
        host.set_system_status(TuiSystemStatus(usage_request_count=42))
        assert page.usage._requests.render().plain == "42 request(s)"
        assert not host.query("#welcome-version")
        await pilot.pause()
        frames, layouts = page.frames, page.layouts
        await pilot.pause(0.15)
        assert page.frames > frames and page.layouts == layouts

        for width, height in ((160, 48), (40, 16), (200, 60)):
            await pilot.resize_terminal(width, height)
            await pilot.pause()
            assert page.region == host.region == app.screen.region
            assert surface.region.width == width
            assert surface.region.y == (0 if top else height - surface.region.height)
            assert composer.region in surface.region
            # Sample the actual composited frame outside the shared input and child widgets.
            y = height - 1 if top else 0
            row = app.screen._compositor.render_strips()[y].text
            assert len(row) == width
            assert row[0] in "0123456789ABCDEF" and row[-1] in "0123456789ABCDEF"

        composer.load_text("/")
        await pilot.pause()
        popup = app.query_one(CommandPopup)
        assert popup.display and popup.region in app.screen.region
        assert (
            popup.region.y >= surface.region.bottom
            if top
            else popup.region.bottom <= surface.region.y
        )
        assert composer.text == "/" and app.focused is composer
        composer.load_text("a draft\nwith two lines")
        await pilot.pause()

        await pilot.press("ctrl+p")
        frames = page.frames
        await pilot.pause(0.12)
        assert page.frames == frames and not page.playback[0]
        await pilot.press("escape")
        await pilot.pause(0.12)
        assert page.frames > frames
        await host.set_welcome_style(package, motion=False)
        frames = page.frames
        await pilot.pause(0.12)
        assert page.frames == frames
        app._refresh_theme_content()
        assert page.theme_updates > 0

        host.set_welcome_mode(False)
        await pilot.pause()
        assert not page.display
        assert not host.has_class("welcome-style-byte-page")
        assert surface.styles.background != Color.parse("#123456")
        host.set_welcome_mode(True)
        await host.set_welcome_style(WELCOME_STYLES["basic"], motion=True)
        await pilot.pause()
        assert page.timer is None
        assert host.input_surface is surface and app.query_one(Composer) is composer
        assert composer.text == "a draft\nwith two lines"
        assert not host.has_class("welcome-style-byte-page")
        assert surface.styles.background != Color.parse("#123456")


async def test_invalid_factory_keeps_the_mounted_page_and_input(tmp_path):
    TuiPreferencesStore(tmp_path).save(TuiPreferences(welcome_style="event-horizon"))
    app = OuroTuiApp(application=two_route_application(tmp_path))
    async with run_ready_app(app):
        host = app.query_one(WelcomePanel)
        page, surface = host.page, host.input_surface
        for create in (lambda: Static("invalid"), lambda: page):
            invalid = WelcomeStyle("invalid", "Invalid", "Invalid factory", create)
            with pytest.raises(
                ValueError, match="invalid: factory must return a fresh WelcomePage"
            ):
                await host.set_welcome_style(invalid, motion=True)
            assert host.page is page and host.input_surface is surface
            assert page.is_attached and page.matrix.active


@pytest.mark.parametrize("kind", ["width", "outside", "empty", "fractional"])
async def test_page_layout_rejects_invalid_input_geometry_with_package_identity(kind):
    class InvalidPage(WelcomePage):
        def place_input(self, viewport, measure):
            if kind == "width":
                measure(0)
            if kind == "empty":
                return Region()
            if kind == "fractional":
                return Region(0.5, 0, 4, 4)
            return Region(viewport.width, 0, 4, 4)

    class LayoutApp(App):
        def compose(self):
            package = WelcomeStyle("invalid-page", "Invalid", "Invalid geometry", InvalidPage)
            host = WelcomeHost(Static("input"), package, motion=False)
            host.set_welcome_mode(True)
            yield host

    with pytest.raises(ValueError, match="invalid-page: invalid input"):
        async with LayoutApp().run_test(size=(40, 16)) as pilot:
            await pilot.pause()


def test_custom_catalog_is_immutable_isolated_and_rejects_colliding_identities():
    catalog = welcome_catalog((CUSTOM,))
    assert "custom-scene" not in WELCOME_STYLES
    assert catalog["custom-scene"].create() is not catalog["custom-scene"].create()
    with pytest.raises(TypeError):
        catalog["extra"] = CUSTOM  # type: ignore[index]
    with pytest.raises(ValueError, match="duplicate"):
        welcome_catalog((CUSTOM, CUSTOM))
    with pytest.raises(ValueError, match="lowercase"):
        WelcomeStyle("../scene", "Bad", "Bad ID", CustomPage)


def test_style_and_motion_persist_independently_of_color_theme_and_validate_against_catalog(
    tmp_path,
):
    store = TuiPreferencesStore(tmp_path, welcome_styles=welcome_catalog((CUSTOM,)))
    preferences = store.validate(
        {"welcome_style": CUSTOM.style_id, "welcome_motion": False, "theme": "coral"}
    )
    store.save(preferences)
    assert store.load() == preferences
    with pytest.raises(ConfigError, match="unknown welcome style"):
        TuiPreferencesStore(tmp_path).load()
    with pytest.raises(ConfigError, match="unknown welcome style"):
        store.save(preferences.model_copy(update={"welcome_style": "missing"}))


async def select_menu(app, pilot, label):
    app.screen.query_one("#choice-filter", Input).value = label
    await pilot.pause()
    await pilot.press("enter")
    await pilot.pause()
    workers = [worker for worker in app.workers if worker.group.startswith("choice-selection-")]
    await asyncio.wait_for(asyncio.gather(*(worker.wait() for worker in workers)), 3)


async def open_welcome_picker(app, pilot):
    await pilot.press("ctrl+p")
    await select_menu(app, pilot, "Theme")
    assert isinstance(app.screen, ChoiceScreen)
    assert app.screen.page_title == "THEME"


@pytest.mark.parametrize("standalone", [False, True])
async def test_theme_menu_groups_inline_styles_and_keeps_color_settings_independent(
    tmp_path: Path, standalone: bool
):
    # A package ID may also be a color ID; sharing a menu must not couple them.
    package = WelcomeStyle("coral", "Ember", "A custom component.", CustomPage)
    app = OuroTuiApp(application=two_route_application(tmp_path), welcome_styles=(package,))
    async with run_ready_app(app, size=(80, 24)) as pilot:
        if standalone:
            app.run_worker(app._choose_theme())
            await pilot.pause()
        else:
            await open_welcome_picker(app, pilot)
        menu = app.screen
        assert isinstance(menu, ChoiceScreen)
        revision = menu.page_revision
        choices = menu._choices
        assert list(dict.fromkeys(choice.group for choice in choices)) == [
            "Welcome style",
            "Theme color",
        ]
        assert len({choice.value for choice in choices}) == len(choices)
        assert all(not choice.description for choice in choices)
        style_choices = choices[: len(WELCOME_STYLES) + 1]
        assert [choice.label for choice in style_choices] == [
            "Still",
            "Horizon",
            "Bloom",
            "Labyrinth",
            "Tide",
            "Iris",
            "Prism",
            "Ember",
        ]
        options = menu.query_one(OptionList)
        assert options.get_option_at_index(options.highlighted).id == "welcome:basic"
        assert options.get_option_at_index(0).disabled
        assert options.get_option_at_index(0).prompt.plain == "Welcome style"
        assert all(
            len(options.get_option(choice.value).prompt.plain.splitlines()) == 1
            for choice in style_choices
        )
        options.highlighted = options.get_option_index("coral")
        await _wait_until(
            lambda: (
                menu.query_one("#choice-title", Static).styles.color
                == Color.parse(TUI_THEMES["coral"].accent)
            ),
            pilot,
            description="color preview",
        )

        menu.query_one(Input).value = "Welcome style"
        await pilot.pause()
        assert options.option_count == len(style_choices) + 2  # heading and animation
        await _wait_until(
            lambda: (
                menu.query_one("#choice-title", Static).styles.color
                == Color.parse(app.tui_theme.accent)
            ),
            pilot,
            description="welcome group to clear the color preview",
        )
        menu.query_one(Input).value = "Ember"
        await pilot.pause()
        await pilot.press("left", "right")
        await select_menu(app, pilot, "Ember")
        assert app.screen is menu and menu.page_revision == revision
        assert isinstance(app.query_one(WelcomePanel).page, CustomPage)
        preferences = app._preferences_store.load()
        assert preferences.welcome_style == "coral"
        assert preferences.theme == "original" and preferences.theme_saturation_level == 0
        assert next(choice for choice in menu._choices if choice.label == "Ember").marker

        menu.query_one(Input).value = "Theme color"
        await pilot.pause()
        assert options.option_count == len(TUI_THEMES) + 1
        options.highlighted = options.get_option_index("coral")
        await pilot.pause()
        await pilot.press("right", "enter")
        workers = [worker for worker in app.workers if worker.group == "theme-switch"]
        await asyncio.wait_for(asyncio.gather(*(worker.wait() for worker in workers)), 3)
        await pilot.pause()
        await select_menu(app, pilot, "Animation")
        assert app.screen is menu and menu.page_revision == revision
        preferences = app._preferences_store.load()
        assert preferences.theme == "coral" and preferences.theme_saturation_level == 1
        assert preferences.welcome_style == "coral" and not preferences.welcome_motion
        assert next(choice for choice in menu._choices if choice.label == "Animation").trailing == (
            "Paused"
        )
        await pilot.press("escape")
        if not standalone:
            assert app.screen is menu and menu.page_title == "ACTIONS"
            await pilot.press("escape")
        assert app.focused is app.query_one(Composer)


async def test_picker_mounts_arbitrary_custom_package_preserves_input_and_persists(tmp_path: Path):
    app = OuroTuiApp(application=two_route_application(tmp_path), welcome_styles=(CUSTOM,))
    async with run_ready_app(app, size=(100, 40)) as pilot:
        composer = app.query_one(Composer)
        await pilot.press("d", "r", "a", "f", "t")
        await open_welcome_picker(app, pilot)
        menu = app.screen
        await select_menu(app, pilot, "Custom scene")
        slot = app.query_one(WelcomePanel)
        assert isinstance(slot.page, CustomPage)
        assert slot.page.playback == (False, True)
        assert app.screen is menu
        await pilot.press("escape", "escape")
        await pilot.pause()
        assert slot.page.playback == (True, True)
        assert composer.text == "draft"
        assert app.focused is composer
        assert app._preferences_store.load().welcome_style == CUSTOM.style_id
        assert app._preferences_store.load().theme == "original"

    restarted = OuroTuiApp(application=two_route_application(tmp_path), welcome_styles=(CUSTOM,))
    async with run_ready_app(restarted):
        assert isinstance(restarted.query_one(WelcomePanel).page, CustomPage)


async def test_dot_playback_pauses_for_menu_conversation_and_motion_setting_and_releases_timer(
    tmp_path: Path,
):
    TuiPreferencesStore(tmp_path).save(TuiPreferences(welcome_style="event-horizon"))
    app = OuroTuiApp(application=two_route_application(tmp_path))
    async with run_ready_app(app, size=(100, 40)) as pilot:
        panel = app.query_one(WelcomePanel)
        visual = panel.page
        assert isinstance(visual, DotWelcomePage)
        matrix = visual.matrix
        await pilot.pause(0.12)
        assert matrix.elapsed > 0
        await open_welcome_picker(app, pilot)
        before = matrix.elapsed
        await pilot.pause(0.12)
        assert not matrix.active and matrix.elapsed == before
        await select_menu(app, pilot, "Animation")
        await pilot.press("escape", "escape")
        await pilot.pause(0.12)
        assert matrix.elapsed == before
        assert not app._preferences_store.load().welcome_motion
        await panel.set_welcome_style(WELCOME_STYLES["event-horizon"], motion=True)
        await pilot.pause(0.12)
        assert matrix.elapsed > before
        panel.set_welcome_mode(False)
        before = matrix.elapsed
        await pilot.pause(0.12)
        assert matrix.elapsed == before
        panel.set_welcome_mode(True)
        for size in ((40, 16), (80, 24), (160, 48)):
            await pilot.resize_terminal(*size)
            await pilot.pause()
            assert matrix.region in panel.page._top.region
            assert matrix._raster is not None
            assert matrix._raster.grid.columns == matrix.size.width
        await panel.set_welcome_style(WELCOME_STYLES["basic"], motion=True)
        await pilot.pause()
        assert matrix._timer is None
        assert not list(panel.query(DotWelcomePage))
        assert app.query_one(Composer).region.y == panel.page._top.region.bottom


async def test_failed_style_save_keeps_current_package_and_preference(tmp_path, monkeypatch):
    app = OuroTuiApp(application=two_route_application(tmp_path))
    async with run_ready_app(app) as pilot:
        original = app.query_one(WelcomePanel).page
        await open_welcome_picker(app, pilot)

        def fail_save(_preferences: TuiPreferences):
            raise PersistenceError("style storage unavailable")

        monkeypatch.setattr(app._preferences_store, "save", fail_save)
        await select_menu(app, pilot, "Bloom")
        assert app.query_one(WelcomePanel).page is original
        assert app._preferences.welcome_style == "basic"
        assert app._preferences_store.load().welcome_style == "basic"


@pytest.mark.parametrize(
    ("next_choice", "style_id", "theme_id"),
    [("Labyrinth", "julia-reliquary", "original"), ("Coral", "electron-bloom", "coral")],
)
async def test_style_transaction_survives_reselection_and_picker_close(
    tmp_path, monkeypatch, next_choice, style_id, theme_id
):
    app = OuroTuiApp(application=two_route_application(tmp_path))
    entered, release = threading.Event(), threading.Event()
    async with run_ready_app(app) as pilot:
        await open_welcome_picker(app, pilot)
        save = app._preferences_store.save

        def slow_save(preferences):
            if preferences.welcome_style == "electron-bloom":
                entered.set()
                assert release.wait(5)
            save(preferences)

        monkeypatch.setattr(app._preferences_store, "save", slow_save)
        try:
            app.screen.query_one("#choice-filter", Input).value = "Bloom"
            await pilot.pause()
            await pilot.press("enter")
            assert await asyncio.to_thread(entered.wait, 2)
            app.screen.query_one("#choice-filter", Input).value = next_choice
            await pilot.pause()
            await pilot.press("enter")
            await pilot.press("escape", "escape")
        finally:
            release.set()
        workers = [
            worker for worker in app.workers if worker.group in {"welcome-style", "theme-switch"}
        ]
        await asyncio.wait_for(asyncio.gather(*(worker.wait() for worker in workers)), 3)
        await pilot.pause()
        preferences = app._preferences_store.load()
        assert preferences.welcome_style == style_id
        assert preferences.theme == theme_id
        assert app.query_one(WelcomePanel).style_package.style_id == style_id
        assert app.focused is app.query_one(Composer)

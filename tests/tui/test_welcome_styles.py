from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from functools import partial
from pathlib import Path

import pytest
from ouro_tui_themes.dot_matrix.widget import DotMatrix
from ouro_tui_themes.styles import STYLES
from rich.segment import Segment
from rich.text import Text
from textual.app import App
from textual.drivers.headless_driver import HeadlessDriver
from textual.geometry import Region
from textual.strip import Strip
from textual.widgets import Input, OptionList, Static

from ouro.core.errors import ConfigError, PersistenceError
from ouro.tui.app import OuroTuiApp
from ouro.tui.composer.input import Composer
from ouro.tui.composer.popup import CommandPopup
from ouro.tui.composer.surface import WelcomePanel
from ouro.tui.menus.choice import ChoiceScreen
from ouro.tui.preferences import TuiPreferences, TuiPreferencesStore
from ouro.tui.theme import DEFAULT_TUI_THEME, TUI_THEMES
from ouro.tui.view_models import TuiSystemStatus
from ouro.tui.welcome import WelcomePage, WelcomeStyle, welcome_catalog
from ouro.tui.welcome.components import WelcomeUsage
from ouro.tui.welcome.host import WelcomeHost
from tests.tui.helpers import resize_terminal, run_ready_app, two_route_application, wait_until

WELCOME_STYLES = welcome_catalog(STYLES)


class CustomPage(WelcomePage):
    """A non-dot custom package proves the host has no graphics-specific contract."""

    DEFAULT_CSS = "CustomPage { content-align: center middle; }"

    def __init__(self):
        super().__init__()
        self.active = False

    def render(self):
        return Text("CUSTOM WELCOME")

    def place_input(self, viewport, measure):
        width = max(1, viewport.width - 4)
        height = measure(width)
        return Region((viewport.width - width) // 2, viewport.height - height, width, height)

    def set_active(self, active: bool) -> None:
        self.active = active


CUSTOM = WelcomeStyle("custom-scene", "Custom scene", "A custom component.", CustomPage)


async def test_custom_page_tracks_initial_palette_saturation_and_hidden_updates(tmp_path):
    class PalettePage(CustomPage):
        DEFAULT_CSS = """
        PalettePage .palette-css { color: $ouro-theme; background: $ouro-panel; height: 1; }
        PalettePage .palette-rich { height: 1; }
        """

        def __init__(self):
            super().__init__()
            self.palettes = []

        def compose(self):
            yield Static("CSS palette", classes="palette-css")
            yield Static(classes="palette-rich")

        def refresh_theme(self):
            theme = self.tui_theme
            self.palettes.append(theme)
            self.query_one(".palette-rich", Static).update(Text(theme.label, style=theme.accent))

    package = WelcomeStyle("palette-page", "Palette", "Follows TUI colors", PalettePage)
    store = TuiPreferencesStore(tmp_path)
    store.save(
        store.validate(
            {
                "welcome_style": package.style_id,
                "theme": "coral",
                "theme_saturation_level": 5,
            }
        )
    )
    app = OuroTuiApp(application=two_route_application(tmp_path), welcome_styles=(package,))
    async with run_ready_app(app) as pilot:
        host, composer = app.query_one(WelcomePanel), app.query_one(Composer)
        page = host.page
        composer.load_text("keep this draft")
        selection = composer.selection

        def assert_palette(theme_id, level):
            expected = TUI_THEMES[theme_id].at_saturation_level(level)
            assert page.tui_theme == page.palettes[-1] == expected
            assert page.query_one(".palette-css").styles.color.hex.lower() == expected.accent
            assert page.query_one(".palette-css").styles.background.hex.lower() == expected.panel
            content = page.query_one(".palette-rich", Static).render()
            assert content.render_segments()[0].style.color.name == expected.accent

        assert_palette("coral", 5)
        await pilot.press("ctrl+p")
        assert not page.active
        for theme_id, level in (("blossom", 0), ("blossom", 20)):
            count = len(page.palettes)
            app._request_tui_theme(theme_id, saturation_level=level)
            workers = [worker for worker in app.workers if worker.group == "theme-switch"]
            await asyncio.gather(*(worker.wait() for worker in workers))
            await pilot.pause()
            assert host.page is page and len(page.palettes) == count + 1
            assert not page.active
            assert_palette(theme_id, level)

        # A newly loaded page receives the currently resolved palette behind the menu.
        await host.set_welcome_style(WELCOME_STYLES["basic"])
        await host.set_welcome_style(package)
        page = host.page
        assert_palette("blossom", 20)
        assert not page.active
        await pilot.press("escape")
        assert page.active
        assert app.query_one(Composer) is composer and app.focused is composer
        assert composer.text == "keep this draft" and composer.selection == selection


@pytest.mark.parametrize(
    "style", [style for key, style in WELCOME_STYLES.items() if key != "basic"]
)
async def test_particle_page_owns_caption_independently_of_catalog_label(style):
    class PackageApp(App):
        def get_css_variables(self):
            return {**super().get_css_variables(), **DEFAULT_TUI_THEME.css_variables()}

        def compose(self):
            host = WelcomeHost(Static("input"), replace(style, label="Menu label"))
            host.set_welcome_mode(True)
            yield host

    app = PackageApp()
    async with app.run_test(size=(120, 40)):
        page = app.query_one(WelcomeHost).page
        assert page.query_one(".art-caption", Static).render().plain == (
            f"O U R O  /  {style.label.upper()}"
        )
        assert page.query_one(DotMatrix) is page.matrix


async def test_package_owns_pointer_interaction_without_replacing_host_input():
    class InteractivePage(CustomPage):
        def __init__(self):
            super().__init__()
            self.clicks = 0

        def on_click(self, event):
            event.stop()
            self.clicks += 1
            self.refresh()

        def render(self):
            return Text(f"LOCAL ACTION {self.clicks}")

    class PackageApp(App):
        def compose(self):
            package = WelcomeStyle(
                "interactive", "Interactive", "Local interaction", InteractivePage
            )
            host = WelcomeHost(Composer(), package)
            host.set_welcome_mode(True)
            yield host

    app = PackageApp()
    async with app.run_test() as pilot:
        page, composer = app.query_one(InteractivePage), app.query_one(Composer)
        composer.load_text("draft")
        composer.focus()
        await pilot.click(InteractivePage, offset=(1, 1))
        assert page.clicks == 1 and page.render().plain == "LOCAL ACTION 1"
        assert composer.text == "draft"
        await pilot.press("end", "!")
        assert composer.text == "draft!"


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
        self.active = False
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
        self.set_active(self.active)

    def advance(self):
        self.frames += 1
        self.refresh()

    def set_active(self, active):
        self.active = active
        if self.timer is not None:
            if active:
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
        await host.set_welcome_style(package)
        page = host.page
        assert isinstance(page, BytePage)
        await wait_until(lambda: page.frames > 0, pilot, description="first welcome frame")
        assert page.frames > 0
        assert page.query_one(".byte-label", Static).render().plain == "OURO / BYTE FIELD"
        assert composer.selection == selection and app.focused is composer
        host.set_system_status(TuiSystemStatus(usage_request_count=42))
        assert page.usage._requests.render().plain == "42 request(s)"
        assert not host.query("#welcome-version")
        await pilot.pause()
        frames, layouts = page.frames, page.layouts
        await wait_until(lambda: page.frames > frames, pilot, description="next welcome frame")
        assert page.frames > frames and page.layouts == layouts

        for width, height in ((160, 48), (40, 16), (200, 60)):
            await resize_terminal(pilot, width, height)
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
        assert page.frames == frames and not page.active
        await pilot.press("escape")
        await wait_until(lambda: page.frames > frames, pilot, description="resumed welcome frames")
        assert page.frames > frames
        host.set_welcome_mode(False)
        frames = page.frames
        await pilot.pause(0.12)
        assert page.frames == frames
        app._refresh_theme_content()
        assert page.theme_updates > 0

        host.set_welcome_mode(False)
        await pilot.pause()
        assert not page.display
        assert not host.has_class("welcome-style-byte-page")
        host.set_welcome_mode(True)
        await host.set_welcome_style(WELCOME_STYLES["basic"])
        await pilot.pause()
        assert page.timer is None
        assert host.input_surface is surface and app.query_one(Composer) is composer
        assert composer.text == "a draft\nwith two lines"
        assert not host.has_class("welcome-style-byte-page")


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
                await host.set_welcome_style(invalid)
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
            host = WelcomeHost(Static("input"), package)
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


def test_style_persists_independently_of_color_theme_and_installed_catalog(
    tmp_path,
):
    store = TuiPreferencesStore(tmp_path)
    preferences = store.validate({"welcome_style": CUSTOM.style_id, "theme": "coral"})
    store.save(preferences)
    assert store.load() == preferences
    assert TuiPreferencesStore(tmp_path).load() == preferences
    with pytest.raises(ConfigError, match="String should match pattern"):
        store.save(preferences.model_copy(update={"welcome_style": "../missing"}))


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
        await pilot.pause()

        menu.query_one(Input).value = "Welcome style"
        await pilot.pause()
        assert options.option_count == len(style_choices) + 1  # group heading
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
        assert app.screen is menu and menu.page_revision == revision
        preferences = app._preferences_store.load()
        assert preferences.theme == "coral" and preferences.theme_saturation_level == 1
        assert preferences.welcome_style == "coral"
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
        assert not slot.page.active
        assert app.screen is menu
        await pilot.press("escape", "escape")
        await pilot.pause()
        assert slot.page.active
        assert composer.text == "draft"
        assert app.focused is composer
        assert app._preferences_store.load().welcome_style == CUSTOM.style_id
        assert app._preferences_store.load().theme == "original"

    restarted = OuroTuiApp(application=two_route_application(tmp_path), welcome_styles=(CUSTOM,))
    async with run_ready_app(restarted):
        assert isinstance(restarted.query_one(WelcomePanel).page, CustomPage)


async def test_particle_theme_pauses_when_hidden_and_releases_worker_on_replacement(
    tmp_path: Path,
):
    TuiPreferencesStore(tmp_path).save(TuiPreferences(welcome_style="event-horizon"))
    app = OuroTuiApp(application=two_route_application(tmp_path))
    async with run_ready_app(app, size=(100, 40)) as pilot:
        panel = app.query_one(WelcomePanel)
        visual = panel.page
        assert visual.query_one(DotMatrix) is visual.matrix
        matrix = visual.matrix
        await wait_until(
            lambda: matrix.elapsed > 0, pilot, description="particle animation started"
        )
        assert matrix.elapsed > 0
        await open_welcome_picker(app, pilot)
        before = matrix.elapsed
        await pilot.pause(0.12)
        assert not matrix.active and matrix.elapsed == before
        await pilot.press("escape", "escape")
        await wait_until(
            lambda: matrix.elapsed > before, pilot, description="particle animation resumed"
        )
        assert matrix.elapsed > before
        panel.set_welcome_mode(False)
        before = matrix.elapsed
        await pilot.pause(0.12)
        assert matrix.elapsed == before
        panel.set_welcome_mode(True)
        for size in ((40, 16), (80, 24), (160, 48)):
            await resize_terminal(pilot, *size)
            await pilot.pause()
            assert matrix.region in panel.page._top.region
            assert matrix._raster is not None
            assert matrix._raster.columns == matrix.size.width
        await panel.set_welcome_style(WELCOME_STYLES["basic"])
        await pilot.pause()
        assert not any(worker.node is matrix for worker in app.workers)
        assert not list(panel.query(DotMatrix))
        assert app.query_one(Composer).region.y == panel.page._top.region.bottom


def test_normal_startup_displays_saved_particle_theme(tmp_path: Path, monkeypatch):
    frames = []
    mount = DotMatrix.on_mount
    frame_loop = DotMatrix._render_frames
    worker_started = asyncio.Event()

    async def yielding_mount(matrix):
        mount(matrix)
        # Exercise the valid ordering where the worker starts before Mount completes.
        await worker_started.wait()

    async def early_frame_loop(matrix):
        assert not matrix.is_mounted
        worker_started.set()
        await frame_loop(matrix)

    monkeypatch.setattr(DotMatrix, "on_mount", yielding_mount)
    monkeypatch.setattr(DotMatrix, "_render_frames", early_frame_loop)

    class OutputSink(HeadlessDriver):
        @property
        def is_headless(self):
            return False

        def write(self, data):
            if any(0x2800 < ord(char) <= 0x28FF for char in data):
                frames.append(data)

    class StartupApp(OuroTuiApp):
        def get_driver_class(self):
            return OutputSink

        def on_ready(self):
            self.set_timer(5, lambda: self.exit(False))
            self.set_interval(1 / 30, self.check_output)

        def check_output(self):
            if len(set(frames)) >= 2:
                self.exit(True)

    TuiPreferencesStore(tmp_path).save(TuiPreferences(welcome_style="event-horizon"))
    app = StartupApp(application=two_route_application(tmp_path))
    # App.run uses production startup ordering; run_test can mask an early worker exit.
    assert app.run(size=(130, 31)), "the selected particle theme never reached terminal output"


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
    [("Tide", "interference", "original"), ("Coral", "electron-bloom", "coral")],
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

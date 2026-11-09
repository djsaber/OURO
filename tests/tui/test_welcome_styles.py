from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from functools import partial
from pathlib import Path

import pytest
from ouro_tui_themes.packs import PACKAGES
from ouro_tui_themes.tools.dot_matrix.widget import DotMatrix
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
from ouro.tui.startup_screen import StartupScreen
from ouro.tui.theme import TUI_THEMES
from ouro.tui.view_models import TuiSystemStatus
from ouro.tui.welcome import ThemePackage, WelcomePage, WelcomeStyle, welcome_catalog
from ouro.tui.welcome.components import WelcomeUsage
from ouro.tui.welcome.host import WelcomeHost
from tests.tui.helpers import resize_terminal, run_ready_app, two_route_application, wait_until

WELCOME_STYLES = welcome_catalog(PACKAGES)


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
CUSTOM_PACKAGE = ThemePackage("custom", "Custom themes", "Custom pages.", "Tests", (CUSTOM,))


async def test_custom_page_tracks_initial_palette_saturation_and_hidden_updates(tmp_path):
    class PalettePage(CustomPage):
        def __init__(self):
            super().__init__()
            self.palettes = []

        def refresh_theme(self):
            self.palettes.append(self.tui_theme)

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
    app = OuroTuiApp(
        application=two_route_application(tmp_path),
        welcome_packages=(replace(CUSTOM_PACKAGE, themes=(package,)),),
    )
    async with run_ready_app(app) as pilot:
        host, composer = app.query_one(WelcomePanel), app.query_one(Composer)
        page = host.page
        composer.load_text("keep this draft")
        selection = composer.selection

        def assert_palette(theme_id, level):
            expected = TUI_THEMES[theme_id].at_saturation_level(level)
            assert page.tui_theme == page.palettes[-1] == expected

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
    app = OuroTuiApp(
        application=two_route_application(tmp_path),
        welcome_packages=(replace(CUSTOM_PACKAGE, themes=(package,)),),
    )
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
    catalog = welcome_catalog((CUSTOM_PACKAGE,))
    assert "custom-scene" not in WELCOME_STYLES
    assert catalog["custom-scene"].create() is not catalog["custom-scene"].create()
    with pytest.raises(TypeError):
        catalog["extra"] = CUSTOM  # type: ignore[index]
    with pytest.raises(ValueError, match="duplicate"):
        welcome_catalog((CUSTOM_PACKAGE, CUSTOM_PACKAGE))
    with pytest.raises(ValueError, match="duplicate welcome style"):
        welcome_catalog((CUSTOM_PACKAGE, replace(CUSTOM_PACKAGE, package_id="another")))
    with pytest.raises(ValueError, match="lowercase"):
        replace(CUSTOM_PACKAGE, package_id="../package")
    with pytest.raises(ValueError, match="author and themes"):
        replace(CUSTOM_PACKAGE, themes=())
    for field in ("name", "description", "author"):
        with pytest.raises(ValueError, match="author and themes"):
            replace(CUSTOM_PACKAGE, **{field: " "})
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
async def test_theme_menu_navigates_packages_and_keeps_color_settings_independent(
    tmp_path: Path, standalone: bool
):
    # A theme ID may also be a color ID; selecting it must not change the palette.
    package = WelcomeStyle("coral", "Ember", "A custom component.", CustomPage)
    app = OuroTuiApp(
        application=two_route_application(tmp_path),
        welcome_packages=(replace(CUSTOM_PACKAGE, themes=(package,)),),
    )
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
        assert {choice.value for choice in choices if choice.group == "Welcome style"} == {
            "welcome:basic",
            "welcome-package:particle-art",
            "welcome-package:matrix",
            "welcome-package:custom",
        }
        options = menu.query_one(OptionList)
        assert options.get_option_at_index(options.highlighted).id == "welcome:basic"
        await select_menu(app, pilot, "Custom themes")
        assert app.screen is menu and menu.page_title == "CUSTOM THEMES"
        assert menu.page_revision > revision
        child_revision = menu.page_revision
        assert CUSTOM_PACKAGE.description in menu._description
        assert CUSTOM_PACKAGE.author in menu._description
        assert [choice.value for choice in menu._choices] == ["welcome:coral"]
        await pilot.press("left", "right")
        await select_menu(app, pilot, "Ember")
        assert app.screen is menu and menu.page_revision == child_revision
        assert isinstance(app.query_one(WelcomePanel).page, CustomPage)
        preferences = app._preferences_store.load()
        assert preferences.welcome_style == "coral"
        assert preferences.theme == "original" and preferences.theme_saturation_level == 0
        assert next(choice for choice in menu._choices if choice.label == "Ember").marker

        await pilot.press("escape")
        await wait_until(lambda: menu.page_title == "THEME", pilot, description="theme menu parent")
        assert menu.query_one(Input).value == "Custom themes"
        parent_choice = next(
            choice for choice in menu._choices if choice.value == "welcome-package:custom"
        )
        assert parent_choice.marker and parent_choice.trailing == "Ember"
        options = menu.query_one(OptionList)
        assert options.get_option_at_index(options.highlighted).id == "welcome-package:custom"
        revision = menu.page_revision
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
    app = OuroTuiApp(
        application=two_route_application(tmp_path), welcome_packages=(CUSTOM_PACKAGE,)
    )
    async with run_ready_app(app, size=(100, 40)) as pilot:
        composer = app.query_one(Composer)
        await pilot.press("d", "r", "a", "f", "t")
        await open_welcome_picker(app, pilot)
        menu = app.screen
        await select_menu(app, pilot, "Custom themes")
        await select_menu(app, pilot, "Custom scene")
        slot = app.query_one(WelcomePanel)
        assert isinstance(slot.page, CustomPage)
        assert not slot.page.active
        assert app.screen is menu
        await pilot.press("escape", "escape", "escape")
        await pilot.pause()
        assert slot.page.active
        assert composer.text == "draft"
        assert app.focused is composer
        assert app._preferences_store.load().welcome_style == CUSTOM.style_id
        assert app._preferences_store.load().theme == "original"

    restarted = OuroTuiApp(
        application=two_route_application(tmp_path), welcome_packages=(CUSTOM_PACKAGE,)
    )
    async with run_ready_app(restarted):
        assert isinstance(restarted.query_one(WelcomePanel).page, CustomPage)


async def test_package_menu_filters_unavailable_themes_without_constructing_pages(tmp_path):
    created = []

    def create():
        page = CustomPage()
        created.append(page)
        return page

    available = replace(CUSTOM, create=create)
    unavailable = replace(
        CUSTOM, style_id="unavailable-scene", check_requirements=lambda: "missing"
    )
    package = replace(CUSTOM_PACKAGE, themes=(available, unavailable))
    app = OuroTuiApp(application=two_route_application(tmp_path), welcome_packages=(package,))
    async with run_ready_app(app, size=(60, 18)) as pilot:
        await open_welcome_picker(app, pilot)
        menu = app.screen
        entry = next(choice for choice in menu._choices if choice.value == "welcome-package:custom")
        assert entry.trailing == "1 theme"
        await select_menu(app, pilot, "Custom themes")
        assert [choice.value for choice in menu._choices] == ["welcome:custom-scene"]
        assert not created
        await select_menu(app, pilot, "Custom scene")
        assert created == [app.query_one(WelcomePanel).page]


@pytest.mark.parametrize("reenter", [False, True])
async def test_theme_save_after_navigation_refreshes_the_current_page(
    tmp_path, monkeypatch, reenter
):
    app = OuroTuiApp(
        application=two_route_application(tmp_path), welcome_packages=(CUSTOM_PACKAGE,)
    )
    entered, release = threading.Event(), threading.Event()
    async with run_ready_app(app) as pilot:
        await open_welcome_picker(app, pilot)
        menu = app.screen
        await select_menu(app, pilot, "Custom themes")
        save = app._preferences_store.save

        def slow_save(preferences):
            entered.set()
            assert release.wait(10)
            save(preferences)

        monkeypatch.setattr(app._preferences_store, "save", slow_save)
        try:
            await pilot.press("enter")
            await wait_until(entered.is_set, pilot, description="theme persistence started")
            await pilot.press("escape")
            assert menu.page_title == "THEME"
            if reenter:
                await pilot.press("enter")
                await wait_until(
                    lambda: menu.page_title == "CUSTOM THEMES",
                    pilot,
                    description="package reopened during persistence",
                )
            expected_title = "CUSTOM THEMES" if reenter else "THEME"
            expected_choice = "welcome:custom-scene" if reenter else "welcome-package:custom"
            revision = menu.page_revision
            release.set()
            await wait_until(
                lambda: any(
                    choice.value == expected_choice and choice.marker for choice in menu._choices
                ),
                pilot,
                description="saved theme reflected in current menu page",
            )
            assert app.screen is menu and menu.page_title == expected_title
            assert menu.page_revision == revision
            assert menu.query_one(Input).value == ("" if reenter else "Custom themes")
            assert app.query_one(WelcomePanel).welcome_style.style_id == CUSTOM.style_id
        finally:
            release.set()


async def test_color_save_does_not_replace_an_open_package_page(tmp_path, monkeypatch):
    app = OuroTuiApp(
        application=two_route_application(tmp_path), welcome_packages=(CUSTOM_PACKAGE,)
    )
    entered, release = threading.Event(), threading.Event()
    async with run_ready_app(app) as pilot:
        await open_welcome_picker(app, pilot)
        menu = app.screen
        save = app._preferences_store.save

        def slow_save(preferences):
            entered.set()
            assert release.wait(10)
            save(preferences)

        monkeypatch.setattr(app._preferences_store, "save", slow_save)
        try:
            await select_menu(app, pilot, "Coral")
            await wait_until(entered.is_set, pilot, description="palette persistence started")
            await select_menu(app, pilot, "Custom themes")
            revision = menu.page_revision
            parent_refreshes = []
            theme_choices = app._theme_choices

            def record_parent_refresh(*args, **kwargs):
                parent_refreshes.append(True)
                return theme_choices(*args, **kwargs)

            monkeypatch.setattr(app, "_theme_choices", record_parent_refresh)
            release.set()
            await wait_until(
                lambda: not app._theme_switch_active, pilot, description="palette saved"
            )
            assert app.screen is menu and menu.page_title == "CUSTOM THEMES"
            assert menu.page_revision == revision
            assert [choice.value for choice in menu._choices] == ["welcome:custom-scene"]
            assert not parent_refreshes
            assert app._preferences_store.load().theme == "coral"
            assert app._preferences_store.load().welcome_style == "basic"
        finally:
            release.set()


@pytest.mark.parametrize("style_id", ["event-horizon", "quantum-veil"])
async def test_particle_theme_pauses_when_hidden_and_releases_worker_on_replacement(
    tmp_path: Path,
    style_id,
):
    TuiPreferencesStore(tmp_path).save(TuiPreferences(welcome_style=style_id))
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
            await wait_until(
                lambda: (
                    matrix._raster is not None
                    and (matrix._raster.columns, matrix._raster.rows)
                    == (matrix.size.width, matrix.size.height)
                ),
                pilot,
                description="particle raster matches the resized viewport",
            )
            assert matrix._raster is not None
            assert matrix._raster.columns == matrix.size.width
        await panel.set_welcome_style(WELCOME_STYLES["basic"])
        await pilot.pause()
        assert not any(worker.node is matrix for worker in app.workers)
        assert not list(panel.query(DotMatrix))


@pytest.mark.parametrize("style_id", ["event-horizon", "quantum-veil"])
@pytest.mark.parametrize("dimensions", [(1, 1), (10, 50), (50, 10), (80, 24), (160, 50), (300, 90)])
def test_particle_input_placement_stays_in_bounds_for_short_and_full_height_drafts(
    style_id, dimensions
):
    from textual.geometry import Size

    viewport = Size(*dimensions)
    page = WELCOME_STYLES[style_id].create()
    for height in (1, min(4, viewport.height), viewport.height):
        measured = []

        def measure(width, measured=measured, height=height):
            assert 1 <= width <= viewport.width
            measured.append(width)
            return height

        region = page.place_input(viewport, measure)
        assert region in viewport.region
        assert region.height == height
        assert measured == [region.width]


@pytest.mark.parametrize("style_id", ["event-horizon", "quantum-veil"])
async def test_particle_input_reuses_raster_and_workspace_follows_theme_lifecycle(
    tmp_path, monkeypatch, style_id
):
    from ouro.tui.widgets.status import StatusLine

    TuiPreferencesStore(tmp_path).save(TuiPreferences(welcome_style=style_id))
    app = OuroTuiApp(application=two_route_application(tmp_path))
    async with run_ready_app(app, size=(160, 50)) as pilot:
        panel, composer = app.query_one(WelcomePanel), app.query_one(Composer)
        page = panel.page
        await wait_until(lambda: page.matrix._raster is not None, pilot, description="first frame")
        raster = page.matrix._raster
        composer.focus()
        for text in ("draft", "a\nb\nc", "x" * 350, "restored", "/"):
            composer.load_text(text)
            await pilot.pause()
            assert page.matrix._raster is raster
            assert composer.text == text and app.focused is composer
            x, y = composer.region.x + 1, composer.region.y
            assert app.screen.get_widget_at(x, y)[0] is composer
        assert app.query_one(CommandPopup).display
        await pilot.press("escape")

        workspace, status_line = page._workspace, app.query_one(StatusLine)
        assert workspace.parent is status_line
        assert len(panel.query(".particle-workspace")) == 1
        host_children = [child for child in status_line.children if child is not workspace]
        updates = []
        update = workspace.update

        def record_update(content):
            updates.append(content)
            return update(content)

        monkeypatch.setattr(workspace, "update", record_update)
        panel.set_welcome_mode(False)
        await pilot.pause()
        assert not workspace.display
        status = TuiSystemStatus(workspace=str(tmp_path / "changed-workspace"))
        panel.set_system_status(status)
        panel.set_system_status(replace(status, usage_request_count=42))
        assert len(updates) == 1
        assert status.workspace in workspace.render().plain
        page.refresh_theme()
        assert len(updates) == 2
        panel.set_welcome_mode(True)
        await pilot.pause()
        assert workspace.display
        await panel.set_welcome_style(WELCOME_STYLES["basic"])
        await pilot.pause()
        assert not panel.query(".particle-workspace")
        assert list(status_line.children) == host_children


@pytest.mark.parametrize(
    ("style_id", "pointer"),
    [
        ("event-horizon", "pointer_x"),
        ("quantum-veil", "pointer_y"),
    ],
)
async def test_particle_mouse_target_is_coalesced_and_does_not_capture_input(
    tmp_path, style_id, pointer
):
    from textual import events

    TuiPreferencesStore(tmp_path).save(TuiPreferences(welcome_style=style_id))
    app = OuroTuiApp(application=two_route_application(tmp_path))
    async with run_ready_app(app, size=(160, 50)) as pilot:
        page = app.query_one(WelcomePanel).page
        matrix, composer = page.matrix, app.query_one(Composer)
        await wait_until(lambda: matrix._raster is not None, pilot, description="first frame")
        raster, region = matrix._raster, matrix.region
        vertical = pointer == "pointer_y"
        extent = matrix.size.height if vertical else matrix.size.width
        direction = -1 if vertical else 1

        def offset(value):
            return (5, value) if vertical else (value, 5)

        composer.focus()
        await pilot.hover(DotMatrix, offset=offset(0))
        assert getattr(page._scene, pointer) == -direction
        await pilot.hover(DotMatrix, offset=offset(extent - 1))
        assert getattr(page._scene, pointer) == direction
        assert app.focused is composer
        revision = matrix._render_revision
        for index in range(1000):
            x, y = offset(index % extent)
            matrix.on_mouse_move(events.MouseMove(matrix, x, y, 0, 0, 0, False, False, False))
        assert getattr(page._scene, pointer) == pytest.approx(
            direction * (2 * (999 % extent) / (extent - 1) - 1)
        )
        assert matrix._render_revision == revision
        assert matrix.region == region and matrix._raster is raster
        assert len([worker for worker in app.workers if worker.node is matrix]) == 1
        await pilot.hover(Composer)
        assert getattr(page._scene, pointer) == 0
        await pilot.click(DotMatrix, offset=offset(extent - 1))
        assert app.focused is composer
        await pilot.press("ctrl+p")
        assert not matrix.active and getattr(page._scene, pointer) == 0
        await pilot.press("escape")
        composer.load_text("a\nb\nc")
        await pilot.pause()
        await pilot.hover(Composer, offset=(2, 0))
        assert getattr(page._scene, pointer) == 0
        assert matrix.region == region and matrix._raster is raster


def test_normal_startup_keeps_saved_theme_worker_alive(tmp_path: Path, monkeypatch):
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

    class TestDriver(HeadlessDriver):
        @property
        def is_headless(self):
            return False

    class StartupApp(OuroTuiApp):
        def get_driver_class(self):
            return TestDriver

        def on_ready(self):
            self.set_timer(5, lambda: self.exit(False))
            self.set_interval(1 / 30, self.check_ready)

        def check_ready(self):
            if isinstance(self.screen, StartupScreen):
                return
            composer = self.query_one(Composer)
            if self.focused is not composer or composer.disabled:
                return
            matrix = self.query_one(DotMatrix)
            self.exit(any(worker.node is matrix and worker.is_running for worker in self.workers))

    TuiPreferencesStore(tmp_path).save(TuiPreferences(welcome_style="event-horizon"))
    app = StartupApp(application=two_route_application(tmp_path))
    # App.run uses production startup ordering; run_test can mask an early worker exit.
    assert app.run(size=(130, 31)), "the selected theme worker stopped before input was ready"


async def test_failed_style_save_keeps_current_package_and_preference(tmp_path, monkeypatch):
    app = OuroTuiApp(application=two_route_application(tmp_path))
    async with run_ready_app(app) as pilot:
        original = app.query_one(WelcomePanel).page
        await open_welcome_picker(app, pilot)

        def fail_save(_preferences: TuiPreferences):
            raise PersistenceError("style storage unavailable")

        monkeypatch.setattr(app._preferences_store, "save", fail_save)
        await select_menu(app, pilot, "Particle Art")
        await select_menu(app, pilot, "Quantum veil")
        assert app.query_one(WelcomePanel).page is original
        assert app._preferences.welcome_style == "basic"
        assert app._preferences_store.load().welcome_style == "basic"


@pytest.mark.parametrize(
    ("next_choice", "style_id", "theme_id"),
    [("Horizon", "event-horizon", "original"), ("Coral", "quantum-veil", "coral")],
)
async def test_style_transaction_survives_reselection_and_picker_close(
    tmp_path, monkeypatch, next_choice, style_id, theme_id
):
    app = OuroTuiApp(application=two_route_application(tmp_path))
    entered, release = threading.Event(), threading.Event()
    async with run_ready_app(app) as pilot:
        await open_welcome_picker(app, pilot)
        await select_menu(app, pilot, "Particle Art")
        save = app._preferences_store.save

        def slow_save(preferences):
            if preferences.welcome_style == "quantum-veil":
                entered.set()
                assert release.wait(5)
            save(preferences)

        monkeypatch.setattr(app._preferences_store, "save", slow_save)
        try:
            app.screen.query_one("#choice-filter", Input).value = "Quantum veil"
            await pilot.pause()
            await pilot.press("enter")
            assert await asyncio.to_thread(entered.wait, 2)
            if next_choice == "Coral":
                await pilot.press("escape")
            app.screen.query_one("#choice-filter", Input).value = next_choice
            await pilot.pause()
            await pilot.press("enter")
            await pilot.press(*(["escape"] * (2 if next_choice == "Coral" else 3)))
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
        assert not isinstance(app.screen, ChoiceScreen)
        assert app.query_one(WelcomePanel).welcome_style.style_id == style_id
        assert app.focused is app.query_one(Composer)

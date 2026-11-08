from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap
from importlib.metadata import PackageNotFoundError

import pytest

pytest.importorskip("ouro_tui_themes")

import ouro_tui_themes.tools.dot_matrix.requirements as theme_styles
from ouro_tui_themes.packs import PACKAGES

from ouro.core.errors import ConfigError
from ouro.tui.app import OuroTuiApp
from ouro.tui.composer.surface import WelcomePanel
from ouro.tui.preferences import TuiPreferences, TuiPreferencesStore
from ouro.tui.welcome import welcome_catalog
from ouro.tui.welcome_packages import load_theme_packages
from tests.tui.helpers import run_ready_app, two_route_application


def test_bundled_packages_integrate_with_the_existing_host_catalog():
    assert {
        package.package_id: tuple(theme.style_id for theme in package.themes)
        for package in PACKAGES
    } == {
        "particle-art": ("event-horizon", "quantum-veil"),
        "matrix": ("matrix",),
    }
    assert len({package.package_id for package in PACKAGES}) == len(PACKAGES)
    loaded, error = load_theme_packages()
    assert error is None
    catalog = welcome_catalog(loaded)  # Also rejects duplicate theme IDs across packages.
    assert len(catalog) == 1 + sum(len(package.themes) for package in PACKAGES)
    for package in PACKAGES:
        assert all(
            value.strip()
            for value in (package.package_id, package.name, package.description, package.author)
        )
        for theme in package.themes:
            assert catalog[theme.style_id] is theme
    particle, matrix = PACKAGES
    assert all(
        theme.check_requirements is theme_styles.check_requirements for theme in particle.themes
    )
    assert all(theme.check_requirements is None for theme in matrix.themes)


def test_style_metadata_does_not_load_numpy_or_renderers():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent("""
            import sys
            from ouro_tui_themes.packs import PACKAGES

            assert {style.style_id for package in PACKAGES for style in package.themes} == {
                "event-horizon", "quantum-veil", "matrix"
            }
            assert "numpy" not in sys.modules
            assert {name for name in sys.modules if name.startswith("ouro_tui_themes.tools.")} == {
                "ouro_tui_themes.tools.dot_matrix",
                "ouro_tui_themes.tools.dot_matrix.requirements",
            }
            assert {name for name in sys.modules if name.startswith("ouro_tui_themes.packs.")} == {
                "ouro_tui_themes.packs.particle_art", "ouro_tui_themes.packs.matrix"
            }
        """),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "selected", [style.style_id for package in PACKAGES for style in package.themes]
)
def test_page_factories_load_only_the_selected_renderer(selected):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent("""
            import sys
            from ouro_tui_themes.packs import PACKAGES

            selected = sys.argv[1]
            renderers = {
                "event-horizon": "ouro_tui_themes.packs.particle_art.event_horizon",
                "quantum-veil": "ouro_tui_themes.packs.particle_art.quantum_veil",
                "matrix": "ouro_tui_themes.packs.matrix.matrix",
            }
            blocked = {module for name, module in renderers.items() if name != selected}
            if selected == "matrix":
                blocked.update({"numpy", "ouro_tui_themes.tools.dot_matrix.field",
                                "ouro_tui_themes.tools.dot_matrix.raster",
                                "ouro_tui_themes.tools.dot_matrix.widget"})

            class RejectUnselected:
                def find_spec(self, fullname, path=None, target=None):
                    assert fullname not in blocked, fullname

            sys.meta_path.insert(0, RejectUnselected())
            theme = next(
                style for package in PACKAGES for style in package.themes
                if style.style_id == selected
            )
            first, second = theme.create(), theme.create()
            assert type(first).__module__ == renderers[selected]
            assert type(second) is type(first) and first is not second
            assert blocked.isdisjoint(sys.modules)
        """),
            selected,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_missing_theme_dependency_has_an_actionable_reason(monkeypatch):
    def missing(_name):
        raise PackageNotFoundError("numpy")

    monkeypatch.setattr(theme_styles, "version", missing)
    assert "ouro-agent[themes]" in theme_styles.check_requirements()


@pytest.mark.parametrize("installed", ["2.3.2", "3.0.0", "invalid"])
def test_incompatible_theme_dependency_is_not_imported(monkeypatch, installed):
    monkeypatch.setattr(theme_styles, "version", lambda _name: installed)

    def unexpected_import(_name):
        pytest.fail("incompatible extension must not be loaded")

    monkeypatch.setattr(theme_styles, "import_module", unexpected_import)
    assert "incompatible" in theme_styles.check_requirements()


@pytest.mark.parametrize("error", [ImportError("missing symbol"), OSError("invalid binary")])
def test_broken_extension_keeps_its_loading_diagnostic(monkeypatch, error):
    monkeypatch.setattr(theme_styles, "version", lambda _name: "2.3.3")

    def broken(_name):
        raise error

    monkeypatch.setattr(theme_styles, "import_module", broken)
    assert str(error) in theme_styles.check_requirements()


def test_dependency_programming_errors_are_not_hidden(monkeypatch):
    monkeypatch.setattr(theme_styles, "version", lambda _name: "2.3.3")

    def broken(_name):
        raise RuntimeError("unexpected extension bug")

    monkeypatch.setattr(theme_styles, "import_module", broken)
    with pytest.raises(RuntimeError, match="unexpected extension bug"):
        theme_styles.check_requirements()


@pytest.mark.parametrize("selected", ["basic", "event-horizon"])
async def test_missing_builtin_dependency_keeps_tui_usable_and_preferences_intact(
    tmp_path, monkeypatch, selected
):
    def missing(_name):
        raise PackageNotFoundError("numpy")

    monkeypatch.setattr(theme_styles, "version", missing)
    store = TuiPreferencesStore(tmp_path)
    store.save(TuiPreferences(welcome_style=selected))
    app = OuroTuiApp(application=two_route_application(tmp_path))
    notices = []
    monkeypatch.setattr(app, "notify", lambda message, **_kwargs: notices.append(message))
    async with run_ready_app(app):
        assert app._preferences_error is None
        assert app.query_one(WelcomePanel).welcome_style.style_id == "basic"
        assert [
            choice.value for choice in app._theme_choices() if choice.group == "Welcome style"
        ] == ["welcome:basic", "welcome-package:matrix"]
        if selected != "basic":
            assert any("Horizon is unavailable" in notice for notice in notices)
        else:
            assert not notices
        assert not await app._set_welcome_style("event-horizon")
        app._request_tui_theme("coral")
        workers = [worker for worker in app.workers if worker.group == "theme-switch"]
        await asyncio.gather(*(worker.wait() for worker in workers))
        assert store.load().welcome_style == selected
        assert store.load().theme == "coral"
    with pytest.raises(ConfigError, match="String should match pattern"):
        store.save(TuiPreferences.model_construct(welcome_style="../invalid-theme"))


def test_available_dependency_can_be_used_without_an_extra_activation_flag(monkeypatch):
    monkeypatch.setattr(theme_styles, "version", lambda _name: "2.3.3")
    imports = []
    monkeypatch.setattr(theme_styles, "import_module", lambda name: imports.append(name))
    assert theme_styles.check_requirements() is None
    assert imports == ["numpy"]

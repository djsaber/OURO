from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap
from importlib.metadata import PackageNotFoundError

import pytest

pytest.importorskip("ouro_tui_themes")

import ouro_tui_themes.dot_matrix.requirements as theme_styles

from ouro.core.errors import ConfigError
from ouro.tui.app import OuroTuiApp
from ouro.tui.composer.surface import WelcomePanel
from ouro.tui.preferences import TuiPreferences, TuiPreferencesStore
from tests.tui.helpers import run_ready_app, two_route_application


def test_style_metadata_does_not_load_numpy_or_renderers():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent("""
            import sys
            from ouro_tui_themes.styles import STYLES

            assert len(STYLES) == 5
            assert "numpy" not in sys.modules
            assert "ouro_tui_themes.dot_matrix.widget" not in sys.modules
            assert not any(name.startswith("ouro_tui_themes.styles.") for name in sys.modules)
        """),
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
        assert app.query_one(WelcomePanel).style_package.style_id == "basic"
        assert [
            choice.value for choice in app._theme_choices() if choice.value.startswith("welcome:")
        ] == ["welcome:basic"]
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

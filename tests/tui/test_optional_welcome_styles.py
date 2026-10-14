from __future__ import annotations

import subprocess
import sys
import textwrap
from dataclasses import replace
from importlib.metadata import PackageNotFoundError

import pytest

import ouro.tui.welcome.dot_matrix as dot_matrix
from ouro.core.errors import ConfigError
from ouro.tui.app import OuroTuiApp
from ouro.tui.preferences import TuiPreferences, TuiPreferencesStore
from ouro.tui.welcome import WELCOME_STYLES, unavailable_styles, welcome_catalog
from ouro.tui.widgets import WelcomePanel
from tests.tui.helpers import run_ready_app, two_route_application


def test_tui_import_and_basic_page_do_not_require_arrayfunc():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent("""
                import importlib.abc
                import sys

                class NoArrayfunc(importlib.abc.MetaPathFinder):
                    def find_spec(self, fullname, path=None, target=None):
                        if fullname == "arrayfunc" or fullname.startswith("arrayfunc."):
                            raise ModuleNotFoundError("arrayfunc is not installed", name=fullname)

                sys.meta_path.insert(0, NoArrayfunc())
                from ouro.tui.app import OuroTuiApp
                from ouro.tui.preferences import TuiPreferences
                from ouro.tui.welcome import DEFAULT_WELCOME_STYLE, WELCOME_STYLES

                assert DEFAULT_WELCOME_STYLE == "basic"
                assert TuiPreferences().welcome_style == "basic"
                assert WELCOME_STYLES["basic"].create() is not None
                assert "arrayfunc" not in sys.modules
                assert "ouro.tui.welcome.dot_matrix.widget" not in sys.modules
            """),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_missing_theme_dependency_has_an_actionable_reason(monkeypatch):
    def missing(_name):
        raise PackageNotFoundError("arrayfunc")

    monkeypatch.setattr(dot_matrix, "version", missing)
    assert "ouro-agent[themes]" in dot_matrix.check_requirements()


@pytest.mark.parametrize("installed", ["8.5.6", "9.0.0", "invalid"])
def test_incompatible_theme_dependency_is_not_imported(monkeypatch, installed):
    monkeypatch.setattr(dot_matrix, "version", lambda _name: installed)

    def unexpected_import(_name):
        pytest.fail("incompatible extension must not be loaded")

    monkeypatch.setattr(dot_matrix, "import_module", unexpected_import)
    assert "incompatible" in dot_matrix.check_requirements()


@pytest.mark.parametrize("error", [ImportError("missing symbol"), OSError("invalid binary")])
def test_broken_extension_keeps_its_loading_diagnostic(monkeypatch, error):
    monkeypatch.setattr(dot_matrix, "version", lambda _name: "8.5.7")

    def broken(_name):
        raise error

    monkeypatch.setattr(dot_matrix, "import_module", broken)
    assert str(error) in dot_matrix.check_requirements()


def test_dependency_programming_errors_are_not_hidden(monkeypatch):
    monkeypatch.setattr(dot_matrix, "version", lambda _name: "8.5.7")

    def broken(_name):
        raise RuntimeError("unexpected extension bug")

    monkeypatch.setattr(dot_matrix, "import_module", broken)
    with pytest.raises(RuntimeError, match="unexpected extension bug"):
        dot_matrix.check_requirements()


def test_shared_requirements_are_checked_once_and_do_not_change_known_identities():
    calls = []

    def missing():
        calls.append(True)
        return "optional renderer missing"

    first = replace(WELCOME_STYLES["basic"], style_id="first", check_requirements=missing)
    second = replace(first, style_id="second")
    styles = {style.style_id: style for style in (WELCOME_STYLES["basic"], first, second)}
    assert unavailable_styles(styles) == {
        "first": "optional renderer missing",
        "second": "optional renderer missing",
    }
    assert calls == [True]
    assert tuple(styles) == ("basic", "first", "second")


@pytest.mark.parametrize("selected", ["basic", "event-horizon"])
async def test_missing_builtin_dependency_keeps_tui_usable_and_preferences_intact(
    tmp_path, monkeypatch, selected
):
    def missing(_name):
        raise PackageNotFoundError("arrayfunc")

    monkeypatch.setattr(dot_matrix, "version", missing)
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
        assert await app._set_welcome_style("__ouro_welcome_motion__")
        assert store.load().welcome_style == selected
        assert store.load().welcome_motion is False
    with pytest.raises(ConfigError, match="unknown welcome style"):
        store.save(TuiPreferences.model_construct(welcome_style="misspelled-theme"))


async def test_restarting_after_dependency_install_restores_the_saved_theme(tmp_path):
    installed = False

    def check():
        return None if installed else "renderer missing"

    style = replace(WELCOME_STYLES["basic"], style_id="optional-scene", check_requirements=check)
    store = TuiPreferencesStore(tmp_path, welcome_styles=welcome_catalog((style,)))
    store.save(store.validate({"welcome_style": style.style_id}))
    for available in (False, True):
        installed = available
        app = OuroTuiApp(application=two_route_application(tmp_path), welcome_styles=(style,))
        async with run_ready_app(app):
            assert app.query_one(WelcomePanel).style_package.style_id == (
                style.style_id if available else "basic"
            )
            assert store.load().welcome_style == style.style_id


async def test_installing_theme_dependencies_does_not_change_the_default(tmp_path):
    app = OuroTuiApp(application=two_route_application(tmp_path))
    async with run_ready_app(app):
        assert app.query_one(WelcomePanel).style_package.style_id == "basic"
        assert app._preferences.welcome_style == "basic"


def test_available_dependency_can_be_used_without_an_extra_activation_flag(monkeypatch):
    monkeypatch.setattr(dot_matrix, "version", lambda _name: "8.5.7")
    imports = []
    monkeypatch.setattr(dot_matrix, "import_module", lambda name: imports.append(name))
    assert dot_matrix.check_requirements() is None
    assert imports == ["arrayfunc"]

from __future__ import annotations

import subprocess
import sys
import textwrap
from dataclasses import replace
from importlib.metadata import PackageNotFoundError
from types import SimpleNamespace

import pytest

import ouro.tui.welcome_packages as theme_packages
from ouro.tui.app import OuroTuiApp
from ouro.tui.preferences import TuiPreferencesStore
from ouro.tui.welcome import WELCOME_STYLES, unavailable_styles
from ouro.tui.widgets import WelcomePanel
from tests.tui.helpers import run_ready_app, two_route_application


@pytest.mark.parametrize("block_extension", [False, True])
def test_tui_import_and_basic_page_do_not_require_extensions(block_extension):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent(f"""
            import importlib.abc
            import sys

            blocked = {{"numpy", "ouro_tui_themes"}} if {block_extension!r} else {{"numpy"}}

            class NoExtensions(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if fullname.split(".")[0] in blocked:
                        raise ModuleNotFoundError("extension is not installed", name=fullname)

            sys.meta_path.insert(0, NoExtensions())
            from ouro.tui.app import OuroTuiApp
            from ouro.tui.preferences import TuiPreferences
            from ouro.tui.welcome import DEFAULT_WELCOME_STYLE, WELCOME_STYLES

            assert DEFAULT_WELCOME_STYLE == "basic"
            assert TuiPreferences().welcome_style == "basic"
            assert WELCOME_STYLES["basic"].create() is not None
            assert "numpy" not in sys.modules
            assert not any(name.startswith("ouro_tui_themes") for name in sys.modules)
        """),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_missing_distribution_does_not_import_extension_code(monkeypatch):
    def missing(_name):
        raise PackageNotFoundError("ouro-tui-themes")

    monkeypatch.setattr(theme_packages, "version", missing)
    monkeypatch.setattr(theme_packages, "import_module", lambda _: pytest.fail("unexpected import"))
    styles, reason = theme_packages.load_theme_styles()
    assert not styles
    assert "not installed" in reason and "ouro-agent[themes]" in reason


@pytest.mark.parametrize("installed", ["0.0.1", "99.0.0", "invalid"])
def test_incompatible_distribution_is_rejected_before_import(monkeypatch, installed):
    monkeypatch.setattr(theme_packages, "version", lambda _: installed)
    monkeypatch.setattr(theme_packages, "import_module", lambda _: pytest.fail("unexpected import"))
    styles, reason = theme_packages.load_theme_styles()
    assert not styles
    assert "incompatible" in reason


def test_matching_distribution_supplies_style_metadata(monkeypatch):
    styles = (replace(WELCOME_STYLES["basic"], style_id="installed-scene"),)
    monkeypatch.setattr(theme_packages, "version", lambda _: theme_packages.THEME_PACKAGE_VERSION)
    monkeypatch.setattr(theme_packages, "import_module", lambda _: SimpleNamespace(STYLES=styles))
    assert theme_packages.load_theme_styles() == (styles, None)


@pytest.mark.parametrize("missing", ["ouro_tui_themes", "ouro_tui_themes.styles", "internal_bug"])
def test_only_missing_package_modules_are_reported_as_unavailable(monkeypatch, missing):
    monkeypatch.setattr(theme_packages, "version", lambda _: theme_packages.THEME_PACKAGE_VERSION)

    def fail(_name):
        raise ModuleNotFoundError("missing module", name=missing)

    monkeypatch.setattr(theme_packages, "import_module", fail)
    if missing == "internal_bug":
        with pytest.raises(ModuleNotFoundError):
            theme_packages.load_theme_styles()
    else:
        styles, reason = theme_packages.load_theme_styles()
        assert not styles and "could not load" in reason


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


async def test_restarting_after_dependency_install_restores_the_saved_theme(tmp_path):
    installed = False

    def check():
        return None if installed else "renderer missing"

    style = replace(WELCOME_STYLES["basic"], style_id="optional-scene", check_requirements=check)
    store = TuiPreferencesStore(tmp_path)
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

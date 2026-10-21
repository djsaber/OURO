"""Smoke-test an installed wheel: python -I dev/check_distribution.py [--themes]."""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, distribution
from importlib.util import find_spec
from pathlib import Path
from tempfile import TemporaryDirectory

from packaging.requirements import Requirement

import ouro
from ouro.tui.app import OuroTuiApp
from ouro.tui.preferences import TuiPreferences, TuiPreferencesStore
from ouro.tui.widgets import WelcomePanel


async def check_tui(*, themes: bool) -> None:
    with TemporaryDirectory(prefix="ouro-install-") as home:
        store = TuiPreferencesStore(home)
        store.save(TuiPreferences(welcome_style="event-horizon"))
        app = OuroTuiApp(home=home)
        async with app.run_test(size=(100, 36)) as pilot:
            for worker in tuple(app.workers):
                if worker.name == "tui-start":
                    await worker.wait()
            await pilot.pause()
            assert app.startup_data_error is None
            assert app._preferences_error is None
            host = app.query_one(WelcomePanel)
            assert host.style_package.style_id == ("event-horizon" if themes else "basic")
            expected = (
                {
                    "basic",
                    "event-horizon",
                    "electron-bloom",
                    "interference",
                    "clifford-torus",
                    "quasicrystal",
                }
                if themes
                else {"basic"}
            )
            actual = {
                choice.value.removeprefix("welcome:")
                for choice in app._theme_choices()
                if choice.value.startswith("welcome:")
            }
            assert actual == expected, actual
            app._request_tui_theme("coral")
            workers = [worker for worker in app.workers if worker.group == "theme-switch"]
            await asyncio.gather(*(worker.wait() for worker in workers))
            assert store.load().welcome_style == "event-horizon"
            assert store.load().theme == "coral"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--themes", action="store_true")
    args = parser.parse_args()
    package = distribution("ouro-agent")
    assert ouro.__version__ == package.version
    installed_module = Path(ouro.__file__).resolve()
    assert installed_module == Path(package.locate_file("ouro/__init__.py")).resolve()
    source_package = Path(__file__).resolve().parent.parent / "ouro"
    assert not installed_module.is_relative_to(source_package)
    requirements = [Requirement(value) for value in package.requires or ()]
    optional = next(value for value in requirements if value.name == "ouro-tui-themes")
    assert not any(value.name == "numpy" for value in requirements)
    assert optional.marker is not None
    assert not optional.marker.evaluate({"extra": ""})
    assert optional.marker.evaluate({"extra": "themes"})
    try:
        extension = distribution("ouro-tui-themes")
    except PackageNotFoundError:
        assert not args.themes, "theme extension was not installed"
        assert find_spec("ouro_tui_themes") is None
        assert find_spec("numpy") is None
    else:
        assert args.themes, "base installation unexpectedly contains the theme extension"
        assert extension.version in optional.specifier
        files = {str(file).replace("\\", "/") for file in extension.files or ()}
        assert "ouro_tui_themes/dot_matrix/field.py" in files
        assert "ouro_tui_themes/styles/event_horizon.py" in files
        dependencies = [Requirement(value) for value in extension.requires or ()]
        numpy = next(value for value in dependencies if value.name == "numpy")
        assert distribution("numpy").version in numpy.specifier
        core = next(value for value in dependencies if value.name == "ouro-agent")
        assert package.version in core.specifier
    assert not any(
        "ouro/tui/welcome/styles/" in str(file).replace("\\", "/") for file in package.files or ()
    )
    for flag in ("--version", "--help"):
        result = subprocess.run(
            [sys.executable, "-I", "-m", "ouro.cli.main", flag],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert (ouro.__version__ if flag == "--version" else "usage:") in result.stdout
    asyncio.run(check_tui(themes=args.themes))
    print(f"OURO {package.version}: {'themes' if args.themes else 'base'} installation passed")


if __name__ == "__main__":
    main()

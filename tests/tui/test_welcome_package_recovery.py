import asyncio
from dataclasses import replace

from ouro.tui.app import OuroTuiApp
from ouro.tui.composer.surface import WelcomePanel
from ouro.tui.preferences import TuiPreferencesStore
from ouro.tui.welcome import WELCOME_STYLES
from tests.tui.helpers import run_ready_app, two_route_application


async def test_removed_package_keeps_preferences_and_restores_when_registered(
    tmp_path, monkeypatch
):
    style = replace(WELCOME_STYLES["basic"], style_id="external-scene")
    store = TuiPreferencesStore(tmp_path)
    # A valid saved identity can outlive the package that supplied it.
    store.path.parent.mkdir(parents=True)
    store.path.write_text(
        '{"welcome_style": "external-scene", "theme": "coral", "collapse_traces": true}',
        encoding="utf-8",
    )
    for registered in (False, True):
        app = OuroTuiApp(
            application=two_route_application(tmp_path),
            welcome_styles=(style,) if registered else (),
        )
        notices = []
        monkeypatch.setattr(app, "notify", lambda message, sink=notices, **_: sink.append(message))
        async with run_ready_app(app):
            assert app._preferences_error is None
            assert app._preferences.theme == ("indigo" if registered else "coral")
            assert app._preferences.collapse_traces
            assert app.query_one(WelcomePanel).style_package.style_id == (
                style.style_id if registered else "basic"
            )
            assert bool(notices) is not registered
            if not registered:
                assert "external-scene is unavailable" in notices[0]
            assert not await app._set_welcome_style("unknown-choice")
            app._request_tui_theme("indigo")
            workers = [worker for worker in app.workers if worker.group == "theme-switch"]
            await asyncio.gather(*(worker.wait() for worker in workers))
            assert store.load().welcome_style == style.style_id
            assert store.load().theme == "indigo"

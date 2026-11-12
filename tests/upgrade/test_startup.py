import asyncio
from pathlib import Path

import pytest

from ouro.core.errors import ConfigError, UnsupportedDataVersionError
from ouro.tui.app import OuroTuiApp
from ouro.tui.recovery import DataRecoveryScreen
from ouro_upgrade.engine import upgrade
from tests.tui.helpers import run_ready_app, two_route_application
from tests.upgrade.conftest import FIXTURES
from tests.upgrade.recovery_host import RecoveryHost
from tests.upgrade.test_recovery import wait_until


async def test_tui_opens_upgraded_history_without_executing_tasks(
    historical_home: Path, monkeypatch
) -> None:
    upgrade(historical_home)
    application = two_route_application(historical_home)
    # The historical fixture's task is disabled; the model catalog is entirely faux.
    app = OuroTuiApp(application=application, resume_session_id="ses_fixture")

    async def redundant_status_read():
        pytest.fail("Resumed Session already supplies its reasoning and approval state")

    monkeypatch.setattr(app._controller, "session_reasoning", redundant_status_read)
    monkeypatch.setattr(app._controller, "session_approval_preset", redundant_status_read)
    async with run_ready_app(app, size=(100, 32)):
        assert app._controller._started
        assert app._controller.session_id == "ses_fixture"
        assert application.tasks.repository.get("task_fixture").revision == 1


async def test_historical_startup_upgrades_and_opens_homepage_in_one_host(historical_home):
    app = OuroTuiApp(home=historical_home, resume_session_id="ses_fixture")
    async with run_ready_app(app, size=(100, 32)):
        assert app._recovery_screen is not None
        assert app._controller.session_id is None
        assert len(app.screen_stack) == 1
        assert list((historical_home / ".upgrade/receipts").glob("*.json"))


@pytest.mark.parametrize("resume", [False, True])
async def test_startup_defers_request_content_until_explicit_full_read(
    historical_home: Path, resume: bool
) -> None:
    upgrade(historical_home)
    context = next((historical_home / "sessions/ses_fixture/request-content").glob("*.json"))
    context.write_bytes((FIXTURES / "files/sessions/ses_fixture/context.json").read_bytes())
    app = OuroTuiApp(
        application=two_route_application(historical_home),
        resume_session_id="ses_fixture" if resume else None,
    )
    async with run_ready_app(app, size=(100, 32)):
        assert app._controller._started
    from ouro.sessions import LocalSessionStore

    with pytest.raises(UnsupportedDataVersionError):
        LocalSessionStore(historical_home).load_latest_context("ses_fixture")


async def test_bootstrap_failure_closes_started_services_before_return(
    tmp_path, monkeypatch
) -> None:
    application = two_route_application(tmp_path)
    app = OuroTuiApp(application=application)

    async def fail_usage():
        assert application._started
        raise UnsupportedDataVersionError("unsupported usage data")

    monkeypatch.setattr(app._controller, "usage_summary", fail_usage)
    async with app.run_test(size=(100, 32)):
        await wait_until(lambda: isinstance(app.screen, DataRecoveryScreen) and app.screen._busy)
    assert application._closed
    assert not app._controller._started
    assert app._controller._event_sink_registration is None


async def test_configuration_failure_does_not_trigger_data_recovery(tmp_path, monkeypatch) -> None:
    app = OuroTuiApp(home=tmp_path)

    async def fail_configuration():
        raise ConfigError("configure a model first")

    monkeypatch.setattr(app._controller, "start", fail_configuration)
    async with app.run_test(size=(100, 32)) as pilot:
        for worker in tuple(app.workers):
            if worker.name == "tui-start":
                await worker.wait()
        await pilot.pause()
        assert not app._exit


@pytest.mark.parametrize("auto_upgrade", [False, True])
@pytest.mark.parametrize("fail", [False, True])
async def test_external_shutdown_waits_for_offline_thread(
    tmp_path, monkeypatch, auto_upgrade, fail
) -> None:
    from threading import Event

    from ouro.tui import recovery

    finished = Event()
    release = Event()

    def operation(*_, **kwargs):
        kwargs["progress"](recovery.UpgradeProgress("publish", 0, 1))
        assert release.wait(5)
        finished.set()
        if fail:
            raise recovery.UpgradeError("interrupted data")
        return {"status": "upgraded"}

    monkeypatch.setattr(recovery, "upgrade", operation)
    app = RecoveryHost(home=tmp_path, error="old", auto_upgrade=auto_upgrade)
    try:
        async with app.run_test(size=(100, 32)) as pilot:
            if not auto_upgrade:
                await pilot.press("up", "enter")
            await wait_until(lambda: app.recovery._displayed_progress is not None)
            asyncio.get_running_loop().call_later(0.15, release.set)
            app.exit()
        assert finished.is_set()
    finally:
        release.set()


@pytest.mark.parametrize("quit_key", [None, "ctrl+c", "ctrl+q"])
async def test_recovery_waits_for_readers_and_keeps_surface_until_runtime_ready(
    tmp_path, monkeypatch, quit_key
):
    from threading import Event

    from ouro.tui.composer.input import Composer
    from ouro.tui.startup_screen import StartupScreen
    from ouro_upgrade.workspace import Workspace

    application = two_route_application(tmp_path)
    app = OuroTuiApp(application=application, resume_session_id="old")
    controller = app._controller
    reading, failed, release_read = asyncio.Event(), asyncio.Event(), asyncio.Event()
    rebuilding, release_build = Event(), Event()
    upgrades = []
    models = controller.models
    skills_read, tasks_read = asyncio.Event(), asyncio.Event()
    skills = application.runtime.available_skills
    tasks = controller.task_status

    async def read_skills():
        skills_read.set()
        return await skills()

    async def read_tasks():
        tasks_read.set()
        return await tasks()

    async def slow_models():
        reading.set()
        await release_read.wait()
        return await models()

    async def fail_usage():
        failed.set()
        raise UnsupportedDataVersionError("old usage")

    def upgrade(home, **options):
        assert home == tmp_path
        assert application._closed
        assert controller._event_sink_registration is None
        with Workspace(home, check_only=True):
            pass
        upgrades.append(home)
        return {"status": "upgraded"}

    def build(home, **options):
        assert home == tmp_path
        rebuilding.set()
        assert release_build.wait(10)
        return two_route_application(home)

    monkeypatch.setattr(controller, "models", slow_models)
    monkeypatch.setattr(application.runtime, "available_skills", read_skills)
    monkeypatch.setattr(controller, "task_status", read_tasks)
    monkeypatch.setattr(controller, "usage_summary", fail_usage)
    monkeypatch.setattr("ouro.tui.recovery.upgrade", upgrade)
    monkeypatch.setattr("ouro.tui.controller.LocalApplication.build", build)
    try:
        async with app.run_test(size=(100, 32)) as pilot:
            await asyncio.wait_for(
                asyncio.gather(
                    reading.wait(), failed.wait(), skills_read.wait(), tasks_read.wait()
                ),
                5,
            )
            assert isinstance(app.screen, StartupScreen)
            assert not upgrades and not application._closed
            release_read.set()
            assert await asyncio.to_thread(rebuilding.wait, 5)
            screen = app.screen
            assert isinstance(screen, DataRecoveryScreen)
            assert app.query_one(Composer).disabled
            await pilot.press("ctrl+n", "ctrl+o")
            assert app.screen is screen
            if quit_key is not None:
                from textual import events

                app.post_message(events.Key(quit_key, None))
                await wait_until(lambda: app._host_closing)
                assert app.screen is screen and not app._exit
                release_build.set()
                await wait_until(lambda: app._exit)
                assert app.return_value == 0
                return
            release_build.set()
            await wait_until(lambda: not app._starting)
            assert len(app.screen_stack) == 1
            assert app._controller.session_id is None
            assert app.focused is app.query_one(Composer)
            assert upgrades == [tmp_path]
    finally:
        release_read.set()
        release_build.set()


async def test_repeated_startup_failure_reuses_recovery_and_requires_retry(tmp_path, monkeypatch):
    upgrades = []
    builds = []

    def build(*_, **__):
        builds.append(True)
        raise UnsupportedDataVersionError("unsupported data")

    def upgrade(*_, **__):
        upgrades.append(True)
        return {"status": "current"}

    monkeypatch.setattr("ouro.tui.controller.LocalApplication.build", build)
    monkeypatch.setattr("ouro.tui.recovery.upgrade", upgrade)
    app = OuroTuiApp(home=tmp_path)
    async with app.run_test(size=(100, 32)) as pilot:
        await wait_until(
            lambda: (
                isinstance(app.screen, DataRecoveryScreen)
                and any(choice.value == "retry" for choice in app.screen._choices)
            )
        )
        screen = app.screen
        assert len(upgrades) == 1 and len(builds) == 2
        await pilot.press("up", "enter")
        await wait_until(lambda: len(builds) == 3 and screen.recovering and not screen._busy)
        assert app.screen is screen and len(app.screen_stack) == 2
        assert len(upgrades) == 2
        await pilot.press("ctrl+q")
    assert app.return_value == 1

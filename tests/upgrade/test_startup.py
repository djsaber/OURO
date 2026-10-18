import asyncio
from pathlib import Path

import pytest

from ouro.core.errors import ConfigError, UnsupportedDataVersionError
from ouro.tui.app import OuroTuiApp
from ouro_upgrade.engine import upgrade
from tests.tui.helpers import run_ready_app, two_route_application
from tests.upgrade.conftest import FIXTURES
from tests.upgrade.test_recovery import wait_until


async def test_tui_opens_upgraded_history_without_executing_tasks(historical_home: Path) -> None:
    upgrade(historical_home)
    application = two_route_application(historical_home)
    # The historical fixture's task is disabled; the model catalog is entirely faux.
    app = OuroTuiApp(application=application, resume_session_id="ses_fixture")
    async with run_ready_app(app, size=(100, 32)):
        assert app._controller._started
        assert app._controller.session_id == "ses_fixture"
        assert application.tasks.repository.get("task_fixture").revision == 1


async def test_historical_startup_returns_version_failure_without_opening_homepage(
    historical_home: Path,
) -> None:
    app = OuroTuiApp(home=historical_home)
    async with app.run_test(size=(100, 32)):
        await wait_until(lambda: app.startup_data_error is not None)
    assert isinstance(app.startup_data_error, UnsupportedDataVersionError)
    assert app.return_value == 1


@pytest.mark.parametrize("resume", [False, True])
async def test_homepage_does_not_read_historical_context_until_session_is_opened(
    historical_home: Path, resume: bool
) -> None:
    upgrade(historical_home)
    context = historical_home / "sessions/ses_fixture/context.json"
    context.write_bytes((FIXTURES / "files/sessions/ses_fixture/context.json").read_bytes())
    app = OuroTuiApp(
        application=two_route_application(historical_home),
        resume_session_id="ses_fixture" if resume else None,
    )
    if resume:
        async with app.run_test(size=(100, 32)):
            await wait_until(lambda: app.startup_data_error is not None)
        assert isinstance(app.startup_data_error, UnsupportedDataVersionError)
    else:
        async with run_ready_app(app, size=(100, 32)):
            assert app._controller._started
            assert app.startup_data_error is None


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
        await wait_until(lambda: app.startup_data_error is not None)
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
        assert app.startup_data_error is None
        assert not app._exit


async def test_external_shutdown_waits_for_offline_thread(tmp_path, monkeypatch) -> None:
    from threading import Event

    from ouro.tui import recovery

    finished = Event()
    release = Event()

    def operation(*_, **kwargs):
        kwargs["progress"](recovery.UpgradeProgress("publish", 0, 1))
        assert release.wait(5)
        finished.set()
        return {"status": "upgraded"}

    monkeypatch.setattr(recovery, "upgrade", operation)
    app = recovery.DataRecoveryApp(home=tmp_path, error="old", auto_upgrade=True)
    try:
        async with app.run_test(size=(100, 32)):
            await wait_until(lambda: app._displayed_progress is not None)
            asyncio.get_running_loop().call_later(0.15, release.set)
            app.exit()
        assert finished.is_set()
    finally:
        release.set()

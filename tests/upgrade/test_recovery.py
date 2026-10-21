from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest
from textual.widgets import OptionList, Static

from ouro.core.errors import PersistenceError, UnsupportedDataVersionError
from ouro.deployment import access_home, ensure_home_available
from ouro.tui import recovery
from ouro.tui.recovery import DataRecoveryApp
from ouro.tui.screens import ConfirmationScreen
from ouro_upgrade.engine import upgrade
from ouro_upgrade.errors import UpgradeError
from ouro_upgrade.progress import UpgradeProgress
from ouro_upgrade.workspace import Workspace
from tests.upgrade.test_isolation import clone_files
from tests.upgrade.test_upgrade import hashes


async def wait_until(predicate) -> None:
    async with asyncio.timeout(8):
        while not predicate():
            await asyncio.sleep(0.02)


@pytest.mark.parametrize("historical_home", ["files", "sqlite"], indirect=True)
async def test_recovery_automatically_upgrades_selected_backends(historical_home: Path) -> None:
    backend = "sqlite" if (historical_home / "sessions.sqlite3").exists() else "files"
    app = DataRecoveryApp(
        home=historical_home,
        error="old schema",
        auto_upgrade=True,
        session_backend=backend,
        task_backend=backend,
    )
    async with app.run_test(size=(100, 32)):
        await wait_until(lambda: app.return_value == "upgraded")
    assert upgrade(historical_home, session_backend=backend, task_backend=backend)["status"] == (
        "current"
    )
    assert list((historical_home / ".upgrade/receipts").glob("*.json"))


async def test_progress_animates_and_cannot_be_cancelled_mid_write(tmp_path, monkeypatch) -> None:
    release = Event()

    def slow_upgrade(home, *, progress, **_):
        progress(UpgradeProgress("publish", 2, 10))
        assert release.wait(8)
        return {"status": "upgraded"}

    monkeypatch.setattr(recovery, "upgrade", slow_upgrade)
    theme = replace(recovery.DEFAULT_TUI_THEME, spinner_frames=("◐", "◓", "◑", "◒"))
    app = DataRecoveryApp(home=tmp_path, error="old schema", auto_upgrade=True, theme=theme)
    try:
        async with app.run_test(size=(100, 32)) as pilot:
            await wait_until(lambda: app._displayed_progress is not None)
            assert app._menu._choices[0].label == "Writing upgraded files (2/10)"
            assert app._menu._choices[0].disabled
            assert app.tui_theme is theme
            assert app.tui_theme.spinner_frames == theme.spinner_frames
            frame = app._menu._spinner_frame
            await wait_until(lambda: app._menu._spinner_frame != frame)
            await pilot.press("escape", "ctrl+q", "enter")
            assert app._busy and app.return_value is None
            assert app.screen is app._menu
            release.set()
            await wait_until(lambda: app.return_value == "upgraded")
    finally:
        release.set()


async def test_unsupported_upgrade_preserves_data_and_offers_safe_default(
    historical_home,
) -> None:
    layout = historical_home / "tasks/layout.json"
    layout.write_text('{"schema_version":99}', encoding="utf-8")
    before = hashes(historical_home)
    app = DataRecoveryApp(home=historical_home, error="old schema", auto_upgrade=True)
    async with app.run_test(size=(100, 32)) as pilot:
        await wait_until(lambda: bool(app._failures) and not app._busy)
        assert not app._busy
        assert all(
            hashes(historical_home)[name] == value
            for name, value in before.items()
            if name.startswith("tasks/")
        )
        assert {choice.value for choice in app._menu._choices} == {"retry", "exit"}
        options = app.screen.query_one(OptionList)
        assert options.get_option_at_index(options.highlighted).id == "exit"
        assert (
            "unsupported" in str(app.screen.query_one("#recovery-status", Static).render()).lower()
        )
        await pilot.press("escape")
    assert hashes(historical_home) != before


async def test_discard_confirmation_defaults_to_cancel_and_preserves_successful_data(
    historical_home,
) -> None:
    home = historical_home
    bad = clone_files(home, "sessions/ses_fixture", "ses_bad")
    (bad / "context.json").write_bytes(b'{"schema_version":999}')
    (home / "plugins").mkdir()
    (home / "plugins/private.json").write_text("plugin data", encoding="utf-8")
    (home / "credentials.json").write_text("private credentials", encoding="utf-8")
    app = DataRecoveryApp(home=home, error="old schema", auto_upgrade=True)
    async with app.run_test(size=(100, 32)) as pilot:
        await wait_until(lambda: bool(app._failures) and not app._busy)
        successful = hashes(home)
        await pilot.press("up", "enter")
        await wait_until(lambda: isinstance(app.screen, ConfirmationScreen))
        assert "DELETE FAILED" in app.screen.page_title
        assert app.screen._choices[1].destructive
        assert "ses_bad" in str(app.screen.query_one("#recovery-status", Static).render())
        await pilot.press("enter")  # The confirmation defaults to Cancel.
        await wait_until(lambda: app.screen is app._menu)
        assert hashes(home) == successful
        await pilot.press("enter")
        await wait_until(lambda: isinstance(app.screen, ConfirmationScreen))
        await pilot.press("down", "enter")
        await wait_until(lambda: app.return_value == "upgraded")
    assert not bad.exists()
    after = hashes(home)
    assert all(
        after[name] == checksum
        for name, checksum in successful.items()
        if name != "sessions/index.json" and not name.startswith("sessions/ses_bad/")
    )
    assert ensure_home_available(home) == home


async def test_unknown_failure_never_offers_unscoped_deletion(tmp_path) -> None:
    app = DataRecoveryApp(home=tmp_path, error="unknown startup failure", auto_upgrade=False)
    async with app.run_test(size=(100, 32)) as pilot:
        assert {choice.value for choice in app._menu._choices} == {"retry", "exit"}
        await pilot.press("escape")


def test_progress_reports_phases_and_success_only_after_publish(historical_home) -> None:
    events = []
    report = upgrade(historical_home, progress=events.append)
    phases = list(dict.fromkeys(event.phase for event in events))
    assert phases[:4] == ["configuration", "sessions", "tasks", "usage"]
    assert phases[-1] == "complete"
    count = len(report["files"])
    for phase in ("prepare", "publish", "verify_source"):
        progress = [event for event in events if event.phase == phase]
        assert progress[0].completed == 0
        assert progress[-1] == UpgradeProgress(phase, count, count)
    for phase in ("sessions", "tasks", "verify_source"):
        progress = [event for event in events if event.phase == phase and event.total is not None]
        assert progress[0].completed == 0
        assert progress[-1].completed == progress[-1].total > 0


def test_standard_hosts_and_maintenance_exclude_each_other(tmp_path) -> None:
    with access_home(tmp_path), access_home(tmp_path):
        with pytest.raises(UpgradeError, match="another"):
            upgrade(tmp_path)
    with Workspace(tmp_path):
        with pytest.raises(PersistenceError, match="maintenance"):
            with access_home(tmp_path):
                pytest.fail("Host entered a home during maintenance")
    assert upgrade(tmp_path)["status"] == "current"


def test_entry_releases_host_before_upgrade_and_reloads_homepage(tmp_path, monkeypatch) -> None:
    from ouro.tui import app as app_module
    from ouro.tui import entry

    starts = []
    closed = False

    class Host:
        tui_theme = recovery.DEFAULT_TUI_THEME

        def __init__(self, **kwargs):
            starts.append(kwargs)
            self.startup_data_error = (
                UnsupportedDataVersionError("old") if len(starts) == 1 else None
            )

        def run(self):
            nonlocal closed
            with pytest.raises(UpgradeError, match="another"):
                with Workspace(tmp_path):
                    pass
            closed = True
            return 0

    class Recovery:
        def __init__(self, **kwargs):
            assert kwargs["auto_upgrade"]
            assert kwargs["session_backend"] == "sqlite"
            assert kwargs["task_backend"] == "files"
            assert kwargs["home"] == tmp_path

        def run(self):
            assert closed
            with Workspace(tmp_path):
                pass
            return "upgraded"

    monkeypatch.setattr(app_module, "OuroTuiApp", Host)
    monkeypatch.setattr(recovery, "DataRecoveryApp", Recovery)
    monkeypatch.setattr(entry, "set_terminal_title", lambda _: None)
    assert (
        entry.run_tui(
            home=tmp_path, session_backend="sqlite", task_backend="files", resume_session_id="old"
        )
        == 0
    )
    assert len(starts) == 2
    assert starts[1]["resume_session_id"] is None


def test_pending_marker_enters_recovery_before_main_app(tmp_path, monkeypatch) -> None:
    from ouro.cli.main import main
    from ouro.tui import app as app_module
    from ouro.tui import entry

    (tmp_path / ".upgrade").mkdir()
    (tmp_path / ".upgrade/in-progress.json").write_text(
        json.dumps({"operation": "reset"}), encoding="utf-8"
    )

    def forbidden(**_):
        pytest.fail("The runtime Host must not open data with a pending marker")

    class Recovery:
        def __init__(self, **kwargs):
            assert not kwargs["auto_upgrade"]

        def run(self):
            return None

    monkeypatch.setattr(app_module, "OuroTuiApp", forbidden)
    monkeypatch.setattr(recovery, "DataRecoveryApp", Recovery)
    monkeypatch.setattr(entry, "set_terminal_title", lambda _: None)
    assert main(["--home", str(tmp_path)]) == 1

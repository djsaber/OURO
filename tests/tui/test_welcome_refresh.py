from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from ouro.core.billing import UsageFilter
from ouro.core.messages import MessageRecord
from ouro.core.usage_queries import UsageSummary, UsageTotals
from ouro.tui.app import OuroTuiApp
from ouro.tui.composer.input import Composer
from ouro.tui.composer.surface import WelcomePanel
from ouro.tui.conversation.view import ConversationView
from tests.tui.app.support import _application
from tests.tui.helpers import run_ready_app


def application_with_session(tmp_path):
    application = _application(tmp_path)
    session = application.runtime.sessions.create(title="Selected").session
    application.runtime.store.append_messages(
        [MessageRecord.text(session_id=session.session_id, sequence=1, role="user", text="History")]
    )
    return application, session.session_id


async def wait_for_status_refresh(app):
    workers = [worker for worker in app.workers if worker.group == "welcome-status"]
    await asyncio.wait_for(app.workers.wait_for_complete(workers), 3)


async def test_welcome_is_visible_and_editable_before_status_read_finishes(tmp_path, monkeypatch):
    application, session_id = application_with_session(tmp_path)
    app = OuroTuiApp(application=application)
    entered, release = asyncio.Event(), asyncio.Event()
    async with run_ready_app(app) as pilot:
        await app._open_session(session_id)
        previous = app._system_status

        async def status():
            assert app.query_one(ConversationView).showing_welcome
            entered.set()
            await release.wait()
            return replace(previous, usage_request_count=11, skill_count=3)

        monkeypatch.setattr(app._controller, "system_status", status)
        try:
            await asyncio.wait_for(app._new_session(), 2)
            await asyncio.wait_for(entered.wait(), 2)
            assert app._controller.session_id is None
            assert app.query_one(WelcomePanel)._status == previous
            composer = app.query_one(Composer)
            assert not composer.disabled
            await pilot.press("d", "r", "a", "f", "t")
            assert composer.text == "draft"
        finally:
            release.set()
        await wait_for_status_refresh(app)
        assert app.query_one(WelcomePanel)._status.usage_request_count == 11
        assert app.query_one(WelcomePanel)._status.skill_count == 3
        assert composer.text == "draft"


@pytest.mark.parametrize("reenter", [False, True])
async def test_late_system_status_cannot_update_another_page_or_welcome_visit(
    tmp_path, monkeypatch, reenter
):
    application, session_id = application_with_session(tmp_path)
    app = OuroTuiApp(application=application)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0
    async with run_ready_app(app) as pilot:
        await app._open_session(session_id)

        async def status():
            nonlocal calls
            calls += 1
            captured = replace(app._system_status, usage_request_count=calls)
            if calls == 1:
                entered.set()
                await release.wait()
            return captured

        monkeypatch.setattr(app._controller, "system_status", status)
        try:
            await app._new_session()
            await asyncio.wait_for(entered.wait(), 2)
            await app._open_session(session_id)
            previous = app.query_one(WelcomePanel)._status
            if reenter:
                await app._new_session()
                await pilot.pause()
        finally:
            release.set()
        await wait_for_status_refresh(app)
        if reenter:
            assert calls == 2
            assert app.query_one(WelcomePanel)._status.usage_request_count == 2
        else:
            assert calls == 1
            assert app._controller.session_id == session_id
            assert app.query_one(WelcomePanel)._status == previous


async def test_full_refresh_and_completion_updates_share_one_reader(tmp_path, monkeypatch):
    app = OuroTuiApp(application=_application(tmp_path))
    entered, release = asyncio.Event(), asyncio.Event()
    usage_calls = 0
    async with run_ready_app(app):

        async def status():
            captured = replace(app._system_status, usage_request_count=1, skill_count=3)
            entered.set()
            await release.wait()
            return captured

        async def usage():
            nonlocal usage_calls
            usage_calls += 1
            return UsageSummary(usage_filter=UsageFilter(), totals=UsageTotals(request_count=9))

        monkeypatch.setattr(app._controller, "system_status", status)
        monkeypatch.setattr(app._controller, "usage_summary", usage)
        try:
            await app._new_session()
            await asyncio.wait_for(entered.wait(), 2)
            for _ in range(8):
                await app._refresh_welcome_status()
            assert usage_calls == 0
        finally:
            release.set()
        await wait_for_status_refresh(app)
        assert usage_calls == 1
        assert app.query_one(WelcomePanel)._status.usage_request_count == 9
        assert app.query_one(WelcomePanel)._status.skill_count == 3


async def test_workspace_change_during_refresh_keeps_current_selection(tmp_path, monkeypatch):
    workspace = tmp_path / "chosen"
    workspace.mkdir()
    app = OuroTuiApp(application=_application(tmp_path))
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0
    async with run_ready_app(app):

        async def status():
            nonlocal calls
            calls += 1
            captured = replace(app._system_status, skill_count=3)
            if calls == 1:
                entered.set()
                await release.wait()
            return captured

        monkeypatch.setattr(app._controller, "system_status", status)
        try:
            await app._new_session()
            await asyncio.wait_for(entered.wait(), 2)
            await app._select_workspace(str(workspace))
        finally:
            release.set()
        await wait_for_status_refresh(app)
        assert calls == 2
        assert app.query_one(WelcomePanel)._status.workspace == str(workspace.resolve())
        assert app.query_one(WelcomePanel)._status.skill_count == 3


async def test_failed_background_status_read_preserves_welcome_and_last_values(
    tmp_path, monkeypatch
):
    app = OuroTuiApp(application=_application(tmp_path))
    notifications = []
    async with run_ready_app(app) as pilot:
        previous = app._system_status

        async def status():
            raise OSError("temporary read failure")

        monkeypatch.setattr(app._controller, "system_status", status)
        monkeypatch.setattr(app, "notify", lambda message, **kwargs: notifications.append(message))
        await app._new_session()
        await pilot.pause()
        await wait_for_status_refresh(app)
        assert app.query_one(ConversationView).showing_welcome
        assert app.query_one(WelcomePanel)._status == previous
        assert not app.query_one(Composer).disabled
        assert notifications == ["Could not refresh system status: temporary read failure"]

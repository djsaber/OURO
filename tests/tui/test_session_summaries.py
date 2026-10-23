import asyncio

import pytest

from ouro.core.errors import PersistenceError
from ouro.tui.app import OuroTuiApp
from ouro.tui.commands import matching_commands, resolve_command
from ouro.tui.composer.input import Composer
from ouro.tui.conversation.history import ConversationHistory
from tests.session_summaries import add_session_summaries
from tests.tui.helpers import run_ready_app, two_route_application


async def wait_for_summary(app):
    for worker in tuple(app.workers):
        if worker.group == "session-summary":
            await worker.wait()


def test_summary_command_is_discoverable_without_arguments():
    assert [item.name for item in matching_commands("/summ")] == ["/summary"]
    command, argument = resolve_command("/summary")
    assert command is not None
    assert command.usage == "/summary"
    assert argument == ""


@pytest.mark.parametrize("active_index", [0, 1])
async def test_summary_command_shows_only_latest_summary_without_mutating_sessions(
    tmp_path, active_index
):
    application = two_route_application(tmp_path)
    runtime = application.runtime
    target = runtime.sessions.create(title="Current", current_model="deleted/model").session
    runtime.sessions.create(title="Other")
    records = add_session_summaries(runtime.store, target.session_id)
    runtime.store.activate_compaction(target.session_id, records[active_index].compaction_id)
    before = runtime.store.load_session(target.session_id)
    app = OuroTuiApp(application=application, resume_session_id=target.session_id)

    async with run_ready_app(app, size=(100, 32)) as pilot:
        composer = app.query_one(Composer)
        composer.load_command("/summary")
        await pilot.pause()
        await pilot.press("enter")
        await wait_for_summary(app)
        history = app.query_one(ConversationHistory)
        outputs = [entry for entry in history.entries if entry.kind == "command"]
        assert len(outputs) == 1
        output = outputs[0].text
        assert output.startswith("Latest session summary\n")
        assert target.session_id not in output
        assert records[0].summary not in output
        assert records[0].compaction_id not in output
        assert records[1].summary in output
        state = "active" if active_index == 1 else "inactive"
        assert f"{records[1].compaction_id} [{state}]" in output
        assert "第二次摘要" in history.rendered_text
        assert app._controller.session_id == target.session_id
        assert app._visible_session_id() == target.session_id
        assert runtime.store.load_session(target.session_id) == before
        assert not any(
            entry.kind == "user" and "/summary" in entry.text for entry in history.entries
        )


async def test_summary_query_from_welcome_does_not_select_or_create_a_session(
    tmp_path, monkeypatch
):
    application = two_route_application(tmp_path)
    target = application.runtime.sessions.create(title="Saved").session
    add_session_summaries(application.runtime.store, target.session_id)
    app = OuroTuiApp(application=application)
    async with run_ready_app(app):
        notices = []
        monkeypatch.setattr(app, "notify", lambda message, **kwargs: notices.append(str(message)))
        await app._dispatch_command("/summary")
        await wait_for_summary(app)
        assert notices == ["Open a session to view summaries"]
        assert app._controller.session_id is None
        assert app._projection is None
        assert len(application.runtime.store.list_sessions()) == 1
        assert not any(
            entry.kind == "command" for entry in app.query_one(ConversationHistory).entries
        )


async def test_summary_command_reports_empty_history_and_input_errors(tmp_path, monkeypatch):
    application = two_route_application(tmp_path)
    session = application.runtime.sessions.create().session
    other = application.runtime.sessions.create(title="Other").session
    app = OuroTuiApp(application=application, resume_session_id=session.session_id)
    async with run_ready_app(app):
        notices = []
        monkeypatch.setattr(app, "notify", lambda message, **kwargs: notices.append(str(message)))
        for argument in [other.session_id, "ses_one ses_two"]:
            await app._dispatch_command(f"/summary {argument}")
            await wait_for_summary(app)
            assert notices.pop() == "Usage: /summary"
        assert not any(
            entry.kind in {"command", "notice"}
            for entry in app.query_one(ConversationHistory).entries
        )
        await app._dispatch_command("/summary")
        await wait_for_summary(app)
        entry = app.query_one(ConversationHistory).entries[-1]
        assert entry.kind == "notice"
        assert entry.text == "No summaries for the current session."
        assert app._controller.session_id == session.session_id

        def unreadable(_session_id):
            raise PersistenceError(f"invalid compaction record for {session.session_id}")

        monkeypatch.setattr(application.runtime.store, "load_compactions", unreadable)
        await app._dispatch_command("/summary")
        await wait_for_summary(app)
        assert notices.pop() == "Could not load summaries for the current session"
        assert app.query_one(ConversationHistory).entries[-1] == entry


async def test_summary_query_does_not_publish_into_a_different_session(tmp_path, monkeypatch):
    application = two_route_application(tmp_path)
    first = application.runtime.sessions.create(title="First").session
    second = application.runtime.sessions.create(title="Second").session
    app = OuroTuiApp(application=application, resume_session_id=first.session_id)
    async with run_ready_app(app):
        entered, release = asyncio.Event(), asyncio.Event()
        query = app._controller.session_summaries

        async def delayed_query(session_id):
            entered.set()
            await release.wait()
            return await query(session_id)

        monkeypatch.setattr(app._controller, "session_summaries", delayed_query)
        await app._dispatch_command("/summary")
        await asyncio.wait_for(entered.wait(), 5)
        try:
            await app._open_session(second.session_id)
        finally:
            release.set()
        await wait_for_summary(app)
        assert app._controller.session_id == second.session_id
        assert not any(
            entry.kind in {"command", "notice"}
            for entry in app.query_one(ConversationHistory).entries
        )

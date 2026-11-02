from __future__ import annotations

from unittest.mock import Mock

import pytest

from ouro.core.base import new_id
from ouro.tui.app import OuroTuiApp
from ouro.tui.controller import TuiController
from ouro.tui.widgets.brand import BrandHeader
from tests.application.test_attachments import PNG
from tests.tui.app.providers import BlockingStreamProvider
from tests.tui.helpers import release_on_exit, run_ready_app, wait_for_events, wait_until
from tests.tui.test_history_commands import application_at
from tests.tui.test_tasks import streaming_application


@pytest.mark.parametrize("backend", ["files", "sqlite"])
@pytest.mark.parametrize("source", ["upload", "reference", "removed"])
async def test_first_text_titles_prepared_and_new_sessions(tmp_path, backend, source):
    application = application_at(tmp_path / "home", backend=backend)
    controller = TuiController(lambda _event: None, lambda _status: None, application=application)
    path = tmp_path / "picture.png"
    path.write_bytes(PNG)
    await controller.start()
    try:
        identity = new_id("ses")
        if source == "reference":
            attachments = await controller.reference_files((path,))
        else:
            target = await controller.attachment_target(None, identity)
            attachment = await controller.import_attachment(target, path)
            attachments = (attachment,) if source == "upload" else ()
            if source == "removed":
                await controller.release_input_resources(identity, ())
        assert controller.session_id is None
        assert application.runtime.store.list_sessions() == []
        first = await controller.submit(
            "  Describe\n this picture  ", attachments=attachments, new_session_id=identity
        )
        assert first.run.status == "completed"
        assert first.session.session_id == identity
        assert first.session.title == "Describe this picture"
        assert first.session.title_source == "fallback"
        second = await controller.submit("Another subject")
        assert second.session.title == "Describe this picture"
        stored = application.runtime.store.load_session_record(first.session.session_id)
        entry = application.runtime.sessions.catalog().entries[0]
        assert stored.title == entry.title == first.session.title
        assert stored.title_source == entry.title_source == "fallback"
    finally:
        await controller.close()
        await application.close()


@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_visible_title_tracks_commits_while_model_is_running(tmp_path, backend, monkeypatch):
    provider = BlockingStreamProvider()
    application = streaming_application(tmp_path, provider, backend=backend)
    app = OuroTuiApp(application=application)
    async with run_ready_app(app) as pilot, release_on_exit(provider.release):
        session = application.runtime.sessions.create().session
        await app._open_session(session.session_id)
        header = app.query_one(BrandHeader)
        assert header._session_title == "New conversation"
        update_header = Mock(wraps=header.set_context)
        monkeypatch.setattr(header, "set_context", update_header)
        projection = app._projection
        subscription = app._observation_subscription
        handle = await application.supervisor.submit(
            "First meaningful input", session_id=session.session_id
        )
        try:
            await wait_for_events(
                provider.first_chunk_sent, pilot=pilot, description="model stream blocked"
            )
            await wait_until(
                lambda: header._session_title == app._active_title == "First meaningful input",
                pilot,
                description="live session title",
            )
            assert not provider.release.is_set()
            assert app._projection is projection
            assert app._observation_subscription is subscription
        finally:
            provider.release.set()
            await application.supervisor.wait(handle)
        await wait_until(
            lambda: app._projection.activity.status == "completed",
            pilot,
            description="completed model stream",
        )
        assert update_header.call_count == 1
        application.runtime.sessions.rename(session.session_id, "Renamed after completion")
        await wait_until(
            lambda: header._session_title == app._active_title == "Renamed after completion",
            pilot,
            description="metadata-only title update",
        )
        assert app._projection is projection
        assert app._observation_subscription is subscription
        assert update_header.call_count == 2


@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_attachment_only_session_keeps_placeholder_until_text_arrives(tmp_path, backend):
    application = application_at(tmp_path / "home", backend=backend)
    controller = TuiController(lambda _event: None, lambda _status: None, application=application)
    path = tmp_path / "picture.png"
    path.write_bytes(PNG)
    await controller.start()
    try:
        identity = new_id("ses")
        target = await controller.attachment_target(None, identity)
        attachment = await controller.import_attachment(target, path)
        assert application.runtime.store.list_sessions() == []
        first = await controller.submit("", attachments=(attachment,), new_session_id=identity)
        assert first.run.status == "completed"
        assert first.session.session_id == identity
        assert first.session.title == "New conversation"
        assert first.session.title_source == "placeholder"
        second = await controller.submit("Describe the picture")
        assert second.session.title == "Describe the picture"
        assert second.session.title_source == "fallback"
    finally:
        await controller.close()
        await application.close()

from __future__ import annotations

import pytest

from ouro.tui.controller import TuiController
from tests.application.test_attachments import PNG
from tests.tui.test_history_commands import application_at


@pytest.mark.parametrize("backend", ["files", "sqlite"])
@pytest.mark.parametrize("source", ["upload", "reference", "removed"])
async def test_first_text_titles_prepared_and_new_sessions(tmp_path, backend, source):
    application = application_at(tmp_path / "home", backend=backend)
    controller = TuiController(lambda _event: None, lambda _status: None, application=application)
    path = tmp_path / "picture.png"
    path.write_bytes(PNG)
    await controller.start()
    try:
        if source == "reference":
            attachments = await controller.reference_files((path,))
        else:
            session = await controller.attachment_session()
            attachment = await controller.import_attachment(session, path)
            attachments = (attachment,) if source == "upload" else ()
        first = await controller.submit("  Describe\n this picture  ", attachments=attachments)
        assert first.run.status == "completed"
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
async def test_attachment_only_session_keeps_placeholder_until_text_arrives(tmp_path, backend):
    application = application_at(tmp_path / "home", backend=backend)
    controller = TuiController(lambda _event: None, lambda _status: None, application=application)
    path = tmp_path / "picture.png"
    path.write_bytes(PNG)
    await controller.start()
    try:
        session = await controller.attachment_session()
        attachment = await controller.import_attachment(session, path)
        first = await controller.submit("", attachments=(attachment,))
        assert first.session.title == "New conversation"
        assert first.session.title_source == "placeholder"
        second = await controller.submit("Describe the picture")
        assert second.session.title == "Describe the picture"
        assert second.session.title_source == "fallback"
    finally:
        await controller.close()
        await application.close()

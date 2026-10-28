import pytest

from ouro.application.context.prompts import PromptContext
from ouro.application.runtime.service import RuntimeService
from ouro.core.adapter import RuntimeAdapter
from ouro.core.compaction import CompactionRecord
from ouro.core.content import ImageContent
from ouro.core.context_selection import ContextSelection, MessageBoundary
from ouro.core.messages import MessageRecord
from ouro.core.provider import ModelMetadata
from ouro.core.session_commits import SessionCommitConflict
from ouro.core.state import SessionRecord
from ouro.providers import FauxProvider
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.tools import ToolRegistry
from tests.application.test_media_resource_ownership import observe
from tests.application.test_resources import _services
from tests.session_summaries import commit_summary


@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_projection_uses_selected_tail_without_old_prefix_or_last_request(
    tmp_path, monkeypatch, backend
):
    store = (
        LocalSessionStore(tmp_path)
        if backend == "files"
        else SQLiteSessionStore(tmp_path / "sessions.db")
    )
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    messages = [
        MessageRecord.text(session_id=session.session_id, role="user", text=str(i))
        for i in range(4)
    ]
    messages[2].content.append(
        ImageContent(uri="data:image/png;base64,aW1hZ2U=", media_type="image/png")
    )
    store.append_messages(messages)
    summary = CompactionRecord(
        session_id=session.session_id,
        strategy_id="test",
        trigger="manual",
        summary="old summary",
        covered_message_count=2,
        covered_end_message_id=messages[1].message_id,
        history_boundary_message_id=messages[-1].message_id,
    )
    commit_summary(store, summary)
    session = store.load_session_record(session.session_id)
    session = session.model_copy(
        update={
            "context_selection": session.context_selection.model_copy(
                update={
                    "media_elided_through": MessageBoundary(
                        message_id=messages[2].message_id, sequence=3
                    ),
                }
            )
        }
    )
    store.save_session(session)

    def unavailable(*args, **kwargs):
        pytest.fail("projection read old sources or the previous request")

    monkeypatch.setattr(store, "load_message", unavailable)
    monkeypatch.setattr(store, "load_latest_context_header", unavailable)
    monkeypatch.setattr(store, "load_latest_context", unavailable)
    runtime = RuntimeService(
        store=store,
        tools=ToolRegistry(),
        adapter=RuntimeAdapter(
            FauxProvider(),
            ModelMetadata(model_id="test", provider_id="test", supports_image_input=True),
        ),
    )
    try:
        view = runtime.context_preparation._project_context(
            session=session,
            messages=messages[2:],
            active=summary,
            tools=[],
            added_context=[],
            adapter=runtime.adapter,
            skill_instructions="",
            prompt_context=PromptContext(""),
        )
        assert view.selection == session.context_selection
        assert "old summary" in view.provider_context.messages[0].text_content
        assert "Historical image" in str(view.provider_context.messages)
        assert not any(
            isinstance(part, ImageContent)
            for m in view.provider_context.messages
            for part in m.content
        )
    finally:
        await runtime.close()


@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_reselect_earlier_summary_then_original_media_without_rewriting_last_request(
    tmp_path, backend, monkeypatch
):
    services = _services(tmp_path / "home", backend=backend)
    store, _, resources, sessions = services
    session, media_message, _ = observe(services, tmp_path, "reference", 4096)
    messages = [media_message]
    for index in range(1, 6):
        message = MessageRecord.text(
            session_id=session.session_id,
            role="assistant" if index % 2 else "user",
            text=f"Retained history {index}",
        )
        store.append_message(message)
        messages.append(message)
    summaries = []
    for covered in (2, 4):
        summary = CompactionRecord(
            session_id=session.session_id,
            previous_compaction_id=summaries[-1].compaction_id if summaries else None,
            strategy_id="test",
            trigger="manual",
            summary=f"Summary through {covered}",
            covered_message_count=covered,
            covered_end_message_id=messages[covered - 1].message_id,
            history_boundary_message_id=messages[-1].message_id,
        )
        commit_summary(store, summary)
        summaries.append(summary)
    selection = ContextSelection(
        compaction_id=summaries[-1].compaction_id,
        media_elided_through=MessageBoundary(message_id=messages[1].message_id, sequence=2),
    )
    sessions.set_context_selection(session.session_id, selection)
    runtime = RuntimeService(
        store=store,
        resources=resources,
        tools=ToolRegistry(),
        adapter=RuntimeAdapter(
            FauxProvider(),
            ModelMetadata(model_id="test", provider_id="test", supports_image_input=True),
        ),
    )
    runtime.compaction_config.policy.auto_enabled = False

    def no_full_history(*args, **kwargs):
        pytest.fail("selection expanded a full Session or all summaries")

    monkeypatch.setattr(store, "load_session", no_full_history)
    monkeypatch.setattr(store, "load_compactions", no_full_history)
    try:
        assert (
            await runtime.run("Use newest summary", session_id=session.session_id)
        ).run.status == ("completed")
        latest = store.load_latest_context(session.session_id)
        assert "Summary through 4" in str(latest.provider_context.messages)
        assert "Retained history 2" not in str(latest.provider_context.messages)
        earlier = selection.model_copy(update={"compaction_id": summaries[0].compaction_id})
        sessions.set_context_selection(session.session_id, earlier)
        assert store.load_latest_context(session.session_id) == latest
        assert (
            await runtime.run("Use earlier summary", session_id=session.session_id)
        ).run.status == ("completed")
        latest = store.load_latest_context(session.session_id)
        assert "Summary through 2" in str(latest.provider_context.messages)
        assert "Retained history 2" in str(latest.provider_context.messages)
        with pytest.raises(SessionCommitConflict, match="selected compaction"):
            sessions.set_context_selection(
                session.session_id, ContextSelection(compaction_id="cmp_missing")
            )
        assert store.load_session_record(session.session_id).context_selection == earlier
        sessions.set_context_selection(session.session_id, ContextSelection())
        assert store.load_latest_context(session.session_id) == latest
        assert (
            await runtime.run("Read original media", session_id=session.session_id)
        ).run.status == ("completed")
        selected = store.load_latest_context(session.session_id).provider_context.messages
        assert "Summary through" not in str(selected)
        assert [
            part
            for message in selected
            for part in message.content
            if isinstance(part, ImageContent)
        ] == [part for part in media_message.content if isinstance(part, ImageContent)]
        assert store.load_message(session.session_id, media_message.message_id).content == (
            media_message.content
        )
    finally:
        await runtime.close()

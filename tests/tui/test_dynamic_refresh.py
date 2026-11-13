import asyncio

from ouro.application.contracts.agents import AgentInteractionView
from ouro.application.contracts.execution import RuntimeToolCallActivity
from ouro.application.observations.content import SessionContentProjection
from ouro.core.events import EventRecord
from ouro.providers import FauxProvider
from ouro.tui.app import OuroTuiApp
from ouro.tui.conversation.view import ConversationView
from tests.tui.helpers import release_on_exit, run_ready_app
from tests.tui.test_tasks import streaming_application


async def test_interaction_read_cannot_clear_a_new_primary_approval(tmp_path, monkeypatch):
    app = OuroTuiApp(application=streaming_application(tmp_path, FauxProvider()))
    entered, release = asyncio.Event(), asyncio.Event()

    async def read():
        entered.set()
        await release.wait()
        return ()

    async with run_ready_app(app), release_on_exit(release):
        projection = SessionContentProjection("session")
        app._projection = projection
        monkeypatch.setattr(app._controller, "agent_interactions", read)
        await app._refresh_interaction(refresh_agents=True)
        await asyncio.wait_for(entered.wait(), 5)
        projection.apply(
            EventRecord(
                session_id="session",
                run_id="run",
                sequence=1,
                event_type="approval_requested",
                payload={"tool_call_id": "call", "tool_name": "Read", "approval_id": "approval"},
            )
        )
        await app._refresh_interaction()
        assert app._interaction == projection.pending_interaction
        task = app._interaction_refresh_task
        release.set()
        await task
        assert app._interaction == projection.pending_interaction


async def test_text_frames_reuse_interactions_and_notifications_coalesce(tmp_path, monkeypatch):
    app = OuroTuiApp(application=streaming_application(tmp_path, FauxProvider()))
    entered, release = asyncio.Event(), asyncio.Event()
    reads = 0
    interaction = AgentInteractionView(
        kind="approval",
        delegation_id="delegation",
        agent_id="agent",
        agent_name="Agent",
        task_description="Task",
        child_session_id="child",
        child_run_id="child_run",
        tool_call_id="call",
        tool_name="Read",
        request_id="approval",
        requested_at="now",
    )

    async def read():
        nonlocal reads
        reads += 1
        entered.set()
        await release.wait()
        return (interaction,)

    async with run_ready_app(app), release_on_exit(release):
        app._projection = SessionContentProjection("session")
        monkeypatch.setattr(app._controller, "agent_interactions", read)
        await app._refresh_interaction(refresh_agents=True)
        await asyncio.wait_for(entered.wait(), 5)
        for _ in range(20):
            app._request_agent_interactions()
        task = app._interaction_refresh_task
        release.set()
        await task
        assert reads == 2
        assert app._interaction.is_subagent
        for sequence in range(1, 6):
            await app._apply_runtime_events(
                (
                    EventRecord(
                        session_id="session",
                        run_id="run",
                        sequence=sequence,
                        event_type="model_text_delta",
                        payload={"text": "next "},
                    ),
                )
            )
        assert reads == 2
        assert app._interaction.request_id == "approval"


async def test_interaction_read_is_discarded_after_selection_changes(tmp_path, monkeypatch):
    app = OuroTuiApp(application=streaming_application(tmp_path, FauxProvider()))
    entered, release = asyncio.Event(), asyncio.Event()

    async def read():
        entered.set()
        await release.wait()
        return ()

    async with run_ready_app(app), release_on_exit(release):
        app._projection = SessionContentProjection("old")
        monkeypatch.setattr(app._controller, "agent_interactions", read)
        app._request_agent_interactions()
        await asyncio.wait_for(entered.wait(), 5)
        cached_projection = app._agent_interactions_projection
        current = SessionContentProjection("new")
        app._projection = current
        task = app._interaction_refresh_task
        release.set()
        await task
        assert app._agent_interactions_projection is cached_projection


async def test_tool_animation_reuses_static_content(tmp_path, monkeypatch):
    import ouro.tui.conversation.tools as tools

    app = OuroTuiApp(application=streaming_application(tmp_path, FauxProvider()))
    calls = 0
    original = tools.present_tool

    def present(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    async with run_ready_app(app):
        monkeypatch.setattr(tools, "present_tool", present)
        tool = RuntimeToolCallActivity(
            tool_call_id="write",
            tool_name="Write",
            status="running",
            source_index=0,
            arguments={"path": "out.txt", "content": "line\n" * 100000},
        )
        conversation = app.query_one(ConversationView)
        await conversation.sync_tools((tool,))
        row = conversation.query_one(tools.ToolActivityRow)
        calls = 0
        for _ in range(20):
            row._tick()
        assert calls == 0
        assert "+100000" in row.render().plain
        row.apply_tool(tool.model_copy(update={"arguments": {"path": "out.txt", "content": "new"}}))
        assert calls == 1
        assert "+1 line" in row.render().plain

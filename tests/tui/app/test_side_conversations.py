"""Writable side conversations on the existing TUI navigation surface."""

import asyncio
import json
import re
from pathlib import Path

import pytest

from ouro.application.inputs.requests import UserInputRequest
from ouro.application.transfer.sessions import SessionTransferService
from ouro.core.billing import UsageFilter
from ouro.core.errors import ConfigError, SessionBranchError
from ouro.core.input_content import InputContent, InputFileReference, InputSkillReference
from ouro.core.messages import MessageRecord
from ouro.core.provider import ProviderStreamEvent
from ouro.providers import FauxProvider
from ouro.resources.local import LocalResourceStore
from ouro.tools import builtin_tools
from ouro.tui.app import OuroTuiApp
from ouro.tui.composer.input import Composer
from ouro.tui.conversation.history import ConversationHistory
from ouro.tui.conversation.tools import ToolActivityRow
from ouro.tui.conversation.view import ConversationView
from ouro.tui.menus.choice import ChoiceScreen
from ouro.tui.menus.multiple import MultiChoiceScreen
from ouro.tui.preferences import TuiPreferences, TuiPreferencesStore
from ouro.tui.session_navigation import SessionNavigationPanel
from tests.application.test_responses_runtime import responses_runtime as responses_runtime
from tests.application.test_runtime_authentication import login
from tests.providers.responses_fixtures import call, message, reasoning, terminal
from tests.tui.app.providers import BlockingStreamProvider, MultiChildStreamingProvider
from tests.tui.app.support import (
    _blocking_application,
    _multi_child_streaming_application,
    _wait_for_tool_approval,
)
from tests.tui.helpers import (
    choose_menu,
    release_on_exit,
    run_ready_app,
    wait_for_screen,
    wait_for_worker_group,
    wait_until,
)
from tests.tui.test_history_commands import application_at


@pytest.mark.asyncio
@pytest.mark.parametrize("remember", [True, False])
async def test_btw_command_and_node_switches_keep_independent_input_surfaces(
    tmp_path: Path,
    remember: bool,
) -> None:
    provider = BlockingStreamProvider()
    provider.release.set()
    application = _blocking_application(tmp_path, provider)
    TuiPreferencesStore(tmp_path).save(TuiPreferences(input_draft_memory_enabled=remember))
    source = application.sessions.create(title="Debug the timeout").session.session_id
    app = OuroTuiApp(application=application)

    async with run_ready_app(app, size=(120, 34)) as pilot:
        await app._open_session(source)
        composer = app.query_one(Composer)
        source_draft = InputContent(
            text="main draft",
            attachments=(InputFileReference(name="log.txt", path=str(tmp_path / "log.txt")),),
            skills=(InputSkillReference(name="review", text_offset=2, order=1),),
        )
        composer.load_input(source_draft)
        composer.move_cursor((0, 0))
        await pilot.press("slash", *"btw", "space", *"explain", "enter")
        await wait_until(
            lambda: (
                app._controller.session_id not in {source, None}
                and app._projection is not None
                and app._projection.activity.status == "completed"
            ),
            pilot,
            description="writable side response",
        )
        side = app._controller.session_id
        assert side and app._visible_session_id() == side and not composer.disabled
        assert [
            m.text_content
            for m in application.runtime.store.load_session(side).messages
            if m.role == "user"
        ] == ["explain"]
        assert not application.runtime.store.load_session(source).messages
        panel = app.query_one(SessionNavigationPanel)
        assert panel.current_session_id == side and panel.render_content(120).plain.endswith(
            "▣ | ■"
        )
        side_draft = InputContent(text="side draft", skills=(InputSkillReference(name="debug"),))
        composer.load_input(side_draft)
        await pilot.press("alt+up")
        await wait_until(
            lambda: app._visible_session_id() == source,
            pilot,
            description="source surface restored",
        )
        assert app._controller.session_id == source and composer.input_content == source_draft
        await application.runtime.run("MAIN-NEW", session_id=source)
        await pilot.press("alt+left")
        await wait_until(
            lambda: app._visible_session_id() == side, pilot, description="side surface restored"
        )
        assert composer.input_content == side_draft and app._controller.session_id == side
        assert application.sessions.view(side).message_count == 2
        composer.load_text("Follow up")
        await pilot.press("enter")
        await wait_until(
            lambda: application.sessions.view(side).message_count >= 5,
            pilot,
            description="side follow-up",
        )
        messages = application.runtime.store.load_session(side).messages
        assert messages[-3].semantic_tag == "source-session-context"
        assert "MAIN-NEW" in messages[-3].text_content and messages[-2].text_content == "Follow up"


@pytest.mark.asyncio
async def test_rejected_first_question_is_retriable_in_the_same_side_session(tmp_path, monkeypatch):
    provider = BlockingStreamProvider()
    provider.release.set()
    application = _blocking_application(tmp_path, provider)
    source = application.sessions.create().session.session_id
    app = OuroTuiApp(application=application)
    question = "Explain the timeout"

    async def reject(request, **kwargs):
        assert request.prompt == question and request.session_id != source
        raise ConfigError("Input was rejected before acceptance")

    async with run_ready_app(app) as pilot:
        await app._open_session(source)
        with monkeypatch.context() as patch:
            patch.setattr(application.supervisor, "submit_user_input", reject)
            await app._dispatch_command(f"/btw {question}")
            await wait_until(
                lambda: app.query_one(Composer).text == question and not app._input_drafts.pending,
                pilot,
                description="first question restored to side draft",
            )
        (side,) = await app._controller.side_conversations(source)
        assert app._controller.session_id == app._visible_session_id() == side.session_id
        assert application.sessions.view(side.session_id).message_count == 0
        await pilot.press("enter")
        await wait_until(
            lambda: application.sessions.view(side.session_id).message_count == 2,
            pilot,
            description="same side accepted the retried question",
        )
        assert [
            m.text_content
            for m in application.runtime.store.load_session(side.session_id).messages
            if m.role == "user"
        ] == [question]
        assert application.sessions.view(source).message_count == 0


async def test_queued_side_command_returns_to_its_source_as_a_command(tmp_path):
    application = application_at(tmp_path)
    source, other = (application.sessions.create().session.session_id for _ in range(2))
    app = OuroTuiApp(application=application, resume_session_id=source)
    async with run_ready_app(app) as pilot:
        composer = app.query_one(Composer)
        async with app._composer_submission_lock:
            composer.load_text("/btw Explain the log in A")
            await pilot.press("enter")
            await app._open_session(other)
        await wait_for_worker_group(app, pilot, group="composer-command")
        assert {item.session_id for item in application.sessions.catalog().entries} == {
            source,
            other,
        }
        assert composer.input_content.is_empty
        await app._open_session(source)
        assert composer.command_context() == ("/btw Explain the log in A", "")
        assert application.sessions.view(other).message_count == 0


async def test_side_nodes_open_its_own_agent_then_return_to_source(tmp_path):
    provider = MultiChildStreamingProvider()
    application = _multi_child_streaming_application(tmp_path, provider)
    source = application.sessions.create().session.session_id
    app = OuroTuiApp(application=application, resume_session_id=source)
    async with (
        run_ready_app(app) as pilot,
        release_on_exit(provider.release_root, *provider.releases.values()),
    ):
        app.query_one(Composer).load_text("/btw start two children")
        await pilot.press("enter")
        panel = app.query_one(SessionNavigationPanel)
        await wait_until(
            lambda: len(app._agent_nodes) == 2, pilot, description="side dispatched both agents"
        )
        side = app._visible_session_id()
        child = next(iter(app._agent_nodes.values())).session_id
        assert side != source and child in panel.visible_related_session_ids
        app._navigate_to_session_node(child)
        await wait_until(
            lambda: app._visible_session_id() == child, pilot, description="side agent opened"
        )
        assert app._visible_read_only and app._controller.session_id == side
        for target in (side, source):
            await pilot.press("alt+up")
            await wait_until(
                lambda target=target: (
                    app._visible_session_id() == target and not app._session_switch_in_progress
                ),
                pilot,
                description="returned to owning conversation",
            )


async def test_nested_nodes_follow_direct_source_without_scheduling_runs(tmp_path, monkeypatch):
    application = application_at(tmp_path)
    sessions = application.sessions
    root = sessions.create().session.session_id
    side = sessions.create_side_conversation(root, "Explain the log").session.session_id
    nested = sessions.create_side_conversation(side, "Explain the unit").session.session_id
    app = OuroTuiApp(application=application, resume_session_id=root)
    async with run_ready_app(app) as pilot:

        async def unexpected_schedule(*args, **kwargs):
            pytest.fail("Browsing a node scheduled execution")

        monkeypatch.setattr(application.supervisor, "schedule_session", unexpected_schedule)
        panel = app.query_one(SessionNavigationPanel)
        for key, target in (
            ("alt+left", side),
            ("alt+right", nested),
            ("alt+up", side),
            ("alt+up", root),
            ("alt+left", side),
            ("alt+right", nested),
        ):
            await pilot.press(key)
            await wait_until(
                lambda target=target: (
                    app._visible_session_id() == target and not app._session_switch_in_progress
                ),
                pilot,
                description="direct source and nested side are reachable",
            )
            assert app._controller.session_id == panel.current_session_id == target
        assert all(sessions.view(s).message_count == 0 for s in (root, side, nested))


@pytest.mark.asyncio
async def test_btw_while_streaming_keeps_both_runs_alive(tmp_path: Path) -> None:
    provider = BlockingStreamProvider()
    application = _blocking_application(tmp_path, provider)
    source = application.sessions.create().session.session_id
    app = OuroTuiApp(application=application)
    async with run_ready_app(app, size=(100, 32)) as pilot:
        await app._open_session(source)
        await pilot.press(*"trace", "enter")
        await wait_until(provider.first_chunk_sent.is_set, pilot, description="source streaming")
        run_id = application.sessions.view(source).active_run_id
        try:
            await pilot.press("slash", *"btw", "space", *"explain", "enter")
            await wait_until(
                lambda: (
                    app._visible_session_id() not in {None, source}
                    and app._projection.activity.run_id is not None
                ),
                pilot,
                description="side streaming",
            )
            side = app._controller.session_id
            assert side
            assert [
                m.text_content for m in application.runtime.store.load_session(side).messages
            ] == ["trace", "explain"]
            await pilot.press("alt+up")
            await wait_until(
                lambda: app._visible_session_id() == source, pilot, description="live source"
            )
            assert application.sessions.view(source).active_run_id == run_id
            assert application.sessions.view(side).active_run_id not in {None, run_id}
        finally:
            provider.release.set()
        await wait_until(
            lambda: all(
                application.runtime.store.load_runs(s)[-1].status == "completed"
                for s in (source, side)
            ),
            pilot,
            description="both runs completed",
        )
        assert [
            m.text_content for m in application.runtime.store.load_session(source).messages
        ] == ["trace", "first second"]


@pytest.mark.asyncio
async def test_two_pending_approvals_follow_their_session_nodes(tmp_path: Path) -> None:
    target = tmp_path / "side-write.txt"
    main_target = tmp_path / "main-write.txt"

    def write_request(call_id, path):
        return [
            ProviderStreamEvent.from_tool_call(
                {
                    "id": call_id,
                    "name": "Write",
                    "arguments": {"path": str(path), "content": call_id},
                }
            ),
            ProviderStreamEvent(type="done"),
        ]

    completed = [
        ProviderStreamEvent(type="text_delta", text="Done"),
        ProviderStreamEvent(type="done"),
    ]
    provider = FauxProvider(
        event_batches=[
            write_request("side_write", target),
            write_request("main_write", main_target),
            completed,
            completed,
        ]
    )
    application = application_at(tmp_path, provider)
    for tool in builtin_tools():
        application.runtime.tools.register(tool)
    source = application.sessions.create(
        approval_mode="approval", workspace=str(tmp_path)
    ).session.session_id
    app = OuroTuiApp(application=application)
    async with run_ready_app(app, size=(100, 32)) as pilot:
        await app._open_session(source)
        await pilot.press("slash", *"btw", "space", *"write", "enter")
        await _wait_for_tool_approval(app, pilot, tool_call_id="side_write")
        side = app._controller.session_id
        pending = app._interaction
        assert side and pending and pending.session_id == side
        assert [entry.text for entry in app.query_one(ConversationHistory).entries] == ["write"]
        assert app.query_one(ToolActivityRow).tool_call_id == "side_write"
        await pilot.press("alt+up")
        await wait_until(
            lambda: app._visible_session_id() == source, pilot, description="source input"
        )
        assert app._interaction is None and not app.query_one(Composer).disabled
        assert application.sessions.view(side).active_run_id == pending.run_id
        assert not target.exists()
        await pilot.press(*"write", "enter")
        await _wait_for_tool_approval(app, pilot, tool_call_id="main_write")
        main_pending = app._interaction
        assert main_pending.session_id == source and main_pending.run_id != pending.run_id
        await pilot.press("alt+left")
        await _wait_for_tool_approval(app, pilot, tool_call_id="side_write")
        assert app._interaction == pending
        await pilot.press("enter")
        await wait_until(
            lambda: app._projection.activity.status == "completed", pilot, description="side write"
        )
        assert target.read_text(encoding="utf-8") == "side_write" and not main_target.exists()
        await pilot.press("alt+up")
        await _wait_for_tool_approval(app, pilot, tool_call_id="main_write")
        assert app._interaction == main_pending
        await pilot.press("enter")
        await wait_until(
            lambda: app._projection.activity.status == "completed",
            pilot,
            description="source write",
        )
        assert main_target.read_text(encoding="utf-8") == "main_write"


async def test_standalone_imported_side_is_openable_from_the_session_picker(tmp_path):
    origin_dir, target_dir = tmp_path / "origin", tmp_path / "target"
    async with application_at(origin_dir) as origin:
        source = origin.sessions.create().session.session_id
        side = origin.sessions.create_side_conversation(
            source, "Standalone question"
        ).session.session_id
        archive = SessionTransferService(
            origin.runtime.store, LocalResourceStore(origin_dir)
        ).export((side,))
    target = application_at(target_dir)
    SessionTransferService(target.runtime.store, LocalResourceStore(target_dir)).import_data(
        archive
    )
    app = OuroTuiApp(application=target)
    async with run_ready_app(app) as pilot:
        await pilot.press("ctrl+o")
        await wait_for_screen(app, ChoiceScreen, title="SESSION • USER")
        assert [choice.value for choice in app.screen._choices] == [side]
        await pilot.press("enter")
        await wait_until(
            lambda: app._visible_session_id() == side, pilot, description="imported side opened"
        )
        assert app._controller.session_id == side and not app._visible_read_only


async def test_side_navigation_and_picker_when_source_is_deleted(tmp_path):
    application = application_at(tmp_path)
    source = application.sessions.create(title="Debug timeout").session.session_id
    side = application.sessions.create_side_conversation(
        source, "Explain timeout"
    ).session.session_id
    app = OuroTuiApp(application=application, resume_session_id=source)
    async with run_ready_app(app) as pilot:
        panel = app.query_one(SessionNavigationPanel)
        assert side in panel.visible_related_session_ids
        await pilot.press("alt+left")
        await wait_until(lambda: app._visible_session_id() == side, pilot, description="side node")
        app.query_one(Composer).load_text("unfinished question")
        await pilot.press("ctrl+o")
        await wait_for_screen(app, ChoiceScreen, title="SESSION • USER")
        assert [choice.value for choice in app.screen._choices] == [source]
        await pilot.press("escape")
        assert app._visible_session_id() == side
        await pilot.press("ctrl+p")
        await wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        await choose_menu(pilot, "Rename session", title="RENAME SESSION • USER")
        assert {choice.value for choice in app.screen._choices} == {source, side}
        await choose_menu(pilot, "Explain timeout")
        await pilot.press("ctrl+u", *"Retry", "enter")
        await wait_until(
            lambda: "Retry" in panel.render_content(80).plain,
            pilot,
            description="renamed side node",
        )
        assert application.sessions.view(side).title == "Retry"
        await pilot.press("escape")
        await wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        await choose_menu(pilot, "Archive session", title="ARCHIVE SESSIONS • USER")
        assert {choice.value for choice in app.screen._choices} == {source, side}
        await pilot.press("ctrl+a", "enter", "down", "enter")
        await wait_until(
            lambda: side not in panel.visible_related_session_ids,
            pilot,
            description="archived side removed from navigation",
        )
        assert panel.current_session_id == side and panel.render_content(80).plain.strip() == "■"
        await pilot.press("escape")
        await wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        await choose_menu(pilot, "Delete session", title="DELETE SESSIONS • ARCHIVED")
        assert isinstance(app.screen, MultiChoiceScreen)
        assert {choice.value for choice in app.screen._choices} == {source, side}
        app.screen.query_one("#choice-filter").value = "Debug timeout"
        await pilot.pause()
        await pilot.press("enter")
        assert "side conversations" in app.screen._choices[-1].description
        await pilot.press("down", "enter")
        await wait_until(
            lambda: (
                app._visible_session_id() is None
                and app.query_one(ConversationView).showing_welcome
            ),
            pilot,
            description="deleted side view returns to welcome",
        )
        assert not application.sessions.catalog().entries
        assert app._input_drafts.content_for(side).is_empty
        assert not app._side_sessions and side not in app._session_history_states


async def test_side_history_paging_stops_at_its_first_own_turn(tmp_path):
    application = application_at(tmp_path)
    source = application.sessions.create().session.session_id
    store = application.runtime.store
    store.append_messages(
        [
            MessageRecord.text(session_id=source, role="assistant", text=f"source {i}")
            for i in range(205)
        ]
    )
    side = application.sessions.create_side_conversation(source, "Explain").session.session_id
    store.append_messages(
        [
            MessageRecord.text(session_id=side, role="assistant", text=f"answer {i}")
            for i in range(203)
        ]
    )
    app = OuroTuiApp(application=application, resume_session_id=side)
    async with run_ready_app(app):
        history = app.query_one(ConversationHistory)
        assert len(history.entries) == 200
        assert await app._prepend_history_window(app._view_revision)
        assert [entry.text for entry in history.entries] == [f"answer {i}" for i in range(203)]
        assert app._history_before_cursor is None
        assert not await app._prepend_history_window(app._view_revision)
        await app._open_session(source)
        assert history.entries[-1].text == "source 204"
        assert app._history_before_cursor is not None


@pytest.mark.parametrize("responses_runtime", [("files", False), ("sqlite", False)], indirect=True)
async def test_side_conversation_delivers_source_progress_at_user_consumption(
    responses_runtime, tmp_path, monkeypatch
):
    build, replies, requests, executions = responses_runtime

    async def ask(app, question, answer, *, session_id=None, tool=None):
        if tool is not None:
            replies.append(terminal([call(tool) if isinstance(tool, int) else tool]))
        replies.append(terminal([message(answer, item_id=f"answer-{len(requests)}")]))
        result = await app.runtime.run(question, session_id=session_id)
        assert result.run.status == "completed", result.run.error
        return result.session.session_id

    async with build() as app:
        await login(app.provider_auth)
        log = tmp_path / "failure.log"
        log.write_text("ETIMEDOUT-701 waiting for a connection from the pool", encoding="utf-8")
        source = app.sessions.create(workspace=str(tmp_path)).session.session_id
        await ask(
            app,
            "Inspect the timeout log",
            "The pool timed out.",
            session_id=source,
            tool=call(701, name="Read", arguments=json.dumps({"path": str(log)})),
        )
        tui = OuroTuiApp(application=app, close_application=False)
        async with run_ready_app(tui) as pilot:
            await tui._open_session(source)

            def explain_log(payload):
                code = re.search(r"ETIMEDOUT-\d+", json.dumps(payload["input"]))
                assert code is not None
                return terminal([message(f"SIDE-ANSWER: {code.group()} queue wait.")])

            side_reasoning = "Checking the pool log."
            replies.extend([terminal([reasoning(side_reasoning), call(702)]), explain_log])
            tui.query_one(Composer).load_text("/btw SIDE-QUESTION: explain the log.")
            await pilot.press("enter")
            await wait_until(
                lambda: (
                    tui._visible_session_id() not in {source, None}
                    and tui._projection.activity.status == "completed"
                ),
                pilot,
                description="side answer through Responses",
            )
            side = app.sessions.view(tui._visible_session_id())
            assert side.context_source_session_id == source
            assert side.parent_session_id is None and side.root_session_id == side.session_id
            assert side.forked_from_message_id == app.sessions.view(source).last_message_id
            assert [
                item.session_id for item in await tui._controller.side_conversations(source)
            ] == [side.session_id]
            assert "ETIMEDOUT-701" in json.dumps(requests[-2][0]["input"])
            previous = requests[-1][0]
            first_turn = [
                ("user", "SIDE-QUESTION: explain the log."),
                ("assistant", side_reasoning),
                ("tool", "call-702"),
                ("assistant", "SIDE-ANSWER: ETIMEDOUT-701 queue wait."),
                ("run", "completed"),
            ]

            def transcript(history):
                rows = []
                for entry in history.entries:
                    if entry.tool:
                        content = entry.tool.tool_call_id
                    elif entry.run:
                        content = entry.run.status
                    else:
                        content = entry.reasoning or entry.text
                    rows.append((entry.kind, content))
                return rows

            history = tui.query_one(ConversationHistory)
            await wait_until(
                lambda: any(entry.kind == "run" for entry in history.entries),
                pilot,
                description="side run summary committed after its answer",
            )
            assert transcript(history) == first_turn
            await pilot.press("alt+up")
            await wait_until(
                lambda: tui._visible_session_id() == source, pilot, description="source node"
            )
            await ask(
                app,
                "Check connection limits",
                "The limit was reached.",
                session_id=source,
                tool=703,
            )
            await ask(app, "Retry the tests", "MAIN-RETEST: 6/6", session_id=source)
            request_count = len(requests)
            await pilot.press("alt+left")
            await wait_until(
                lambda: tui._visible_session_id() == side.session_id, pilot, description="side node"
            )
            frozen = tui._controller.pending_source_history
            assert frozen and app.runtime.source_progress(side.session_id) == frozen
            await pilot.press("alt+up")
            await wait_until(
                lambda: tui._visible_session_id() == source, pilot, description="repeat return"
            )
            await pilot.press("alt+left")
            await wait_until(
                lambda: tui._visible_session_id() == side.session_id,
                pilot,
                description="repeat entry",
            )
            assert tui._controller.pending_source_history == frozen
            assert len(requests) == request_count
            assert transcript(history) == first_turn
        followup = UserInputRequest(
            prompt="Combine our term, the logs and the retest.",
            session_id=side.session_id,
            source_history=frozen,
        )
        # Concurrent acceptance and lost-reply retries retain one immutable intent.
        receipts = await asyncio.gather(
            *(app.runtime.accept_user_input(followup) for _ in range(2))
        )
        assert receipts[0].queue_id == receipts[1].queue_id
        assert app.runtime.source_progress(side.session_id) == frozen
        await ask(app, "Continue working", "MAIN-AFTER-FREEZE", session_id=source)

    async with build() as app:

        def answer_from_request(payload):
            assert {k: v for k, v in payload.items() if k != "input"} == {
                k: v for k, v in previous.items() if k != "input"
            }
            assert payload["input"][: len(previous["input"])] == previous["input"]
            answer, progress, question = payload["input"][len(previous["input"]) :]
            assert "SIDE-ANSWER" in json.dumps(answer)
            assert progress["role"] == "user" and "<source-session-context>" in progress["content"]
            assert question["content"] == followup.prompt and "MAIN-AFTER-FREEZE" not in json.dumps(
                payload
            )
            wire = json.dumps(payload["input"])
            markers = [
                "ETIMEDOUT-701",
                "SIDE-QUESTION",
                "result-702",
                "SIDE-ANSWER",
                "result-703",
                "MAIN-RETEST",
            ]
            return terminal([message(" ".join(marker for marker in markers if marker in wire))])

        replies.append(answer_from_request)
        result = await app.runtime.run_pending_inputs(side.session_id, history_epoch=0)
        assert (
            result
            and result.final_text
            == "ETIMEDOUT-701 SIDE-QUESTION result-702 SIDE-ANSWER result-703 MAIN-RETEST"
        )
        assert (await app.runtime.accept_user_input(followup)).run_id == result.run.run_id
        usage = app.runtime.usage.list_entries(UsageFilter(run_ids={result.run.run_id})).entries
        assert [entry.cache_read_tokens for entry in usage] == [5]  # Reported by the HTTP fixture.
        await ask(app, "Keep investigating", "Working.", session_id=source)
        parent = json.dumps(requests[-1][0])
        assert all(
            marker not in parent
            for marker in (
                "SIDE-QUESTION",
                "SIDE-ANSWER",
                "result-702",
                "source-session-context",
                followup.prompt,
            )
        )
        assert all(marker in parent for marker in ("ETIMEDOUT-701", "result-703", "MAIN-RETEST"))
        remaining = app.runtime.source_progress(side.session_id)
        assert remaining and remaining.after
        assert remaining.after.message_id == frozen.entries[-1].message_id
        assert executions == [702, 703]

        resumed = OuroTuiApp(application=app, close_application=False)
        async with run_ready_app(resumed):
            await resumed._open_session(side.session_id)
            assert await resumed._set_harness_messages(True)
            assert transcript(resumed.query_one(ConversationHistory)) == [
                *first_turn,
                ("user", followup.prompt),
                ("assistant", result.final_text),
                ("run", "completed"),
            ]
        undone = app.sessions.undo(side.session_id)
        assert undone.text_content == followup.prompt
        assert not any(
            m.semantic_tag == "source-session-context"
            for m in app.runtime.store.load_session(side.session_id).messages
        )
        again = app.runtime.source_progress(side.session_id)
        assert again and again.after == frozen.after
        app.sessions.undo(
            source,
            message_id=next(
                m.message_id
                for m in app.runtime.store.load_session(source).messages
                if m.role == "user"
            ),
        )
        with pytest.raises(SessionBranchError, match="rewritten"):
            app.runtime.source_progress(side.session_id)
        reopened = OuroTuiApp(application=app, close_application=False)
        warnings = []
        monkeypatch.setattr(reopened, "notify", lambda text, **kwargs: warnings.append(str(text)))
        async with run_ready_app(reopened):
            await reopened._open_session(side.session_id)
            assert reopened._controller.session_id == side.session_id
            assert not reopened.query_one(Composer).disabled
            assert reopened._controller.pending_source_history is None
            assert any("rewritten" in text for text in warnings)
            elsewhere = app.sessions.create().session.session_id
            await reopened._open_session(elsewhere)
            removed = app.sessions.trash_from_catalog(source)
            assert set(removed.session_ids) == {source, side.session_id}
            assert [entry.session_id for entry in app.sessions.catalog().entries] == [elsewhere]

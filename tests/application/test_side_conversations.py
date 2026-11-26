from __future__ import annotations

import json

import pytest

from ouro.application.inputs.requests import UserInputRequest
from ouro.config import ProviderConfigManager
from ouro.config.queued_inputs import QueuedInputConfigManager
from ouro.core.compaction import CompactionRecord
from ouro.core.messages import MessageRecord, ToolResultReference
from ouro.core.session_extensions import SessionExtensionStateRecord
from ouro.core.tools import ToolCallRequest
from tests.application.test_responses_runtime import responses_runtime as responses_runtime
from tests.application.test_runtime_authentication import login
from tests.harness_state import replace_harness_state
from tests.providers.responses_fixtures import call, message, terminal
from tests.session_summaries import commit_summary, select_summary
from tests.tool_batches import seed_tool_messages


@pytest.mark.parametrize("responses_runtime", [("files", False), ("sqlite", False)], indirect=True)
async def test_side_context_waits_for_publication_of_entire_tool_batch(
    responses_runtime, monkeypatch
):
    build, replies, _, executions = responses_runtime
    async with build() as app:
        await login(app.provider_auth)
        source = app.sessions.create().session.session_id
        first = app.sessions.create_side_conversation(source, "Before tools").session
        publish = app.runtime.run_lifecycle.publish_tool_batch
        during = []

        def capture_before_publication(**kwargs):
            assert all(item.result is not None for item in kwargs["batch"].calls)
            progress = app.runtime.source_progress(first.session_id)
            assert progress and [item.text for item in progress.entries] == [
                "[user] Inspect both logs"
            ]
            second = app.sessions.create_side_conversation(source, "During tools").session
            copied = app.runtime.store.load_session(second.session_id).messages
            assert [item.text_content for item in copied] == ["Inspect both logs"]
            assert second.forked_from_message_id == progress.entries[0].message_id
            assert app.sessions.view(source).active_run_id == kwargs["run"].run_id
            during.append(second)
            raise RuntimeError("Interrupted before tool feedback publication")

        monkeypatch.setattr(
            app.runtime.run_lifecycle, "publish_tool_batch", capture_before_publication
        )
        replies.extend([terminal([call(711), call(712)]), terminal([message("Batch finished")])])
        interrupted = await app.runtime.run("Inspect both logs", session_id=source)
        assert interrupted.run.status == "failed"
        assert app.sessions.view(source).active_run_id is None
        parked = app.sessions.create_side_conversation(source, "While interrupted").session
        assert parked.forked_from_message_id == during[0].forked_from_message_id
        assert app.runtime.source_progress(parked.session_id) is None
        monkeypatch.setattr(app.runtime.run_lifecycle, "publish_tool_batch", publish)
        result = await app.runtime.continue_run(session_id=source)
        assert result.run.status == "completed", result.run.error
        assert executions == [711, 712]
        first_progress = app.runtime.source_progress(first.session_id)
        second_progress = app.runtime.source_progress(during[0].session_id)
        assert (
            first_progress
            and second_progress
            and first_progress.entries[1:] == second_progress.entries
        )
        assert [item.text for item in second_progress.entries] == [
            '[assistant] echo({"round": 711})\necho({"round": 712})\n'
            "[echo result] result-711\n[echo result] result-712",
            "[assistant] Batch finished",
        ]
        assert app.runtime.source_progress(parked.session_id) == second_progress


@pytest.mark.parametrize("responses_runtime", [("files", False), ("sqlite", False)], indirect=True)
async def test_snapshot_and_increment_skip_abandoned_output_in_the_middle(responses_runtime):
    build, replies, requests, _ = responses_runtime
    async with build() as app:
        await login(app.provider_auth)
        source = app.sessions.create().session.session_id
        early = app.sessions.create_side_conversation(source, "Before interruption").session
        partial = MessageRecord.text(session_id=source, role="assistant", text="UNFINISHED-STREAM")
        partial.metadata["incomplete"] = True
        app.runtime.store.append_message(partial)
        reset = MessageRecord.text(
            session_id=source, role="harness", semantic_tag="plan-context", text="RESET-ONLY-412"
        )
        app.runtime.store.append_message(reset)
        replies.append(terminal([message("COMMITTED-ANSWER")]))
        continued = await app.runtime.run("Continue the investigation", session_id=source)
        assert continued.run.status == "completed", continued.run.error
        assert "RESET-ONLY-412" in json.dumps(requests[-1][0]["input"])
        replace_harness_state(
            app.runtime.store,
            SessionExtensionStateRecord(
                session_id=source,
                owner_id="test.plan",
                state_schema_version=1,
                branch_policy="reset",
                projection_semantic_tags=("plan-context",),
                payload={"active": True},
            ),
        )
        store = app.runtime.store
        last = store.load_session(source).messages[-1]
        first_summary = CompactionRecord(
            session_id=source,
            strategy_id="test",
            trigger="manual",
            summary="Orphaned draft",
            covered_message_count=1,
            covered_end_message_id=partial.message_id,
            history_boundary_message_id=partial.message_id,
        )
        final_summary = first_summary.model_copy(
            update={
                "compaction_id": "cmp_committed",
                "previous_compaction_id": first_summary.compaction_id,
                "summary": "Resolved: COMMITTED-ANSWER, RESET-ONLY-412",
                "covered_message_count": last.sequence,
                "covered_end_message_id": last.message_id,
                "history_boundary_message_id": last.message_id,
            }
        )
        commit_summary(store, first_summary)
        commit_summary(store, final_summary)
        late = app.sessions.create_side_conversation(source, "After recovery").session
        progress = app.runtime.source_progress(early.session_id)
        assert progress and [e.text for e in progress.entries] == [
            "[user] Continue the investigation",
            "[assistant] COMMITTED-ANSWER",
        ]
        assert late.forked_from_message_id == progress.entries[-1].message_id
        assert [
            m.text_content for m in app.runtime.store.load_session(late.session_id).messages
        ] == ["Continue the investigation", "COMMITTED-ANSWER"]
        replies.append(terminal([message("Understood")]))
        response = await app.runtime.run("What finished?", session_id=late.session_id)
        assert response.run.status == "completed", response.run.error
        wire = json.dumps(requests[-1][0]["input"])
        assert "COMMITTED-ANSWER" in wire and "Continue the investigation" in wire
        assert final_summary.summary not in wire and "UNFINISHED-STREAM" not in wire
        assert "RESET-ONLY-412" not in wire
        assert not store.load_compactions(late.session_id)


@pytest.mark.parametrize("responses_runtime", [("files", False), ("sqlite", False)], indirect=True)
async def test_conflicting_frozen_inputs_report_a_diagnostic_outside_model_history(
    responses_runtime,
):
    build, replies, requests, _ = responses_runtime
    async with build() as app:
        await login(app.provider_auth)
        source = app.sessions.create().session.session_id
        side = app.sessions.create_side_conversation(source, "Inspect").session.session_id
        nested = app.sessions.create_side_conversation(side, "Inspect the side discussion").session
        replies.append(terminal([message("OLD-RESULT")]))
        await app.runtime.run("Discarded investigation", session_id=source)
        stale = app.runtime.source_progress(side)
        app.sessions.undo(source)
        replies.append(terminal([message("REPLACEMENT-RESULT")]))
        await app.runtime.run("Replacement investigation", session_id=source)
        current = app.runtime.source_progress(side)
        assert stale and current and stale.history_epoch != current.history_epoch
        events = []
        app.runtime.add_event_sink(events.append)
        for evidence, question in (
            (current, "Use the replacement"),
            (stale, "Ask from the other window"),
        ):
            await app.runtime.accept_user_input(
                UserInputRequest(session_id=side, prompt=question, source_history=evidence)
            )
            replies.append(terminal([message("Answered")]))
            result = await app.runtime.run_pending_inputs(side, history_epoch=0)
            assert result and result.run.status == "completed", result
        (diagnostic,) = [e for e in events if e.payload.get("code") == "source_history_conflict"]
        assert diagnostic.event_type == "diagnostic" and diagnostic.session_id == side
        wire = json.dumps(requests[-1][0]["input"])
        assert "Ask from the other window" in wire and "REPLACEMENT-RESULT" in wire
        assert "OLD-RESULT" not in wire and diagnostic.payload["message"] not in wire
        assert (
            sum(
                m.semantic_tag == "source-session-context"
                for m in app.runtime.store.load_session(side).messages
            )
            == 1
        )
        assert app.runtime.source_progress(side) is None
        nested_progress = app.runtime.source_progress(nested.session_id)
        assert nested_progress and [entry.text for entry in nested_progress.entries] == [
            "[user] Use the replacement",
            "[assistant] Answered",
            "[user] Ask from the other window",
            "[assistant] Answered",
        ]


@pytest.mark.parametrize("responses_runtime", [("files", False), ("sqlite", False)], indirect=True)
async def test_undo_preserves_pending_direct_questions_and_their_order(responses_runtime, tmp_path):
    build, replies, requests, _ = responses_runtime
    QueuedInputConfigManager(tmp_path).update(consumption_mode="all_at_once")
    async with build() as app:
        await login(app.provider_auth)
        source = app.sessions.create().session.session_id
        side = app.sessions.create_side_conversation(source, "Ask").session.session_id
        replies.append(terminal([message("Earlier answer")]))
        await app.runtime.run("Earlier question", session_id=side)
        earlier = app.runtime.store.load_session(side).messages[0]
        replies.append(terminal([message("RECENT-LOG")]))
        await app.runtime.run("Retest", session_id=source)
        progress = app.runtime.source_progress(side)
        assert progress
        questions = (
            UserInputRequest(session_id=side, prompt="Explain this log", source_history=progress),
            UserInputRequest(session_id=side, prompt="Another question"),
        )
        for question in questions:
            await app.runtime.accept_user_input(question)
        app.sessions.undo(side, message_id=earlier.message_id)
        record = app.runtime.store.load_session_record(side)
        assert list(record.pending_input_ids) == [question.input_id for question in questions]
        replies.append(terminal([message("Both answered")]))
        result = await app.runtime.run_pending_inputs(side, history_epoch=record.history_epoch)
        assert result and result.run.status == "completed"
        wire = json.dumps(requests[-1][0]["input"])
        assert "RECENT-LOG" in wire and wire.index(questions[0].prompt) < wire.index(
            questions[1].prompt
        )


@pytest.mark.parametrize("responses_runtime", [("files", False), ("sqlite", False)], indirect=True)
async def test_large_tool_batch_is_omitted_as_a_unit_after_model_budget_shrinks(
    responses_runtime, tmp_path
):
    build, replies, requests, _ = responses_runtime
    async with build() as app:
        await login(app.provider_auth)
        store = app.runtime.store
        source = app.sessions.create().session.session_id
        side = app.sessions.create_side_conversation(source, "Ask").session.session_id
        store.append_messages(
            [
                MessageRecord.text(session_id=source, role="assistant", text=f"progress {i}")
                for i in range(63)
            ]
        )
        tool = MessageRecord.text(
            session_id=source, run_id="run_log", role="assistant", text=""
        ).model_copy(update={"tool_calls": [ToolCallRequest(id="call_log", name="Read")]})
        output = MessageRecord.text(
            session_id=source, run_id="run_log", role="tool_result", text="LARGE-RESULT " * 25000
        ).model_copy(
            update={"tool_result": ToolResultReference(tool_call_id="call_log", tool_name="Read")}
        )
        seed_tool_messages(store, tool, [output])
        last = MessageRecord.text(session_id=source, role="assistant", text="SHORT-RETEST")
        store.append_messages([tool, output, last])
        progress = app.runtime.source_progress(side)
        assert progress and "LARGE-RESULT" in progress.entries[-2].text
        await app.runtime.accept_user_input(
            UserInputRequest(session_id=side, prompt="What changed?", source_history=progress)
        )
        model = app.runtime.current_model(side)
    ProviderConfigManager(tmp_path).update_endpoint(
        model.provider_id, model.endpoint_id, context_window=32768
    )
    async with build() as app:
        refrozen = app.runtime.source_progress(side)
        assert (
            refrozen
            and refrozen.entries[-2].omitted
            and refrozen.entries[-1].message_id == last.message_id
        )
        warnings = []
        app.runtime.add_event_sink(warnings.append)
        replies.append(terminal([message("The retest finished")]))
        result = await app.runtime.run_pending_inputs(side, history_epoch=0)
        assert result and result.run.status == "completed", result
        wire = json.dumps(requests[-1][0]["input"])
        assert "What changed?" in wire and "SHORT-RETEST" in wire and "omitted" in wire
        assert "LARGE-RESULT" not in wire and "[assistant] Read(" not in wire
        assert any(event.payload.get("code") == "source_history_budget" for event in warnings)
        assert app.runtime.source_progress(side) is None


@pytest.mark.parametrize("responses_runtime", [("files", False), ("sqlite", False)], indirect=True)
async def test_compacted_source_and_overlapping_queued_inputs(responses_runtime, tmp_path):
    build, replies, requests, _ = responses_runtime
    QueuedInputConfigManager(tmp_path).update(consumption_mode="all_at_once")
    async with build() as app:
        await login(app.provider_auth)
        replies.append(terminal([message("OLD-COVERED")]))
        initial = await app.runtime.run("Initial state")
        source = initial.session.session_id
        side = app.sessions.create_side_conversation(source, "Inspect").session.session_id
        replies.append(terminal([message("NEW-COVERED")]))
        await app.runtime.run("Trace result", session_id=source)
        first = app.runtime.source_progress(side)
        assert first
        dropped = await app.runtime.accept_user_input(
            UserInputRequest(prompt="Withdraw me", session_id=side, source_history=first)
        )
        assert app.sessions.undo(side, queue_id=dropped.queue_id).text_content == "Withdraw me"
        assert app.runtime.source_progress(side) == first
        await app.runtime.accept_user_input(
            UserInputRequest(prompt="Use the trace", session_id=side, source_history=first)
        )
        replies.append(terminal([message("NEW-TAIL")]))
        await app.runtime.run("Check again", session_id=source)
        messages = app.runtime.store.load_session(source).messages
        compaction = CompactionRecord(
            session_id=source,
            strategy_id="test-summary",
            trigger="manual",
            summary="MIXED-SUMMARY of OLD-COVERED and NEW-COVERED",
            covered_message_count=4,
            covered_end_message_id=messages[3].message_id,
            history_boundary_message_id=messages[-1].message_id,
        )
        commit_summary(app.runtime.store, compaction)
        select_summary(app.runtime.store, source, compaction.compaction_id)
        later_side = app.sessions.create_side_conversation(source, "After compaction").session
        replies.append(terminal([message("Using the summary")]))
        await app.runtime.run("Read the summary", session_id=later_side.session_id)
        assert "MIXED-SUMMARY" in json.dumps(requests[-1][0]["input"])
        assert "Initial state" not in json.dumps(requests[-1][0]["input"])
        second = app.runtime.source_progress(side)
        assert second and second.entries[: len(first.entries)] == first.entries
        await app.runtime.accept_user_input(
            UserInputRequest(prompt="Use the retest too", session_id=side, source_history=second)
        )
        replies.append(terminal([message("Combined answer")]))
        result = await app.runtime.run_pending_inputs(side, history_epoch=0)
        assert result and result.run.status == "completed"
        assert await app.runtime.run_pending_inputs(side, history_epoch=0) is None
        assert app.runtime.source_progress(side) is None
        notes = [
            m
            for m in app.runtime.store.load_session(side).messages
            if m.semantic_tag == "source-session-context"
        ]
        assert [m.text_content for m in notes] == [
            "[user] Trace result\n[assistant] NEW-COVERED",
            "[user] Check again\n[assistant] NEW-TAIL",
        ]
        wire = json.dumps(requests[-1][0]["input"])
        assert "MIXED-SUMMARY" not in wire and "Withdraw me" not in wire
        assert all(wire.count(marker) == 1 for marker in ("OLD-COVERED", "NEW-COVERED", "NEW-TAIL"))

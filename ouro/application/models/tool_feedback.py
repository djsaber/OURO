from __future__ import annotations

from dataclasses import asdict

from ouro.core.base import Metadata, utc_now
from ouro.core.content import TextContent
from ouro.core.events import EventRecord
from ouro.core.messages import MessageRecord
from ouro.core.model_execution import ModelExecutionResult
from ouro.core.session_commits import SessionCommit, SessionCommitConflict, SessionCommitStep
from ouro.core.session_ports import SessionStore
from ouro.core.state import ACTIVE_RUN_STATUSES, RunRecord, SessionRecord


def commit_unexecuted_model_tools(
    store: SessionStore, run: RunRecord, turn_id: str, response: ModelExecutionResult
) -> tuple[SessionRecord, RunRecord, tuple[EventRecord, ...]]:
    """Close this model turn and expose why its proposed tools did not execute."""
    with store.session_mutation(run.session_id):
        session = store.load_session_record(run.session_id)
        latest = store.load_run(run.session_id, run.run_id)
        if latest.history_epoch != session.history_epoch:
            raise SessionCommitConflict("Run belongs to withdrawn Session history")
        if latest.abort_requested or latest.status not in ACTIVE_RUN_STATUSES:
            return session, latest, ()
        header = store.load_latest_context_header(run.session_id)
        if header is None or header.run_id != run.run_id or header.turn_id != turn_id:
            raise SessionCommitConflict("model response is no longer current")
        if header.response_closed_at is not None:
            return session, latest, ()
        message = _tool_feedback(run, turn_id, response)
        committed = store.commit_session(
            SessionCommit(
                before=session,
                steps=(SessionCommitStep(message=message),),
                request_header=header.model_copy(update={"response_closed_at": utc_now()}),
            )
        )
        return committed.session, latest, committed.events


def _tool_feedback(run: RunRecord, turn_id: str, response: ModelExecutionResult) -> MessageRecord:
    failure = response.tool_call_failure
    metadata: Metadata = {}
    if failure is None:
        names = ", ".join(call.name for call in response.tool_calls)
        tag = "model-tools-skipped"
        text = f"The Driver did not execute the proposed tool calls: {names}."
    else:
        tag = "model-output-correction"
        text = f"No tool calls from that response were executed.\n{failure.error}"
        if failure.arguments is not None:
            start = max(0, failure.position - 256)
            excerpt = failure.arguments[start : failure.position + 768]
            text += (
                f"\nTool: {failure.name}; stream index: {failure.index}\n"
                f"Argument excerpt (characters {start}..{start + len(excerpt)}):\n{excerpt}"
            )
        metadata["rejected_response"] = {
            "error_code": failure.code,
            "tool_call_failure": asdict(failure),
            "text": response.text,
            "reasoning": response.reasoning,
            "model_response": response.model_response.model_dump(mode="json"),
        }
    return MessageRecord(
        session_id=run.session_id,
        run_id=run.run_id,
        turn_id=turn_id,
        role="harness",
        semantic_tag=tag,
        content=[TextContent(text=text)],
        metadata=metadata,
    )

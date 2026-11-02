from __future__ import annotations

from dataclasses import asdict

from ouro.core.base import utc_now
from ouro.core.events import EventRecord
from ouro.core.messages import MessageRecord
from ouro.core.model_execution import ModelExecutionResult
from ouro.core.session_commits import SessionCommit, SessionCommitConflict, SessionCommitStep
from ouro.core.state import ACTIVE_RUN_STATUSES, RunRecord, SessionRecord
from ouro.sessions import SessionStore

MAX_TOOL_ARGUMENT_RETRIES = 2


def commit_model_correction(
    store: SessionStore, run: RunRecord, turn_id: str, response: ModelExecutionResult
) -> tuple[SessionRecord, RunRecord, tuple[EventRecord, ...]]:
    """Close a rejected response and append its correction in one native commit.

    Only the scaffold enters model history. Rejected content stays in metadata,
    outside provider projection. Round-based counting survives pause/resume and
    resets after an intervening successful request without a success-path write.
    """
    failure = response.tool_call_failure
    assert failure is not None
    with store.session_mutation(run.session_id):
        session = store.load_session_record(run.session_id)
        latest = store.load_run(run.session_id, run.run_id)
        if latest.history_epoch != session.history_epoch:
            raise SessionCommitConflict("Run belongs to withdrawn Session history")
        if latest.abort_requested or latest.status not in ACTIVE_RUN_STATUSES:
            return session, latest, ()
        header = store.load_latest_context_header(run.session_id)
        if header is None or header.run_id != run.run_id or header.turn_id != turn_id:
            raise SessionCommitConflict("rejected response is no longer current")
        if header.response_closed_at is not None:
            return session, latest, ()
        previous_round = latest.metadata.get("model_correction_round")
        attempt = (
            int(latest.metadata.get("model_correction_attempt", 0)) + 1
            if previous_round == latest.model_round_count - 1
            else 1
        )
        start = max(0, failure.position - 256)
        excerpt = failure.arguments[start : failure.position + 768]
        message = MessageRecord.text(
            session_id=run.session_id,
            run_id=run.run_id,
            turn_id=turn_id,
            role="user",
            text=(
                "The previous response contained invalid tool arguments. "
                "No tool calls from that response were executed.\n"
                f"Tool: {failure.name}; stream index: {failure.index}\n"
                f"Error: {failure.error}\n"
                f"Rejected argument excerpt (characters {start}..{start + len(excerpt)}; "
                f"diagnostic text only):\n{excerpt}\n"
                "Generate the complete set of needed tool calls again with valid JSON objects. "
                "Escape control characters inside JSON strings."
            ),
        ).model_copy(
            update={
                "scaffold": True,
                "semantic_tag": "model-output-correction",
                "metadata": {
                    "model_correction_attempt": attempt,
                    "rejected_response": {
                        "error_code": response.error_code,
                        "tool_call_failure": asdict(failure),
                        "text": response.text,
                        "reasoning": response.reasoning,
                        "model_response": response.model_response.model_dump(mode="json"),
                    },
                },
            }
        )
        latest = latest.model_copy(
            update={
                "metadata": {
                    **latest.metadata,
                    "model_correction_round": latest.model_round_count,
                    "model_correction_attempt": attempt,
                }
            }
        )
        committed = store.commit_session(
            SessionCommit(
                before=session,
                steps=(SessionCommitStep(message=message),),
                runs=(latest,),
                request_header=header.model_copy(update={"response_closed_at": utc_now()}),
            )
        )
        return committed.session, latest, committed.events

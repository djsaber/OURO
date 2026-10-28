from __future__ import annotations

from ouro.core.base import utc_now
from ouro.core.context import ContextMetrics
from ouro.core.context_snapshots import ModelRequestHeader, RequestManifestRecord
from ouro.core.request_manifest import RequestManifest
from ouro.core.response_drafts import ResponseDraftChunk
from ouro.core.session_commits import SessionCommit
from ouro.core.state import RunRecord
from ouro.sessions import SessionRepository


def persist_response_draft(
    store: SessionRepository,
    run: RunRecord,
    *,
    turn_id: str,
    text: str = "",
    reasoning: str = "",
) -> ModelRequestHeader:
    """Build an interrupted request through its real preparation/append boundaries."""
    header = ModelRequestHeader(
        session_id=run.session_id,
        run_id=run.run_id,
        turn_id=turn_id,
        model_round=1,
        provider_id="test",
        model_id="test",
        started_at=utc_now(),
        metrics=ContextMetrics(estimated_input_tokens=1, estimated_fixed_tokens=0),
    )
    store.commit_session(
        SessionCommit(
            before=store.load_session_record(run.session_id),
            runs=(run.model_copy(update={"current_request_id": header.context_id}),),
            request_header=header,
            request_manifest=RequestManifestRecord(
                context_id=header.context_id, manifest=RequestManifest()
            ),
        )
    )
    for channel, content in (("reasoning", reasoning), ("text", text)):
        if content:
            store.append_response_draft(
                run.session_id,
                header.context_id,
                ResponseDraftChunk(channel=channel, content=content),
            )
    return header

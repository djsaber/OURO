from __future__ import annotations

from ouro.core.harness_calls import HarnessCallSnapshot
from ouro.sessions.base import SessionRepository


def capture_harness(
    store: SessionRepository, session_id: str, *, run_id: str | None = None
) -> HarnessCallSnapshot:
    """Capture one stage's business state under a short, consistent read boundary."""

    with store.session_mutation(session_id):
        session = store.load_session_record(session_id)
        version = store.load_harness(session_id, session.harness_version)
        return HarnessCallSnapshot(
            session_id=session_id,
            harness_version=version.version_id,
            history_epoch=session.history_epoch,
            last_message_id=session.last_message_id,
            run_id=run_id,
            state=version.state,
        )

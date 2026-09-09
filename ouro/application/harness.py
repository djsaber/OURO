from __future__ import annotations

from ouro.application.usage import UsageService
from ouro.core.errors import MigrationRequiredError
from ouro.core.harness_calls import HarnessCallSnapshot
from ouro.core.state import RunRecord, SessionRecord
from ouro.sessions.base import SessionRepository


def capture_harness(
    store: SessionRepository,
    session_id: str,
    *,
    run: RunRecord | None = None,
    usage_service: UsageService | None = None,
) -> HarnessCallSnapshot:
    """Capture one stage's business state under a short, consistent read boundary."""
    return capture_session_harness(store, session_id, run=run, usage_service=usage_service)[1]


def capture_session_harness(
    store: SessionRepository,
    session_id: str,
    *,
    run: RunRecord | None = None,
    usage_service: UsageService | None = None,
) -> tuple[SessionRecord, HarnessCallSnapshot]:
    """Read the Session preconditions and callback snapshot at the same boundary."""

    with store.session_mutation(session_id):
        session = store.load_session_record(session_id)
        version = store.load_harness(session_id, session.harness_version)
        if any(item.phase != "active" for item in version.state.controls.activations):
            raise MigrationRequiredError(
                "legacy controller transition state requires explicit maintenance before execution"
            )
        return session, HarnessCallSnapshot(
            session_id=session_id,
            harness_version=version.version_id,
            history_epoch=session.history_epoch,
            last_message_id=session.last_message_id,
            run_id=run.run_id if run is not None else None,
            state=version.state,
            run_controls=run.control_bindings if run is not None else (),
            usage_tokens=(
                usage_service.session_scope_tokens(session_id)
                if usage_service is not None
                else None
            ),
        )

"""Seed owner state in integration fixtures through the native Session commit."""

from ouro.core.harness import HarnessState
from ouro.core.session_commits import SessionCommit, SessionCommitStep
from ouro.core.session_extensions import SessionExtensionStateRecord
from ouro.sessions.base import SessionRepository


def replace_harness_state(store: SessionRepository, state: SessionExtensionStateRecord) -> None:
    with store.session_mutation(state.session_id):
        before = store.load_session_record(state.session_id)
        current = store.load_harness(state.session_id).state
        states = {item.owner_id: item for item in current.states}
        states[state.owner_id] = state
        store.commit_session(
            SessionCommit(
                before=before,
                steps=(
                    SessionCommitStep(
                        harness=HarnessState(
                            controls=current.controls, states=tuple(states.values())
                        )
                    ),
                ),
            )
        )


def remove_harness_state(store: SessionRepository, session_id: str, owner_id: str) -> None:
    with store.session_mutation(session_id):
        before = store.load_session_record(session_id)
        current = store.load_harness(session_id).state
        store.commit_session(
            SessionCommit(
                before=before,
                steps=(
                    SessionCommitStep(
                        harness=HarnessState(
                            controls=current.controls,
                            states=tuple(
                                item for item in current.states if item.owner_id != owner_id
                            ),
                        )
                    ),
                ),
            )
        )

from __future__ import annotations

from ouro.core.events import EventRecord
from ouro.core.results import RunResult
from ouro.core.tools import ToolResultValue
from ouro.sessions import SessionRepository


def run_events(store: SessionRepository, result: RunResult) -> list[EventRecord]:
    """Explicit historical query in assertions, never an execution accumulator."""
    events = []
    cursor = 0
    while True:
        page, next_cursor = store.load_events_page(
            result.session.session_id, cursor=cursor, run_id=result.run.run_id
        )
        events.extend(page)
        if next_cursor is None:
            return events
        cursor = next_cursor


def run_tool_results(store: SessionRepository, result: RunResult) -> list[ToolResultValue]:
    """Inspect certain observations in accepted call order, including unpublished results."""
    facts = store.load_session(result.session.session_id).tool_facts
    outcomes = {(item.identity, item.attempt): item.value for item in facts.outcomes}
    values = []
    for call in sorted(facts.calls, key=lambda item: (item.accepted_at, item.request.source_index)):
        if call.identity.run_id != result.run.run_id:
            continue
        attempts = sorted(attempt for identity, attempt in outcomes if identity == call.identity)
        if attempts:
            values.append(outcomes[call.identity, attempts[-1]])
    return values

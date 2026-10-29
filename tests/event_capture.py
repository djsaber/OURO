"""Test-owned recording of delivered notifications, never a Session storage API."""

from collections import defaultdict

from ouro.core.events import EventRecord

captured: dict[str, list[EventRecord]] = defaultdict(list)


def recorded_events(session_id: str, *, run_id: str | None = None) -> list[EventRecord]:
    return [event for event in captured[session_id] if run_id is None or event.run_id == run_id]

"""Pure title policy shared by Session creation, input commits, and explicit naming."""

from __future__ import annotations

from collections.abc import Iterable

from ouro.core.messages import MessageRecord
from ouro.core.state import DEFAULT_SESSION_TITLE, SessionRecord, SessionTitleSource

MAX_TITLE_LENGTH = 160
FALLBACK_TITLE_LENGTH = 60


def normalize_session_title(value: str) -> str:
    title = " ".join(value.split())
    if not title:
        raise ValueError("session title cannot be empty")
    if len(title) > MAX_TITLE_LENGTH:
        raise ValueError(f"session title exceeds {MAX_TITLE_LENGTH} characters")
    return title


def fallback_session_title(text: str) -> str | None:
    """Extract a bounded initial title from input text without inventing empty content."""

    return " ".join(text.split())[:FALLBACK_TITLE_LENGTH].rstrip() or None


def with_session_title(
    session: SessionRecord,
    title: str | None,
    *,
    source: SessionTitleSource = "fallback",
) -> SessionRecord:
    """Assign a validated title; absence of a title explicitly denotes a placeholder."""

    if title is None:
        title, source = DEFAULT_SESSION_TITLE, "placeholder"
    return session.model_copy(
        update={
            "title": normalize_session_title(title),
            "title_source": source,
            "title_locked": source == "user",
        }
    )


def fill_session_title(session: SessionRecord, messages: Iterable[MessageRecord]) -> SessionRecord:
    """Fill an unnamed Session from the first meaningful, non-scaffold user input."""

    if session.title_source != "placeholder" or session.title_locked:
        return session
    for message in messages:
        if message.role != "user" or message.scaffold:
            continue
        title = fallback_session_title(message.text_content)
        if title is None and message.skill_activations:
            title = fallback_session_title(f"Skill: {message.skill_activations[0].name}")
        if title is not None:
            return with_session_title(session, title)
    return session

"""Durable input drafts owned exclusively by the TUI."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from pathlib import Path

from pydantic import Field, ValidationError, field_validator

from ouro.core.base import VersionedOuroModel
from ouro.core.errors import ConfigError
from ouro.core.input_content import InputContent, InputFile, InputSkillReference
from ouro.local_io import read_text_atomic, write_model_atomic

MAX_SESSION_DRAFTS = 20


class TuiSessionDraft(InputContent):
    session_id: str

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("session_id must not be empty")
        return value


class TuiInputDrafts(VersionedOuroModel):
    """Welcome draft plus an MRU window of Session-owned drafts."""

    schema_version: int = 4
    welcome: InputContent = Field(default_factory=InputContent)
    sessions: tuple[TuiSessionDraft, ...] = ()

    @field_validator("sessions")
    @classmethod
    def validate_sessions(
        cls,
        value: tuple[TuiSessionDraft, ...],
    ) -> tuple[TuiSessionDraft, ...]:
        if len(value) > MAX_SESSION_DRAFTS:
            raise ValueError(f"sessions must contain at most {MAX_SESSION_DRAFTS} entries")
        session_ids = tuple(draft.session_id for draft in value)
        if len(set(session_ids)) != len(session_ids):
            raise ValueError("sessions must contain unique session IDs")
        if any(not draft.text and not draft.attachments and not draft.skills for draft in value):
            raise ValueError("session drafts must not be empty")
        return value

    def draft_for(self, session_id: str | None) -> str:
        if session_id is None:
            return self.welcome.text
        return next(
            (draft.text for draft in self.sessions if draft.session_id == session_id),
            "",
        )

    def resources_for(self, session_id: str | None) -> tuple[str, ...]:
        return self.content_for(session_id).resource_refs

    def content_for(self, session_id: str | None) -> InputContent:
        if session_id is None:
            return self.welcome
        return next(
            (
                InputContent(text=draft.text, attachments=draft.attachments, skills=draft.skills)
                for draft in self.sessions
                if draft.session_id == session_id
            ),
            InputContent(),
        )

    def remember(
        self,
        session_id: str | None,
        text: str,
        *,
        attachments: tuple[InputFile, ...] = (),
        skills: tuple[InputSkillReference, ...] = (),
    ) -> TuiInputDrafts:
        """Return state with one draft updated and empty drafts removed."""

        if session_id is None:
            content = InputContent(text=text, attachments=attachments, skills=skills)
            if content == self.welcome:
                return self
            return TuiInputDrafts(welcome=content, sessions=self.sessions)

        if not session_id.strip():
            raise ValueError("session_id must not be empty")
        current = next(
            (draft for draft in self.sessions if draft.session_id == session_id),
            None,
        )
        if (
            current is not None
            and current.text == text
            and current.attachments == attachments
            and current.skills == skills
        ):
            return self
        remaining = tuple(draft for draft in self.sessions if draft.session_id != session_id)
        sessions = (
            (
                TuiSessionDraft(
                    session_id=session_id, text=text, attachments=attachments, skills=skills
                ),
                *remaining,
            )[:MAX_SESSION_DRAFTS]
            if text or attachments or skills
            else remaining
        )
        return TuiInputDrafts(welcome=self.welcome, sessions=sessions)

    def forget_sessions(self, session_ids: Iterable[str]) -> TuiInputDrafts:
        forgotten = frozenset(session_ids)
        if not forgotten:
            return self
        sessions = tuple(draft for draft in self.sessions if draft.session_id not in forgotten)
        if sessions == self.sessions:
            return self
        return TuiInputDrafts(welcome=self.welcome, sessions=sessions)


class TuiInputDraftStore:
    """Read and atomically replace the TUI input-draft artifact."""

    def __init__(self, root: str | Path | None = None) -> None:
        root_value = root if root is not None else os.environ.get("OURO_HOME")
        ouro_home = Path(root_value) if root_value is not None else Path.home() / ".ouro"
        self.path = ouro_home / "config" / "tui-input-drafts.json"

    def load(self) -> TuiInputDrafts:
        if not self.path.exists():
            return TuiInputDrafts()
        try:
            raw = json.loads(read_text_atomic(self.path))
            return TuiInputDrafts.model_validate(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"invalid TUI input drafts {self.path}: {exc}") from exc
        except OSError as exc:
            raise ConfigError(f"could not read TUI input drafts {self.path}: {exc}") from exc
        except ValidationError as exc:
            raise ConfigError(f"invalid TUI input drafts {self.path}: {exc}") from exc

    def save(self, drafts: TuiInputDrafts) -> None:
        write_model_atomic(self.path, drafts)


__all__ = [
    "MAX_SESSION_DRAFTS",
    "TuiInputDrafts",
    "TuiInputDraftStore",
    "TuiSessionDraft",
]

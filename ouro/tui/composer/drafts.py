"""Durable input drafts owned exclusively by the TUI."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from pathlib import Path

from pydantic import Field, ValidationError, field_validator

from ouro.application.inputs.requests import UserMessageEdit
from ouro.core.base import VersionedOuroModel, new_id
from ouro.core.errors import ConfigError
from ouro.core.input_content import InputContent, InputFile, InputSkillReference
from ouro.local_io import read_text_atomic, write_model_atomic
from ouro.tui.view_models import TuiDraftConfiguration

MAX_SESSION_DRAFTS = 20


class TuiEditDraft(InputContent):
    target: UserMessageEdit


class TuiSessionDraft(InputContent):
    session_id: str
    edit: TuiEditDraft | None = None

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("session_id must not be empty")
        return value


class TuiPendingInput(InputContent):
    """A submission retained until its durable acceptance can be established."""

    input_id: str = Field(min_length=1)
    session_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
    origin_session_id: str | None = None
    edit: UserMessageEdit | None = None


class TuiInputDrafts(VersionedOuroModel):
    """Welcome draft plus an MRU window of Session-owned drafts."""

    schema_version: int = 4
    welcome: InputContent = Field(default_factory=InputContent)
    sessions: tuple[TuiSessionDraft, ...] = ()
    welcome_resource_id: str | None = Field(default=None, pattern=r"^ses_[0-9a-f]{32}$")
    welcome_workspace: str | None = None
    welcome_configuration: TuiDraftConfiguration | None = None
    pending: tuple[TuiPendingInput, ...] = ()

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
        if any(
            not (draft.text or draft.attachments or draft.skills or draft.edit) for draft in value
        ):
            raise ValueError("session drafts must not be empty")
        return value

    def draft_for(self, session_id: str | None) -> str:
        return self.content_for(session_id).text

    def resources_for(self, session_id: str | None) -> tuple[str, ...]:
        return self.content_for(session_id).resource_refs

    def content_for(self, session_id: str | None) -> InputContent:
        if session_id is None:
            return self.welcome
        draft = self._session_draft(session_id)
        if draft is None:
            return InputContent()
        content = draft.edit or draft
        return InputContent(
            text=content.text, attachments=content.attachments, skills=content.skills
        )

    def _session_draft(self, session_id: str | None) -> TuiSessionDraft | None:
        return next((draft for draft in self.sessions if draft.session_id == session_id), None)

    def edit_for(self, session_id: str | None) -> UserMessageEdit | None:
        draft = self._session_draft(session_id)
        return draft.edit.target if draft and draft.edit else None

    def edit_message(
        self, session_id: str, target: UserMessageEdit, content: InputContent
    ) -> TuiInputDrafts:
        draft = self._session_draft(session_id) or TuiSessionDraft(session_id=session_id)
        edit = TuiEditDraft(
            target=target, text=content.text, attachments=content.attachments, skills=content.skills
        )
        return self._save_session(draft.model_copy(update={"edit": edit}))

    def finish_edit(self, session_id: str) -> TuiInputDrafts:
        draft = self._session_draft(session_id)
        return self._save_session(draft.model_copy(update={"edit": None})) if draft else self

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
            return self.model_copy(update={"welcome": content})

        draft = self._session_draft(session_id) or TuiSessionDraft(session_id=session_id)
        current = draft.edit or draft
        if current.text == text and current.attachments == attachments and current.skills == skills:
            return self
        if draft.edit is not None:
            edit = TuiEditDraft(
                target=draft.edit.target, text=text, attachments=attachments, skills=skills
            )
            return self._save_session(draft.model_copy(update={"edit": edit}))
        return self._save_session(
            TuiSessionDraft(
                session_id=session_id, text=text, attachments=attachments, skills=skills
            )
        )

    def _save_session(self, draft: TuiSessionDraft) -> TuiInputDrafts:
        remaining = tuple(item for item in self.sessions if item.session_id != draft.session_id)
        sessions = (
            (draft, *remaining)[:MAX_SESSION_DRAFTS]
            if draft.text or draft.attachments or draft.skills or draft.edit
            else remaining
        )
        return self.model_copy(update={"sessions": sessions})

    def forget_sessions(self, session_ids: Iterable[str]) -> TuiInputDrafts:
        forgotten = frozenset(session_ids)
        if not forgotten:
            return self
        sessions = tuple(draft for draft in self.sessions if draft.session_id not in forgotten)
        if sessions == self.sessions:
            return self
        return self.model_copy(update={"sessions": sessions})

    def begin_submission(
        self, content: InputContent, *, session_id: str | None, workspace: str | None
    ) -> tuple[TuiInputDrafts, TuiPendingInput]:
        identity = session_id or self.welcome_resource_id or new_id("ses")
        pending = TuiPendingInput(
            input_id=new_id("queued"),
            session_id=identity,
            origin_session_id=session_id,
            edit=self.edit_for(session_id),
            text=content.text,
            attachments=content.attachments,
            skills=content.skills,
        )
        if session_id is None:
            drafts = self.model_copy(
                update={
                    "welcome": InputContent(),
                    "welcome_resource_id": identity,
                    "welcome_workspace": workspace,
                    "pending": (*self.pending, pending),
                }
            )
        else:
            drafts = self.finish_edit(session_id) if pending.edit else self.remember(session_id, "")
            drafts = drafts.model_copy(update={"pending": (*self.pending, pending)})
        return drafts, pending

    def finish_submission(self, input_id: str) -> TuiInputDrafts:
        return self.model_copy(
            update={"pending": tuple(item for item in self.pending if item.input_id != input_id)}
        )


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
    "TuiPendingInput",
    "TuiSessionDraft",
]
